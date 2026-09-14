"""Controlled offline transport tests; fixture bytes are not released firmware."""
from __future__ import annotations

from dataclasses import replace
import errno
import hashlib
import io
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typix_copilot import cache as module
from typix_copilot.cache import ArtifactCache, CacheError, Cancelled
from typix_copilot.core import load_catalog
from test_core import merged_bytes, image_bytes


class Response(io.BytesIO):
    def __init__(self, content, url, status=200, headers=None):
        super().__init__(content)
        self.url, self.status = url, status
        self.headers = {} if headers is None else headers

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read1(self, size):
        return self.read(size)


class OfficialMirrorTests(unittest.TestCase):
    def test_all_pinned_downloads_match_the_public_mirror_index(self):
        from typix_copilot.registry import parse_catalog, REGISTRY_BASE
        root = Path(__file__).resolve().parents[1]
        indexed = {fw.id: fw for fw in parse_catalog((root / "firmware/index.json").read_bytes())}
        for pinned in load_catalog():
            with self.subTest(firmware=pinned.id):
                expected = (REGISTRY_BASE + "typixdeck-official/" + pinned.version
                            + "/" + pinned.filename)
                self.assertEqual(module._known(pinned), expected)
                self.assertEqual(indexed[pinned.id].download_url, expected)
                self.assertEqual(indexed[pinned.id].sha256, pinned.sha256)
                self.assertEqual(indexed[pinned.id].source_url, pinned.source_url)

    def test_official_mirror_download_and_existing_cache_reuse(self):
        root = Path(__file__).resolve().parents[1]
        firmware = load_catalog()[-1]
        data = (root / "firmware/typixdeck-official" / firmware.version / firmware.filename).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            cache = ArtifactCache(Path(directory).resolve() / "cache")
            url = module._known(firmware)
            cache._opener = Mock(side_effect=lambda request, **kw: Response(data, request.full_url))
            target = cache.ensure(firmware)
            self.assertEqual(cache._opener.call_args.args[0].full_url, url)
            self.assertEqual(target.read_bytes(), data)
            cache._opener = Mock(side_effect=AssertionError("Verified cache must remain offline"))
            self.assertEqual(cache.ensure(firmware), target)


class CacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = merged_bytes()
        # Catalog injection is test-only. There is no corresponding release or
        # actual request for this artificial image and all-zero commit.
        original = load_catalog()[0]
        self.firmware = replace(original, id="test-only-fixture", filename="TEST_ONLY.bin",
                                commit="0" * 40, size=len(self.data),
                                sha256=hashlib.sha256(self.data).hexdigest(),
                                source_url="https://github.com/TypixNode/TypixDeck-esp32s3-firmware/blob/"
                                           + "0" * 40 + "/release/TEST_ONLY.bin")
        catalog_patch = patch.object(module, "load_catalog", return_value=[self.firmware])
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        self.cache = ArtifactCache(self.root / "cache")
        self.url = module._known(self.firmware)
        self.target = self.cache.root / (self.firmware.sha256 + ".bin")
        self.opener = Mock(side_effect=lambda req, **kwargs: Response(
            self.data, self.url, headers={"Content-Length": str(len(self.data))}))
        self.cache._opener = self.opener

    def assert_no_partial(self):
        self.assertEqual(list(self.root.rglob(".partial-*")), [])

    def test_download_progress_atomic_publish_rehash_and_offline_reuse(self):
        progress = []

        def report(done, total):
            self.assertFalse(self.target.exists())
            self.assertEqual(total, len(self.data))
            progress.append(done)

        self.assertIsNone(self.cache.cached(self.firmware))
        self.assertFalse(self.cache.root.exists())
        self.assertEqual(self.cache.ensure(self.firmware, progress=report), self.target)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(progress[0], 0)
        self.assertEqual(progress[-1], len(self.data))
        self.assertEqual(progress, sorted(set(progress)))
        self.assertEqual(self.opener.call_args.args[0].full_url, self.url)
        self.assertEqual(self.opener.call_args.kwargs, {"timeout": module.HTTP_TIMEOUT})
        self.assertEqual(self.opener.call_args.args[0].get_header("Accept-encoding"), "identity")
        self.cache._opener = Mock(side_effect=AssertionError("cache hit must be offline"))
        before = self.target.stat()
        report = Mock()
        self.assertEqual(self.cache.ensure(self.firmware, progress=report), self.target)
        report.assert_called_once_with(len(self.data), len(self.data))
        self.assertEqual(before.st_ino, self.target.stat().st_ino)
        self.target.write_bytes(b"broken")
        self.assertIsNone(self.cache.cached(self.firmware))
        self.assert_no_partial()

    def test_corrupt_entry_survives_failure_and_is_repaired_only_after_validation(self):
        self.cache.root.mkdir()
        self.cache.root.chmod(0o700)
        self.target.write_bytes(b"previous corrupt entry")
        self.cache._opener = Mock(return_value=Response(b"bad", self.url))
        with self.assertRaises(CacheError):
            self.cache.ensure(self.firmware)
        self.assertEqual(self.target.read_bytes(), b"previous corrupt entry")
        self.cache._opener = self.opener
        self.cache.ensure(self.firmware)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assert_no_partial()

    def test_rejects_truncated_extra_and_same_size_corrupt_payloads(self):
        for data in (self.data[:-1], self.data + b"x", b"x" + self.data[1:]):
            with self.subTest(length=len(data)):
                self.cache._opener = Mock(return_value=Response(data, self.url))
                with self.assertRaises(CacheError):
                    self.cache.ensure(self.firmware)
                self.assertFalse(self.target.exists())
                self.assert_no_partial()

    def test_rejects_non_200_redirect_destination_size_and_encoding(self):
        responses = [Response(self.data, self.url, status=206),
                     Response(self.data, "https://untrusted.example/firmware.bin"),
                     Response(self.data, self.url, headers={"Content-Length": "1"}),
                     Response(self.data, self.url, headers={"Content-Length": "bad"}),
                     Response(self.data, self.url, headers={"Content-Encoding": "gzip"})]
        for response in responses:
            self.cache._opener = Mock(return_value=response)
            with self.subTest(headers=response.headers), self.assertRaises(CacheError):
                self.cache.ensure(self.firmware)
            self.assertTrue(response.closed)
            self.assertFalse(self.target.exists())
            self.assert_no_partial()

    def test_cancel_before_download_during_transfer_and_before_commit(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            self.cache.ensure(self.firmware, cancel=cancel)
        self.opener.assert_not_called()
        self.assertFalse(self.cache.root.exists())
        for cutoff in (0, module.CHUNK_BYTES, len(self.data)):
            cancel.clear()

            def progress(done, _total):
                if done >= cutoff:
                    cancel.set()

            with self.assertRaises(Cancelled):
                self.cache.ensure(self.firmware, cancel, progress)
            self.assertFalse(self.target.exists())
            self.assert_no_partial()

    def test_cancel_during_final_verification_still_prevents_atomic_publish(self):
        cancel = threading.Event()
        validate = self.cache._valid

        def validation(descriptor, name, firmware):
            result = validate(descriptor, name, firmware)
            if name == self.target.name:
                cancel.set()
            return result

        with patch.object(self.cache, "_valid", side_effect=validation):
            with self.assertRaises(Cancelled):
                self.cache.ensure(self.firmware, cancel)
        self.assertFalse(self.target.exists())
        self.assert_no_partial()

    def test_cancellation_takes_precedence_over_interrupted_network_read(self):
        cancel = threading.Event()
        response = Response(self.data, self.url)

        def read(_size):
            cancel.set()
            raise socket.timeout()

        response.read = read
        self.cache._opener = Mock(return_value=response)
        with self.assertRaises(Cancelled):
            self.cache.ensure(self.firmware, cancel)
        self.assertFalse(self.target.exists())
        self.assert_no_partial()

    def test_http_offline_timeout_deadline_and_tls_errors_cleanup(self):
        errors = [urllib.error.HTTPError(self.url, 404, "missing", {}, None),
                  urllib.error.URLError("offline"), socket.timeout(),
                  ssl.SSLCertVerificationError("bad certificate")]
        for error in errors:
            self.cache._opener = Mock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(CacheError):
                self.cache.ensure(self.firmware)
            self.assert_no_partial()
        self.cache._opener = self.opener
        with patch.object(module.time, "monotonic", side_effect=[0, module.DOWNLOAD_DEADLINE + 1]):
            with self.assertRaisesRegex(CacheError, "超时"):
                self.cache.ensure(self.firmware)
        self.assertFalse(self.target.exists())
        self.assert_no_partial()

    def test_low_space_and_late_enospc_do_not_publish(self):
        with patch.object(module.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=1, f_frsize=1)):
            with self.assertRaisesRegex(CacheError, "空间不足"):
                self.cache.ensure(self.firmware)
        self.opener.assert_not_called()
        with patch.object(module.os, "fsync", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaisesRegex(CacheError, "空间不足"):
                self.cache.ensure(self.firmware)
        self.assertFalse(self.target.exists())
        self.assert_no_partial()

    def test_invalid_structure_cannot_be_published_even_with_exact_pinned_hash(self):
        for content in (b"not an ESP image", image_bytes()):
            firmware = replace(self.firmware, size=len(content), sha256=hashlib.sha256(content).hexdigest())
            self.cache._opener = Mock(return_value=Response(content, self.url))
            with patch.object(module, "load_catalog", return_value=[firmware]):
                with self.assertRaisesRegex(CacheError, "镜像结构"):
                    self.cache.ensure(firmware)
                self.assertIsNone(self.cache.cached(firmware))
            self.assert_no_partial()

    def test_unknown_modified_and_freeform_url_firmware_is_rejected_before_io(self):
        for firmware in (replace(self.firmware, id="unknown"),
                         replace(self.firmware, size=1),
                         replace(self.firmware, source_url="http://127.0.0.1/file")):
            for operation in (self.cache.cached, self.cache.ensure):
                with self.subTest(id=firmware.id), self.assertRaises(CacheError):
                    operation(firmware)
        self.opener.assert_not_called()
        self.assertFalse(self.cache.root.exists())
        for url in ("http://github.com/x", "https://github.com/other/repo/blob/x/file",
                    self.firmware.source_url + "?token=secret"):
            firmware = replace(self.firmware, source_url=url)
            with patch.object(module, "load_catalog", return_value=[firmware]):
                with self.assertRaisesRegex(CacheError, "官方地址"):
                    self.cache.ensure(firmware)

    def test_symlink_fifo_directory_and_hardlink_cache_entries_rejected(self):
        self.cache.root.mkdir()
        self.cache.root.chmod(0o700)
        external = self.root / "outside.bin"
        external.write_bytes(self.data)
        for kind in ("symlink", "fifo", "directory", "hardlink"):
            if kind == "symlink":
                self.target.symlink_to(external)
            elif kind == "fifo":
                os.mkfifo(self.target)
            elif kind == "directory":
                self.target.mkdir()
            else:
                os.link(external, self.target)
            with self.subTest(kind=kind):
                self.assertIsNone(self.cache.cached(self.firmware))
                with self.assertRaises(CacheError):
                    self.cache.ensure(self.firmware)
                self.opener.assert_not_called()
                self.assertEqual(external.read_bytes(), self.data)
            self.target.rmdir() if kind == "directory" else self.target.unlink()

    def test_unsafe_root_ancestor_shared_permissions_and_path_traversal_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        outside.chmod(0o700)
        link = self.root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        for root in (link, link / "child"):
            with self.assertRaises(CacheError):
                ArtifactCache(root).ensure(self.firmware)
        outside.chmod(0o777)
        with self.assertRaises(CacheError):
            ArtifactCache(outside).ensure(self.firmware)
        for root in (self.root / ".." / "escaped", Path("/")):
            with self.assertRaises(CacheError):
                ArtifactCache(root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_replaced_root_aborts_and_cleans_only_original_temporary_file(self):
        old = self.root / "renamed-cache"

        def progress(done, total):
            if done == total:
                self.cache.root.rename(old)
                self.cache.root.mkdir()
                self.cache.root.chmod(0o700)

        with self.assertRaisesRegex(CacheError, "目录已变化"):
            self.cache.ensure(self.firmware, progress=progress)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(old.iterdir()), [])
        self.assert_no_partial()

    def test_import_known_copies_exact_bytes_without_touching_source_and_reuses(self):
        source = self.root / "renamed-official.bin"
        source.write_bytes(self.data)
        before = source.stat()
        self.assertEqual(self.cache.import_known(source, self.firmware), self.target)
        self.assertEqual(source.read_bytes(), self.data)
        self.assertEqual(before.st_mtime_ns, source.stat().st_mtime_ns)
        self.assertEqual(self.target.read_bytes(), self.data)
        inode = self.target.stat().st_ino
        self.assertEqual(self.cache.import_known(source, self.firmware), self.target)
        self.assertEqual(inode, self.target.stat().st_ino)
        with patch.object(module.os, "fstatvfs", side_effect=AssertionError("cache reuse needs no free bytes")):
            self.assertEqual(self.cache.import_known(source, self.firmware), self.target)
        self.opener.assert_not_called()
        self.assert_no_partial()

    def test_import_unknown_corrupt_symlink_fifo_and_oversize_rejected(self):
        source = self.root / "fixture.bin"
        source.write_bytes(self.data)
        link = self.root / "link.bin"
        link.symlink_to(source)
        fifo = self.root / "fifo.bin"
        os.mkfifo(fifo)
        directory = self.root / "dir.bin"
        directory.mkdir()
        wrong = self.root / "wrong.bin"
        wrong.write_bytes(b"x" + self.data[1:])
        oversized = self.root / "oversized.bin"
        oversized.write_bytes(self.data + b"x")
        for path in (link, fifo, directory, wrong, oversized, self.root / "missing.bin", self.root / "wrong.txt"):
            with self.subTest(path=path.name), self.assertRaises(CacheError):
                self.cache.import_known(path, self.firmware)
            self.assertFalse(self.target.exists())
            self.assert_no_partial()
        with self.assertRaises(CacheError):
            self.cache.import_known(source, replace(self.firmware, id="unknown"))
        self.opener.assert_not_called()

    def test_import_low_space_and_failure_preserve_verified_existing_bytes(self):
        source = self.root / "fixture.bin"
        source.write_bytes(self.data)
        with patch.object(module.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=1, f_frsize=1)):
            with self.assertRaisesRegex(CacheError, "空间不足"):
                self.cache.import_known(source, self.firmware)
        self.cache.import_known(source, self.firmware)
        inode = self.target.stat().st_ino
        source.write_bytes(b"x" + self.data[1:])
        with self.assertRaises(CacheError):
            self.cache.import_known(source, self.firmware)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assert_no_partial()


