"""Actual byte preservation boundaries with no serial or privileged I/O."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from typix_copilot.core import load_catalog
from typix_copilot.registry import parse_catalog, RegistryError
from typix_copilot.settings import effective_image, SettingsError, NVS_OFFSET, NVS_SIZE
from typix_copilot.writer import Journal, execute
from test_core import image_bytes, merged_bytes


def fixture():
    image = merged_bytes(partitions=[(1, 2, 0x9000, 0x6000, b"nvs"),
                                    (1, 1, 0xF000, 0x1000, b"phy_init"),
                                    (0, 0, 0x10000, 0x200000, b"factory"),
                                    (1, 0x82, 0x210000, 0x400000, b"font")])
    backup = bytearray(image + b"\xff" * (8 * 1024 * 1024 - len(image)))
    backup[NVS_OFFSET:NVS_OFFSET + NVS_SIZE] = b"PRIVATE_WIFI_PASSWORD".ljust(NVS_SIZE, b"\xa5")
    fw = replace(load_catalog()[0], id="typixdeck-diy-test", version="test",
                 size=len(image), sha256=hashlib.sha256(image).hexdigest(),
                 source_url="https://github.com/typixdeck/diy-esp32s3-firmware/tree/main",
                 download_url="typixdeck-diy/test/TEST_ONLY.bin", filename="TEST_ONLY.bin",
                 nvs_reset=False, settings_policy="preserve-diy-v1",
                 settings_compatible_apps=(hashlib.sha256(image_bytes()).hexdigest(),))
    return fw, image, bytes(backup)


class SettingsBytesTests(unittest.TestCase):
    def setUp(self):
        self.fw, self.image, self.backup = fixture()

    def test_only_nvs_changes_and_private_backup_remains_untouched(self):
        output, count = effective_image(self.fw, self.image, self.backup)
        self.assertEqual(count, NVS_SIZE)
        self.assertEqual(output[:NVS_OFFSET], self.image[:NVS_OFFSET])
        self.assertEqual(output[NVS_OFFSET + NVS_SIZE:], self.image[NVS_OFFSET + NVS_SIZE:])
        self.assertEqual(output[NVS_OFFSET:NVS_OFFSET + NVS_SIZE], self.backup[NVS_OFFSET:NVS_OFFSET + NVS_SIZE])
        self.assertEqual(len(output), len(self.image))

    def test_same_layout_alone_is_not_a_settings_schema_grant(self):
        fw = replace(self.fw, settings_compatible_apps=("0" * 64,))
        with self.assertRaisesRegex(SettingsError, "settings-source"):
            effective_image(fw, self.image, self.backup)

    def test_exact_partition_sector_and_complete_backup_are_required(self):
        for offset in (0x8003, 0x8004, 0x801C, 0x8022, 0x80F0, 0x8FFF):
            broken = bytearray(self.backup)
            broken[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaisesRegex(SettingsError, "settings-layout"):
                effective_image(self.fw, self.image, bytes(broken))
        with self.assertRaisesRegex(SettingsError, "settings-layout"):
            effective_image(self.fw, self.image, self.backup[:0x20000])

    def test_signed_image_cannot_install_settings_while_claiming_preservation(self):
        image = bytearray(self.image)
        image[NVS_OFFSET] = 0
        with self.assertRaisesRegex(SettingsError, "settings-image-data"):
            effective_image(self.fw, bytes(image), self.backup)

    def test_app_integrity_is_checked_before_hash_allowlist(self):
        backup = bytearray(self.backup)
        backup[0x10030] ^= 1
        with self.assertRaisesRegex(SettingsError, "settings-source"):
            effective_image(self.fw, self.image, bytes(backup))

    def test_legacy_reset_does_not_copy_private_settings_to_other_firmware(self):
        fw = replace(self.fw, settings_policy="reset", settings_compatible_apps=(), nvs_reset=True)
        self.assertEqual(effective_image(fw, self.image, self.backup), (self.image, 0))

    def test_reject_other_namespace_even_with_app_hash_and_same_layout(self):
        fw = replace(self.fw, source_url="https://github.com/typixdeck/copilot/tree/main", id="community-test")
        with self.assertRaisesRegex(SettingsError, "settings-policy"):
            effective_image(fw, self.image, self.backup)


class SignedPolicyTests(unittest.TestCase):
    def test_existing_v1_cache_proof_filename_remains_exactly_compatible(self):
        from typix_copilot.cache import ArtifactCache
        fw = load_catalog()[0]
        old = asdict(fw)
        old.pop("settings_policy"); old.pop("settings_compatible_apps")
        raw = json.dumps(old, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self.assertEqual(ArtifactCache._proof_name(fw), fw.sha256 + "." + hashlib.sha256(raw).hexdigest() + ".proof")

    def test_v2_preservation_fields_round_trip_and_v1_never_grants_them(self):
        fw, _, _ = fixture()
        raw = asdict(fw)
        parsed = parse_catalog(json.dumps({"schema": 2, "firmwares": [raw]}).encode())[0]
        self.assertEqual(parsed.settings_compatible_apps, fw.settings_compatible_apps)
        with self.assertRaises(RegistryError):
            parse_catalog(json.dumps({"schema": 1, "firmwares": [raw]}).encode())
        raw.pop("settings_policy"); raw.pop("settings_compatible_apps")
        raw["nvs_reset"] = True
        old = parse_catalog(json.dumps({"schema": 1, "firmwares": [raw]}).encode())[0]
        self.assertEqual(old.settings_policy, "reset")

    def test_malformed_or_contradictory_policies_reject_before_authorization(self):
        fw, _, _ = fixture()
        for values in ({"settings_policy": "all"}, {"settings_policy": None},
                       {"settings_compatible_apps": []}, {"settings_compatible_apps": [None]},
                       {"settings_compatible_apps": [{}]}, {"settings_compatible_apps": ["A" * 64]},
                       {"settings_compatible_apps": ["0" * 64] * 33}, {"nvs_reset": True},
                       {"settings_policy": "reset"}, {"id": "community-test"}):
            with self.subTest(values=values), self.assertRaises(RegistryError):
                parse_catalog(json.dumps({"schema": 2, "firmwares": [{**asdict(fw), **values}]}).encode())


class PreservedTransactionTests(unittest.TestCase):
    def setUp(self):
        self.fw, self.image, self.backup = fixture()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "state"
        self.events = []
        self.journal = Journal(self.root, self.fw, self.events.append)
        (self.journal.job / "image.bin").write_bytes(self.image)
        outer = self
        class Transport:
            def __init__(self): self.calls = []; self.written = None
            def enter(self): self.calls.append("enter"); return "bound"
            def connect(self, endpoint): return len(outer.backup)
            def read(self, size, phase):
                self.calls.append(phase)
                return outer.backup if phase == "backup" else self.written
            def write(self, data): self.calls.append("write"); self.written = data
            def restart(self): self.calls.append("restart")
            def close(self): self.calls.append("close")
        self.transport = Transport()

    def test_preserves_settings_and_independently_verifies_effective_bytes(self):
        self.assertEqual(execute(self.fw, self.image, self.journal, self.transport), 0)
        expected, _ = effective_image(self.fw, self.image, self.backup)
        self.assertEqual(self.transport.written, expected)
        private = self.journal.job / "effective-image.bin"
        self.assertEqual(private.read_bytes(), expected)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.journal.job / "image.bin").read_bytes(), self.image)
        self.assertTrue(self.events[-1]["settings_preserved"])
        self.assertEqual(self.events[-1]["image_sha256"], self.fw.sha256)
        for path in [self.root / "status.json", *self.root.joinpath("logs").glob("*.json")]:
            data = path.read_bytes()
            self.assertNotIn(b"PRIVATE_WIFI_PASSWORD", data)
            self.assertNotIn(hashlib.sha256(expected).hexdigest().encode(), data)

    def test_source_mismatch_keeps_full_backup_without_starting_write(self):
        broken = bytearray(self.backup); broken[0x8FFF] = 0
        self.backup = bytes(broken)
        self.assertEqual(execute(self.fw, self.image, self.journal, self.transport), 1)
        self.assertNotIn("write", self.transport.calls)
        self.assertTrue(self.events[-1]["backup_complete"])
        self.assertFalse(self.events[-1]["write_started"])
        self.assertEqual(self.events[-1]["code"], "settings-layout")

    def test_failed_private_effective_image_save_prevents_destructive_write(self):
        from typix_copilot import writer
        original = writer.private_write
        def save(path, data, *args):
            if path.name == "effective-image.bin": raise OSError("storage full")
            return original(path, data, *args)
        with patch.object(writer, "private_write", side_effect=save):
            self.assertEqual(execute(self.fw, self.image, self.journal, self.transport), 1)
        self.assertNotIn("write", self.transport.calls)

    def test_corrupted_preserved_sector_fails_readback_and_does_not_restart(self):
        old_read = self.transport.read
        def read(size, phase):
            data = bytearray(old_read(size, phase))
            if phase == "verify": data[NVS_OFFSET] ^= 1
            return bytes(data)
        self.transport.read = read
        self.assertEqual(execute(self.fw, self.image, self.journal, self.transport), 1)
        self.assertNotIn("restart", self.transport.calls)
        self.assertEqual(self.events[-1]["code"], "verify-mismatch")

    def test_prewrite_refusal_reports_possible_maintenance_mode_without_inventing_recovery(self):
        from typix_copilot.live import event_message
        event = {"status": "failed", "phase": "failed", "code": "settings-source",
                 "boot_requested": True, "write_started": False, "reconnected": False,
                 "companion_resume_deferred": True}
        text = event_message(event)
        self.assertIn("未执行写入", text)
        self.assertIn("维护模式", text)
        self.assertIn("需要恢复", text)
        self.assertIn("串口服务", text)
