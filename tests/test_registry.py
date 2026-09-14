"""Registry boundaries and offline fallback with deterministic local transports."""
from __future__ import annotations

from dataclasses import asdict, replace
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typix_copilot import authority
from typix_copilot import registry as module
from typix_copilot.cache import Cancelled
from typix_copilot.core import load_catalog
from typix_copilot.registry import (FirmwareRegistry, RegistryError, merge_catalog,
                                   parse_catalog, validate_registry_firmware)
from test_cache import Response


def row():
    data = asdict(load_catalog()[0])
    data.update(id="test-diy-0.2.0", version="0.2.0", title="DIY test candidate",
                filename="TEST_ONLY.bin", source_url="https://github.com/typixdeck/copilot/tree/main/firmware/test/0.2.0",
                commit="", download_url="test/0.2.0/TEST_ONLY.bin", publisher="TypixDeck")
    return data


def document(rows=None):
    return json.dumps({"schema": 1, "firmwares": [row()] if rows is None else rows},
                      ensure_ascii=False).encode("utf-8")


class SchemaTests(unittest.TestCase):
    def test_relative_url_normalized_without_claiming_writer_approval(self):
        firmware = parse_catalog(document())[0]
        self.assertEqual(firmware.download_url, module.REGISTRY_BASE + "test/0.2.0/TEST_ONLY.bin")
        self.assertEqual(firmware.commit, "")
        self.assertFalse(firmware.hardware_verified)
        self.assertNotIn(firmware, load_catalog())
        self.assertEqual(validate_registry_firmware(firmware), firmware.download_url)
        self.assertEqual(parse_catalog(document([asdict(firmware)])), [firmware])

    def test_rejects_unknown_fields_missing_fields_duplicate_keys_and_bad_schema(self):
        modified = row()
        modified["script"] = "run-this"
        missing = row()
        missing.pop("sha256")
        documents = [document([modified]), document([missing]), b'{"schema":1,"schema":1,"firmwares":[]}',
                     b'{"schema":true,"firmwares":[]}', b'{"schema":2,"firmwares":[]}',
                     b'{"schema":1,"firmwares":[],"port":"/dev/ttyUSB0"}', b'{"schema":1,"firmwares":NaN}',
                     b'{"schema":1,"firmwares":null}', b'{"schema":1,"firmwares":[{"id":"a","id":"b"}]}',
                     b'[]', b'\xff', b'[' * 1500]
        for content in documents:
            with self.subTest(content=content[:70]), self.assertRaises(RegistryError):
                parse_catalog(content)

    def test_rejects_excess_size_count_and_duplicate_ids(self):
        for content in (b" " * (module.MAX_INDEX_BYTES + 1), document([row()] * 101), document([row()] * 2), b""):
            with self.assertRaises(RegistryError):
                parse_catalog(content)
        self.assertEqual(parse_catalog(document([])), [])

    def test_rejects_malformed_fields_chip_board_and_layout_metadata(self):
        cases = {"id": ["../a", "", "a" * 81, ["a"]],
                 "title": ["bad\ntext", "bad\u202etext", " bad", "x" * 121],
                 "summary": [None, "x" * 641], "filename": ["../foo.bin", ".bin", "f.txt"],
                 "size": [True, 0, -1, 16 * 1024 * 1024 + 1, "5"],
                 "sha256": ["a" * 63, "A" * 64, None], "commit": ["main", "1" * 39, None],
                 "capabilities": [["x"] * 17, ["bad\n"], None], "nvs_reset": [1, None],
                 "hardware_verified": [1, "yes"], "flash_offset": [True, 65536, "0"],
                 "chip": ["esp32", None], "board": ["esp32s3-generic"],
                 "image_kind": ["app-image"], "publisher": ["x" * 81]}
        for field, values in cases.items():
            for value in values:
                data = row()
                data[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(RegistryError):
                    parse_catalog(document([data]))

    def test_only_fixed_repository_binary_paths_are_accepted(self):
        paths = ["http://127.0.0.1/TEST_ONLY.bin", "https://localhost/TEST_ONLY.bin",
                 "https://raw.githubusercontent.com:443/typixdeck/copilot/main/firmware/TEST_ONLY.bin",
                 "https://raw.githubusercontent.com/evil/repo/main/TEST_ONLY.bin",
                 "https://raw.githubusercontent.com/typixdeck/copilot/other/firmware/TEST_ONLY.bin",
                 "//host/TEST_ONLY.bin", "/TEST_ONLY.bin", "../TEST_ONLY.bin", "./TEST_ONLY.bin",
                 "a//TEST_ONLY.bin", "a/../TEST_ONLY.bin", "a/%2e%2e/TEST_ONLY.bin",
                 "a/TEST_ONLY.bin?token=x", "a/TEST_ONLY.bin#x", "a\\TEST_ONLY.bin", "a/OTHER.bin"]
        for path in paths:
            data = row()
            data["download_url"] = path
            with self.subTest(path=path), self.assertRaises(RegistryError):
                parse_catalog(document([data]))

    def test_source_url_cannot_contain_credentials_queries_or_external_hosts(self):
        urls = ["https://[invalid/typixdeck/copilot", "http://github.com/typixdeck/copilot", "https://github.com@localhost/typixdeck/copilot",
                "https://github.com/typixdeck/copilot?token=x", "https://github.com/typixdeck/copilot#x",
                "https://github.com/evil/copilot", "https://github.com/typixdeck/../copilot",
                "https://github.com/typixdeck/copilot/%2f.."]
        for url in urls:
            data = row()
            data["source_url"] = url
            with self.subTest(url=url), self.assertRaises(RegistryError):
                parse_catalog(document([data]))

    def test_pinned_entries_cannot_be_overridden_and_merge_keeps_original_objects(self):
        original = load_catalog()[0]
        pinned_row = asdict(original)
        pinned_row["download_url"] = "official/" + original.filename
        remote = parse_catalog(document([pinned_row, row()]))
        merged = merge_catalog(remote)
        self.assertEqual(merged[:-1], load_catalog())
        self.assertEqual(merged[-1].id, row()["id"])
        self.assertEqual(merged[0].download_url, "")
        for changes in ({"sha256": "0" * 64}, {"summary": "Changed"}, {"size": original.size + 1},
                        {"id": "new-name", "size": original.size + 1}):
            with self.subTest(changes=changes), self.assertRaises(RegistryError):
                parse_catalog(document([{**pinned_row, **changes}]))
        with self.assertRaises(RegistryError):
            merge_catalog(remote + remote)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.registry = FirmwareRegistry(self.root / "registry")
        self.key = Ed25519PrivateKey.generate()
        trust_patch = patch.object(authority, "_public_key", return_value=self.key.public_key())
        trust_patch.start()
        self.addCleanup(trust_patch.stop)
        authority._proofs.clear()
        self.addCleanup(authority._proofs.clear)
        self.data = document()
        self.signature = self.key.sign(self.data)
        self.frame = authority.MAGIC + struct.pack("!I", len(self.data)) + self.signature + self.data
        self.target = self.registry._storage.root / module.SNAPSHOT_NAME
        def response(req, **kw):
            self.assertIn(req.full_url, (module.REGISTRY_URL, module.SIGNATURE_URL))
            payload = self.data if req.full_url == module.REGISTRY_URL else self.signature
            return Response(payload, req.full_url, headers={"Content-Length": str(len(payload))})
        self.opener = Mock(side_effect=response)
        self.registry._opener = self.opener

    def assert_no_partial(self):
        self.assertEqual(list(self.root.rglob(".partial-*")), [])

    def test_fetch_persists_valid_snapshot_and_offline_load_revalidates(self):
        self.assertEqual(self.registry.cached_catalog(), [])
        self.assertFalse(self.target.parent.exists())
        result = self.registry.fetch_catalog()
        self.assertEqual(result, parse_catalog(self.data))
        self.assertEqual(self.target.read_bytes(), self.frame)
        self.assertEqual([call.args[0].full_url for call in self.opener.call_args_list],
                         [module.REGISTRY_URL, module.SIGNATURE_URL])
        for call in self.opener.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": module.HTTP_TIMEOUT})
            self.assertEqual(call.args[0].get_header("Accept-encoding"), "identity")
        authority._proofs.clear()
        self.assertEqual(FirmwareRegistry(self.target.parent).cached_catalog(), result)
        self.registry._opener = Mock(side_effect=urllib.error.URLError("offline"))
        with self.assertRaises(RegistryError):
            self.registry.fetch_catalog()
        self.assertEqual(self.registry.cached_catalog(), result)
        self.target.write_bytes(b"broken")
        with self.assertRaises(RegistryError):
            self.registry.cached_catalog()
        self.assert_no_partial()

    def test_failed_refresh_preserves_last_snapshot(self):
        self.registry.fetch_catalog()
        previous = self.target.read_bytes()
        bad_responses = [Response(b"invalid", module.REGISTRY_URL),
                         Response(self.data, module.REGISTRY_URL, status=206),
                         Response(self.data, "https://localhost/index.json"),
                         Response(self.data, module.REGISTRY_URL, headers={"Content-Encoding": "gzip"}),
                         Response(self.data, module.REGISTRY_URL, headers={"Content-Length": "1"}),
                         Response(self.data, module.REGISTRY_URL, headers={"Content-Length": "9" * 10000}),
                         Response(self.data, module.REGISTRY_URL, headers={"Content-Length": "-1"}),
                         Response(b"x" * (module.MAX_INDEX_BYTES + 1), module.REGISTRY_URL)]
        for response in bad_responses:
            self.registry._opener = Mock(return_value=response)
            with self.assertRaises(RegistryError):
                self.registry.fetch_catalog()
            self.assertTrue(response.closed)
            self.assertEqual(self.target.read_bytes(), previous)
        self.assert_no_partial()

    def test_invalid_signature_or_signed_invalid_schema_preserves_previous_snapshot(self):
        self.registry.fetch_catalog()
        previous = self.target.read_bytes()
        candidates = [
            (self.data, bytes(64)),
            (self.data, self.signature[:-1]),
            (self.data, self.signature + b"x"),
            (self.data, Ed25519PrivateKey.generate().sign(self.data)),
            (document([{**row(), "title": "Tampered metadata"}]), self.signature),
            (b'{"schema":2,"firmwares":[]}', self.key.sign(b'{"schema":2,"firmwares":[]}')),
        ]
        for data, signature in candidates:
            def response(req, **kw):
                payload = data if req.full_url == module.REGISTRY_URL else signature
                return Response(payload, req.full_url)
            self.registry._opener = Mock(side_effect=response)
            with self.subTest(data=data[:32], length=len(signature)), self.assertRaises(RegistryError):
                self.registry.fetch_catalog()
            self.assertEqual(self.target.read_bytes(), previous)
            self.assertEqual(self.registry.cached_catalog(), parse_catalog(self.data))
        self.assert_no_partial()

    def test_signature_transport_cannot_redirect_or_use_compression(self):
        self.registry.fetch_catalog()
        previous = self.target.read_bytes()
        for wrong in (
            Response(self.signature, "https://localhost/signature"),
            Response(self.signature, module.SIGNATURE_URL, status=206),
            Response(self.signature, module.SIGNATURE_URL, headers={"Content-Encoding": "gzip"}),
        ):
            self.registry._opener = Mock(side_effect=lambda req, **kw:
                Response(self.data, req.full_url) if req.full_url == module.REGISTRY_URL else wrong)
            with self.assertRaises(RegistryError):
                self.registry.fetch_catalog()
            self.assertTrue(wrong.closed)
            self.assertEqual(self.target.read_bytes(), previous)
        self.assert_no_partial()

    def test_cancellation_before_transfer_during_transfer_and_before_commit(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            self.registry.fetch_catalog(event)
        self.opener.assert_not_called()
        event.clear()
        response = Response(self.data, module.REGISTRY_URL)
        original = response.read

        def cancelled_read(size):
            event.set()
            return original(size)

        response.read = cancelled_read
        self.registry._opener = Mock(return_value=response)
        with self.assertRaises(Cancelled):
            self.registry.fetch_catalog(event)
        event.clear()
        self.registry._opener = self.opener
        with patch.object(module.os, "fsync", side_effect=lambda _fd: event.set()):
            with self.assertRaises(Cancelled):
                self.registry.fetch_catalog(event)
        self.assertFalse(self.target.exists())
        self.assert_no_partial()

    def test_deadline_and_network_failures_leave_existing_snapshot(self):
        self.registry.fetch_catalog()
        before = self.target.read_bytes()
        with patch.object(module.time, "monotonic", side_effect=[0, module.FETCH_DEADLINE + 1]):
            with self.assertRaisesRegex(RegistryError, "超时"):
                self.registry.fetch_catalog()
        for error in (TimeoutError(), urllib.error.HTTPError(module.REGISTRY_URL, 404, "missing", {}, None)):
            self.registry._opener = Mock(side_effect=error)
            with self.assertRaises(RegistryError):
                self.registry.fetch_catalog()
        self.assertEqual(self.target.read_bytes(), before)
        self.assert_no_partial()

    def test_low_space_and_write_failure_preserve_existing_snapshot(self):
        self.registry.fetch_catalog()
        before = self.target.read_bytes()
        with patch.object(module.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=1, f_frsize=1)):
            with self.assertRaisesRegex(RegistryError, "空间不足"):
                self.registry.fetch_catalog()
        with patch.object(module.os, "fsync", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaisesRegex(RegistryError, "空间不足"):
                self.registry.fetch_catalog()
        self.assertEqual(self.target.read_bytes(), before)
        self.assert_no_partial()

    def test_symlink_hardlink_fifo_or_directory_snapshot_is_never_followed(self):
        self.target.parent.mkdir(mode=0o700)
        outside = self.root / "outside.json"
        outside.write_bytes(self.data)
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            if kind == "symlink":
                self.target.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, self.target)
            elif kind == "fifo":
                os.mkfifo(self.target)
            else:
                self.target.mkdir()
            for operation in (self.registry.cached_catalog, self.registry.fetch_catalog):
                with self.subTest(kind=kind), self.assertRaises(RegistryError):
                    operation()
            self.assertEqual(outside.read_bytes(), self.data)
            self.target.rmdir() if kind == "directory" else self.target.unlink()
        self.assert_no_partial()

    def test_symlink_root_or_replaced_root_cannot_redirect_writes(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.target.parent.symlink_to(outside, target_is_directory=True)
        for operation in (self.registry.cached_catalog, self.registry.fetch_catalog):
            with self.assertRaises(RegistryError):
                operation()
        self.assertEqual(list(outside.iterdir()), [])
        self.target.parent.unlink()
        same = self.registry._storage._same_directory
        old = self.root / "old"

        def swapped(descriptor):
            self.target.parent.rename(old)
            self.target.parent.mkdir(mode=0o700)
            same(descriptor)

        with patch.object(self.registry._storage, "_same_directory", side_effect=swapped):
            with self.assertRaises(RegistryError):
                self.registry.fetch_catalog()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(old.iterdir()), [])
        self.assert_no_partial()


if __name__ == "__main__":
    unittest.main()