class RegistryArtifactTests(unittest.TestCase):
    def setUp(self):
        from typix_copilot.registry import parse_catalog
        from dataclasses import asdict
        import json
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = merged_bytes()
        row = asdict(load_catalog()[0])
        row.update(id="registry-fixture", version="0.2.0", filename="TEST_ONLY.bin",
                   size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest(),
                   source_url="https://github.com/typixdeck/copilot/tree/main/firmware/test",
                   commit="", download_url="test/TEST_ONLY.bin")
        self.firmware = parse_catalog(json.dumps({"schema": 1, "firmwares": [row]}).encode())[0]
        self.cache = ArtifactCache(self.root / "cache")
        self.opener = Mock(side_effect=lambda req, **kw: Response(
            self.data, self.firmware.download_url, headers={"Content-Length": str(len(self.data))}))
        self.cache._opener = self.opener

    def test_registry_artifact_download_structure_digest_and_offline_reuse(self):
        self.assertIsNone(self.cache.cached(self.firmware))
        target = self.cache.ensure(self.firmware)
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(self.opener.call_args.args[0].full_url, self.firmware.download_url)
        self.cache._opener = Mock(side_effect=AssertionError("cache hit must be offline"))
        self.assertEqual(self.cache.ensure(self.firmware), target)
        self.assertNotIn(self.firmware, load_catalog())

    def test_constructed_remote_firmware_revalidated_before_any_cache_io(self):
        values = [replace(self.firmware, download_url="http://127.0.0.1/private"),
                  replace(self.firmware, download_url="https://evil.example/TEST_ONLY.bin"),
                  replace(self.firmware, download_url="../TEST_ONLY.bin"),
                  replace(self.firmware, size=True), replace(self.firmware, sha256="bad"),
                  replace(self.firmware, board="generic-esp32s3"),
                  replace(self.firmware, image_kind="app-image"),
                  replace(self.firmware, flash_offset=0x10000),
                  replace(self.firmware, id=load_catalog()[0].id)]
        for firmware in values:
            for operation in (self.cache.cached, self.cache.ensure):
                with self.subTest(url=firmware.download_url), self.assertRaises(CacheError):
                    operation(firmware)
        self.opener.assert_not_called()
        self.assertFalse(self.cache.root.exists())

    def test_registry_checksum_does_not_bypass_merged_image_validation(self):
        invalid = image_bytes()
        firmware = replace(self.firmware, size=len(invalid), sha256=hashlib.sha256(invalid).hexdigest())
        self.cache._opener = Mock(return_value=Response(invalid, firmware.download_url))
        with self.assertRaisesRegex(CacheError, "镜像结构"):
            self.cache.ensure(firmware)
        self.assertIsNone(self.cache.cached(firmware))
        self.assertEqual(list(self.root.rglob(".partial-*")), [])


