"""Signed grants and durable local-cache behavior without hardware or network."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typix_copilot import authority as module
from typix_copilot.cache import ArtifactCache, CacheError
from typix_copilot.core import load_catalog
from typix_copilot.registry import REGISTRY_BASE, parse_catalog
from test_cache import Response


ROOT = Path(__file__).resolve().parents[1]


def document(rows):
    return json.dumps({"schema": 1, "firmwares": rows}, ensure_ascii=False).encode("utf-8")


def envelope(raw, signature):
    return module.MAGIC + struct.pack("!I", len(raw)) + signature + raw


def future_row():
    data = asdict(load_catalog()[0])
    data.update(id="community-instrument-1.0.0", version="1.0.0", title="Community instrument",
                filename="community-instrument.bin", commit="", publisher="第三方",
                source_url="https://github.com/typixdeck/copilot/tree/main/firmware/community/1.0.0",
                download_url="community/1.0.0/community-instrument.bin", hardware_verified=False)
    return data


class BundledGrantTests(unittest.TestCase):
    def setUp(self):
        module._proofs.clear()
        self.addCleanup(module._proofs.clear)

    def test_all_six_bundled_firmwares_have_exact_root_readable_grants(self):
        catalog = module.bundled_catalog()
        self.assertEqual(len(catalog), 6)
        self.assertEqual(sum(fw.publisher == "自研" for fw in catalog), 2)
        for fw in catalog:
            with self.subTest(firmware=fw.id):
                self.assertEqual(module.authorize_firmware(fw), fw)
                frame = module.encode_authorization(fw)
                self.assertIn(fw, module.decode_catalog(frame))
                with tempfile.TemporaryFile() as source:
                    source.write(frame + b"firmware bytes follow")
                    source.seek(0)
                    self.assertEqual(module.receive_authorization(source, fw.id), fw)
                    self.assertEqual(source.read(), b"firmware bytes follow")

    def test_builtin_metadata_mutation_is_not_granted(self):
        fw = module.bundled_catalog()[0]
        for changes in ({"sha256": "0" * 64}, {"size": fw.size + 1},
                        {"version": "fake"}, {"flash_offset": 4096},
                        {"id": "unlisted"}, {"hardware_verified": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                module.authorize_firmware(replace(fw, **changes))


class SignedGrantTests(unittest.TestCase):
    def setUp(self):
        module._proofs.clear()
        self.addCleanup(module._proofs.clear)
        self.key = Ed25519PrivateKey.generate()
        trust = patch.object(module, "_public_key", return_value=self.key.public_key())
        trust.start()
        self.addCleanup(trust.stop)
        self.raw = document([future_row()])
        self.signature = self.key.sign(self.raw)
        self.frame = envelope(self.raw, self.signature)

    def receive(self, frame, identifier):
        with tempfile.TemporaryFile() as source:
            source.write(frame)
            source.seek(0)
            return module.receive_authorization(source, identifier)

    def test_signed_future_custom_id_needs_no_version_allowlist(self):
        fw = module.verify_catalog(self.raw, self.signature)[0]
        self.assertNotIn(fw, load_catalog())
        self.assertFalse(fw.hardware_verified)
        self.assertEqual(module.authorize_firmware(fw), fw)
        self.assertEqual(module.encode_authorization(fw), self.frame)
        self.assertEqual(self.receive(self.frame, fw.id), fw)

    def test_signature_tampering_wrong_trust_and_changed_signed_metadata_reject(self):
        altered = document([{**future_row(), "version": "9.0.0"}])
        cases = [(altered, self.signature), (self.raw, bytes(64)),
                 (self.raw, self.signature[:-1]), (self.raw, self.signature + b"x"),
                 (self.raw, Ed25519PrivateKey.generate().sign(self.raw))]
        for raw, signature in cases:
            with self.subTest(length=len(signature)), self.assertRaises(ValueError):
                module.verify_catalog(raw, signature)
        fw = module.verify_catalog(self.raw, self.signature)[0]
        with patch.object(module, "_public_key", return_value=Ed25519PrivateKey.generate().public_key()):
            with self.assertRaises(ValueError):
                module.encode_authorization(fw)

    def test_valid_signature_cannot_bypass_catalog_schema_or_board_constraints(self):
        for raw in (b'{"schema":2,"firmwares":[]}',
                    document([{**future_row(), "chip": "esp32"}]),
                    document([{**future_row(), "flash_offset": 0x10000}]),
                    document([{**future_row(), "script": "command"}]),
                    document([{**future_row(), "download_url": "https://localhost/payload.bin"}])):
            with self.subTest(raw=raw[:64]), self.assertRaises(ValueError):
                module.verify_catalog(raw, self.key.sign(raw))

    def test_frame_magic_declared_length_bounds_trailing_data_and_truncation_reject(self):
        oversized_header = module.MAGIC + struct.pack("!I", module.MAX_CATALOG_BYTES + 1) + self.signature
        zero_header = module.MAGIC + struct.pack("!I", 0) + self.signature
        wrong_size = bytearray(self.frame)
        wrong_size[len(module.MAGIC):len(module.MAGIC) + 4] = struct.pack("!I", len(self.raw) + 1)
        cases = [b"", b"wrong" + self.frame[5:], self.frame[:-1], self.frame + b"x",
                 bytes(wrong_size), oversized_header, zero_header,
                 b"x" * (module.MAX_AUTHORIZATION_BYTES + 1)]
        for frame in cases:
            with self.subTest(length=len(frame)), self.assertRaises(ValueError):
                module.decode_catalog(frame)
        for raw in (b"", b"x" * (module.MAX_CATALOG_BYTES + 1), "not-bytes"):
            with self.assertRaises(ValueError):
                module.verify_catalog(raw, self.signature)

    def test_stdin_unknown_id_truncation_and_oversized_header_reject(self):
        with self.assertRaisesRegex(ValueError, "不在签名目录"):
            self.receive(self.frame, "not-published")
        for frame in (self.frame[:module.HEADER_BYTES - 1], self.frame[:-1],
                      b"invalid!" + self.frame[len(module.MAGIC):],
                      module.MAGIC + struct.pack("!I", module.MAX_CATALOG_BYTES + 1) + self.signature):
            with self.subTest(length=len(frame)), self.assertRaises(ValueError):
                self.receive(frame, future_row()["id"])

    def test_stdin_deadline_is_bounded_without_waiting_for_real_timeout(self):
        with tempfile.TemporaryFile() as source, patch.object(module.select, "select", return_value=([], [], [])):
            with self.assertRaisesRegex(ValueError, "超时"):
                module.receive_authorization(source, future_row()["id"])

    def test_in_memory_proof_cache_is_bounded(self):
        for version in range(20):
            raw = document([{**future_row(), "id": f"future-{version}", "version": str(version)}])
            module.verify_catalog(raw, self.key.sign(raw))
        self.assertEqual(len(module._proofs), 16)


class HistoricalCacheTests(unittest.TestCase):
    def setUp(self):
        module._proofs.clear()
        self.addCleanup(module._proofs.clear)
        self.key = Ed25519PrivateKey.generate()
        trust = patch.object(module, "_public_key", return_value=self.key.public_key())
        trust.start()
        self.addCleanup(trust.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = ArtifactCache(self.root / "cache")
        self.data = (ROOT / "firmware/typixdeck-diy/0.3.0/typixdeck-diy-0.3.0-full.bin").read_bytes()
        row = future_row()
        row.update(id="historical-diy-0.3.0", size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())
        self.raw = document([row])
        self.signature = self.key.sign(self.raw)
        self.firmware = module.verify_catalog(self.raw, self.signature)[0]
        self.cache._opener = Mock(side_effect=lambda request, **kw: Response(self.data, request.full_url))
        self.binary = self.cache.root / (self.firmware.sha256 + ".bin")
        self.proof = self.cache.root / self.cache._proof_name(self.firmware)

    def store(self):
        self.assertEqual(self.cache.ensure(self.firmware), self.binary)
        self.assertTrue(self.proof.is_file())
        self.assertEqual(self.proof.read_bytes(), self.firmware.id.encode() + b"\n" + envelope(self.raw, self.signature))

    def cold_cache(self):
        module._proofs.clear()
        cache = ArtifactCache(self.cache.root)
        cache._opener = Mock(side_effect=AssertionError("Old cached firmware must work offline"))
        return cache

    def test_cold_restart_lists_authorizes_and_reuses_retired_cached_firmware_offline(self):
        self.store()
        cache = self.cold_cache()
        self.assertEqual(cache.list_cached([]), [self.firmware])
        self.assertEqual(module.authorize_firmware(self.firmware), self.firmware)
        self.assertEqual(cache.ensure(self.firmware), self.binary)
        self.assertEqual(self.binary.read_bytes(), self.data)
        cache._opener.assert_not_called()
        module._proofs.clear()
        self.assertTrue(cache.restore_authorization(self.firmware))
        self.assertEqual(module.authorize_firmware(self.firmware), self.firmware)

    def test_remove_only_matching_cache_and_proof_preserves_backup_and_other_files(self):
        self.store()
        preserved = [self.root / "backups" / "current.bin", self.root / "maintenance.json",
                     self.cache.root / "unrelated.bin", self.cache.root / "notes.txt"]
        for path in preserved:
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"must survive")
        self.assertTrue(self.cache.remove(self.firmware))
        self.assertFalse(self.binary.exists())
        self.assertFalse(self.proof.exists())
        self.assertFalse(self.cache.remove(self.firmware))
        self.assertEqual(self.cache.list_cached([]), [])
        for path in preserved:
            self.assertEqual(path.read_bytes(), b"must survive")

    def test_shared_bytes_keep_each_signed_version_until_last_grant_removed(self):
        self.store()
        from dataclasses import asdict
        row = asdict(self.firmware)
        row.update(id="another-diy-0.4.0", version="0.4.0")
        raw = document([row])
        other = module.verify_catalog(raw, self.key.sign(raw))[0]
        self.cache.ensure(other)
        cache = self.cold_cache()
        self.assertCountEqual(cache.list_cached([]), [self.firmware, other])
        self.assertCountEqual(cache.list_cached([self.firmware, other]), [self.firmware, other])
        self.assertTrue(cache.restore_authorization(self.firmware))
        self.assertTrue(cache.restore_authorization(other))
        self.assertTrue(cache.remove(self.firmware))
        self.assertEqual(self.binary.read_bytes(), self.data)
        self.assertEqual(self.cold_cache().list_cached([]), [other])
        self.assertEqual(cache.list_cached([self.firmware, other]), [other])
        self.assertTrue(cache.remove(other))
        self.assertFalse(self.binary.exists())
        self.assertEqual(list(cache.root.glob("*.proof")), [])

    def test_legacy_sidecar_migrates_without_losing_shared_new_identity(self):
        self.store()
        legacy = self.cache.root / (self.firmware.sha256 + ".proof")
        self.proof.rename(legacy)
        cache = self.cold_cache()
        self.assertEqual(cache.list_cached([]), [self.firmware])
        self.assertTrue(cache.restore_authorization(self.firmware))
        cache.ensure(self.firmware)
        self.assertTrue(self.proof.exists())
        self.assertEqual(cache.list_cached([]), [self.firmware])
        self.assertTrue(cache.remove(self.firmware))
        self.assertFalse(legacy.exists())
        self.assertFalse(self.binary.exists())

    def test_full_identity_preserves_same_id_and_bytes_with_different_metadata(self):
        self.store()
        from dataclasses import asdict
        row = asdict(self.firmware)
        row.update(version="0.3.0-r2", title="A separately signed revision")
        raw = document([row])
        other = module.verify_catalog(raw, self.key.sign(raw))[0]
        self.cache.ensure(other)
        cache = self.cold_cache()
        self.assertCountEqual(cache.list_cached([]), [self.firmware, other])
        cache.remove(other)
        self.assertEqual(cache.list_cached([]), [self.firmware])

    def test_tampered_or_wrong_identity_proof_hides_retired_entry(self):
        self.store()
        original = self.proof.read_bytes()
        for bad in (original[:-1], original + b"x", b"different-id\n" + envelope(self.raw, self.signature),
                    self.firmware.id.encode() + b"\n" + envelope(self.raw, bytes(64))):
            self.proof.write_bytes(bad)
            cache = self.cold_cache()
            self.assertEqual(cache.list_cached([]), [])
            self.assertEqual(self.binary.read_bytes(), self.data)

    def test_corrupt_binary_never_lists_even_with_valid_signed_proof(self):
        self.store()
        self.binary.write_bytes(b"x" + self.data[1:])
        self.assertEqual(self.cold_cache().list_cached([]), [])

    def test_filesystem_failures_are_reported_as_cache_errors(self):
        import errno
        self.store()
        for method, args in ((self.cache.restore_authorization, (self.firmware,)),
                             (self.cache.list_cached, ([],)),
                             (self.cache.remove, (self.firmware,)),
                             (self.cache._remember, (self.firmware,))):
            with self.subTest(method=method.__name__), patch.object(
                    self.cache, "_directory", side_effect=OSError(errno.EACCES, "denied")):
                with self.assertRaises(CacheError):
                    method(*args)
        with patch("typix_copilot.cache.os.fsync", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaisesRegex(CacheError, "空间不足"):
                self.cache.ensure(self.firmware)
        self.assertEqual(self.binary.read_bytes(), self.data)
        self.assertEqual(list(self.cache.root.glob(".partial-*")), [])

    def test_unsafe_proof_is_not_followed_or_partially_removed(self):
        self.store()
        payload = self.proof.read_bytes()
        self.proof.unlink()
        outside = self.root / "outside-proof"
        outside.write_bytes(payload)
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            if kind == "symlink":
                self.proof.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, self.proof)
            elif kind == "fifo":
                os.mkfifo(self.proof)
            else:
                self.proof.mkdir()
            with self.subTest(kind=kind):
                self.assertEqual(self.cold_cache().list_cached([]), [])
                with self.assertRaises(CacheError):
                    self.cache.remove(self.firmware)
                self.assertTrue(self.binary.is_file())
                self.assertEqual(outside.read_bytes(), payload)
            self.proof.rmdir() if kind == "directory" else self.proof.unlink()


if __name__ == "__main__":
    unittest.main()