class TransportTests(unittest.TestCase):
    def test_default_transport_requires_tls_and_rejects_redirects(self):
        request = urllib.request.Request("https://raw.githubusercontent.com/example")
        with patch.object(module.urllib.request, "build_opener") as factory:
            module._open_https(request, timeout=10)
        handlers = factory.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], module._NoRedirect)
        self.assertEqual(handlers[2]._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handlers[2]._context.check_hostname)
        with self.assertRaisesRegex(CacheError, "重定向"):
            handlers[1].redirect_request(request, None, 302, "redirect", {}, "http://localhost/file")


class ActualArtifactTests(unittest.TestCase):
    def test_existing_local_audit_bytes_import_and_reuse_without_network(self):
        evidence = Path(__file__).resolve().parents[3] / "artifacts/copilot-research-20260913/official/release"
        if not evidence.is_dir():
            self.skipTest("Actual audited upstream files are optional development evidence")
        with tempfile.TemporaryDirectory() as temporary:
            cache = ArtifactCache(Path(temporary).resolve() / "cache")
            cache._opener = Mock(side_effect=AssertionError("offline reuse must not request network"))
            for firmware in load_catalog():
                with self.subTest(filename=firmware.filename):
                    target = cache.import_known(evidence / firmware.filename, firmware)
                    self.assertEqual(target.stat().st_size, firmware.size)
                    self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), firmware.sha256)
                    self.assertEqual(cache.ensure(firmware), target)


if __name__ == "__main__":
    unittest.main()
