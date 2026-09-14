"""Offline format, provenance and state-transition checks for the preview."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typix_copilot.core import MAX_IMAGE_BYTES, Simulation, inspect_local, load_catalog


ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "artifacts/copilot-research-20260913"


def image_bytes(*, application=True, chip_id=9, hash_appended=True, payload=None):
    """Small explicit test fixture; never a downloadable firmware artifact."""
    if payload is None:
        payload = (b"\x32\x54\xcd\xab" if application else b"BOOT") + bytes(252)
    header = bytearray(24)
    header[:4] = bytes([0xE9, 1, 2, 0x3F])
    struct.pack_into("<H", header, 12, chip_id)
    header[23] = int(hash_appended)
    image = header + struct.pack("<II", 0x3C000020, len(payload)) + payload
    checksum = 0xEF
    for byte in payload:
        checksum ^= byte
    image += bytes(15 - len(image) % 16) + bytes([checksum])
    if hash_appended:
        image += hashlib.sha256(image).digest()
    return bytes(image)


def merged_bytes(*, partitions=None, md5=True):
    if partitions is None:
        partitions = [(1, 2, 0x9000, 0x6000, b"nvs"),
                      (1, 1, 0xF000, 0x1000, b"phy_init"),
                      (0, 0, 0x10000, 0x100000, b"factory")]
    table = b"".join(struct.pack("<HBBII16sI", 0x50AA, kind, subtype, offset,
                                 size, label, 0)
                     for kind, subtype, offset, size, label in partitions)
    if md5:
        table += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(table).digest()
    table += b"\xff" * (0x1000 - len(table))
    boot = image_bytes(application=False)
    return boot + b"\xff" * (0x8000 - len(boot)) + table + b"\xff" * 0x7000 + image_bytes()


class CatalogTests(unittest.TestCase):
    def test_pinned_known_artifacts(self):
        catalog = load_catalog()
        self.assertEqual([f.id for f in catalog], ["official-20260910", "official-20260821",
                                                 "official-20260815", "official-20260814"])
        self.assertEqual([f.size for f in catalog], [3997320, 3936704, 464208, 462448])
        self.assertEqual(catalog[0].sha256,
                         "515860212f0812b2c9b0cc98766d5a95d4be599a06d2d36b522af6b20d5b1ec3")
        for firmware in catalog:
            self.assertTrue(firmware.nvs_reset)
            self.assertEqual(firmware.commit, "fde9dac3b687a92fa2a6a049b5cd953dcb367b23")
            self.assertEqual(firmware.source_url,
                             "https://github.com/TypixNode/TypixDeck-esp32s3-firmware/blob/"
                             + firmware.commit + "/release/" + firmware.filename)
            self.assertIsInstance(firmware.capabilities, tuple)
        with self.assertRaises(FrozenInstanceError):
            catalog[0].version = "changed"

    @unittest.skipUnless((EVIDENCE / "image-inspection.json").is_file(),
                         "Optional local audit fixtures are not distributed with Copilot")
    def test_catalog_and_parser_match_all_actual_audited_images(self):
        audit = {row["name"]: row for row in json.loads(
            (EVIDENCE / "image-inspection.json").read_text())}
        for firmware in load_catalog():
            with self.subTest(firmware=firmware.id):
                expected = audit[firmware.filename]
                self.assertEqual((firmware.size, firmware.sha256, firmware.nvs_reset),
                                 (expected["bytes"], expected["sha256"], expected["nvs_erased_bytes"]))
                result = inspect_local(EVIDENCE / "official/release" / firmware.filename)
                self.assertEqual((result["size"], result["sha256"]), (firmware.size, firmware.sha256))
                self.assertEqual(result["kind"], "merged-image")
                self.assertEqual(result["partitions"], [
                    {"label": row["name"], "offset": int(row["offset"], 16),
                     "size": int(row["size"], 16)} for row in expected["partitions"]])
        result = inspect_local(EVIDENCE / "official/typixdeck_uac_only_macos_verified_20260812.bin")
        self.assertEqual(result["kind"], "app-image")
        self.assertEqual(result["partitions"], [])


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, content, name="fixture.bin"):
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_app_and_merged_classification_without_claiming_board_verification(self):
        for content, kind in [(image_bytes(), "app-image"), (merged_bytes(), "merged-image")]:
            with self.subTest(kind=kind):
                path = self.write(content)
                before = path.stat()
                result = inspect_local(path)
                self.assertEqual(result["kind"], kind)
                self.assertEqual(result["chip_id"], 9)
                self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(result["name"], path.name)
                self.assertNotIn(str(self.root), json.dumps(result, ensure_ascii=False))
                self.assertIn("不代表板级兼容性", result["warnings"][0])
                self.assertEqual(before.st_mtime_ns, path.stat().st_mtime_ns)
                self.assertEqual(path.read_bytes(), content)

    def test_application_payload_cannot_be_misread_as_partition_table(self):
        payload = bytearray(0x9000)
        payload[:4] = b"\x32\x54\xcd\xab"
        payload[0x8000 - 32:0x8000 - 30] = b"\xaa\x50"
        result = inspect_local(self.write(image_bytes(payload=payload)))
        self.assertEqual(result["kind"], "app-image")
        self.assertEqual(result["partitions"], [])

    def test_supports_checksum_only_image_but_not_bare_bootloader(self):
        self.assertEqual(inspect_local(self.write(image_bytes(hash_appended=False)))["kind"],
                         "app-image")
        with self.assertRaisesRegex(ValueError, "无法确认"):
            inspect_local(self.write(image_bytes(application=False)))

    def test_rejects_truncation_of_headers_segments_checksums_and_tables(self):
        app = image_bytes()
        merged = merged_bytes()
        for content in [b"", b"\xe9", app[:23], app[:31], app[:80], app[:-33], app[:-1],
                        merged[:0x8001], merged[:0x9000], merged[:-1]]:
            with self.subTest(length=len(content)), self.assertRaises(ValueError):
                inspect_local(self.write(content))

    def test_rejects_malformed_headers_unsupported_chip_and_corruption(self):
        app = image_bytes()
        cases = [image_bytes(chip_id=0), image_bytes(payload=b"\x32\x54\xcd\xab")]
        for index, value in [(0, 0), (1, 0), (1, 17), (2, 255), (19, 1), (23, 2),
                             (100, 1), (len(app) - 1, 1)]:
            altered = bytearray(app)
            altered[index] = value
            cases.append(altered)
        for content in cases:
            with self.subTest(head=bytes(content[:24]).hex()), self.assertRaises(ValueError):
                inspect_local(self.write(content))

    def test_rejects_invalid_partition_digest_overlap_and_alignment(self):
        bad_digest = bytearray(merged_bytes())
        bad_digest[0x8000 + 3 * 32 + 16] ^= 1
        corrupt_app = bytearray(merged_bytes())
        corrupt_app[0x10000 + 100] ^= 1
        for content in [bad_digest, corrupt_app,
                        merged_bytes(partitions=[(1, 2, 0x9000, 0x10000, b"nvs"),
                                                  (0, 0, 0x10000, 0x100000, b"factory")]),
                        merged_bytes(partitions=[(0, 0, 0x11000, 0x100000, b"factory")]),
                        merged_bytes(partitions=[(0, 0, 0x10000, 0x1000000, b"factory")])]:
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                inspect_local(self.write(content))

    def test_rejects_symlink_directory_fifo_missing_extension_and_oversize(self):
        ordinary = self.write(image_bytes())
        link = self.root / "link.bin"
        link.symlink_to(ordinary)
        directory = self.root / "directory.bin"
        directory.mkdir()
        fifo = self.root / "pipe.bin"
        os.mkfifo(fifo)
        oversize = self.root / "oversize.bin"
        with oversize.open("wb") as stream:
            stream.truncate(MAX_IMAGE_BYTES + 1)
        for path in [link, directory, fifo, self.root / "missing.bin",
                     self.write(image_bytes(), "file.txt"), oversize, Path("/dev/null")]:
            with self.subTest(name=path.name), self.assertRaises(ValueError) as error:
                inspect_local(path)
            self.assertNotIn(str(self.root), str(error.exception))

    def test_same_name_catalog_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "固定审计"):
            inspect_local(self.write(merged_bytes(), load_catalog()[0].filename))

    def test_filename_controls_are_not_exposed(self):
        result = inspect_local(self.write(image_bytes(), "line\nbreak.bin"))
        self.assertNotIn("\n", result["name"])


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog()
        self.sim = Simulation(self.catalog)
        self.first, self.second = self.catalog[0].id, self.catalog[1].id

    def finish(self):
        snapshots = []
        for _ in range(39):
            result = self.sim.tick()
            snapshots.append(result)
            self.assertTrue(result["simulation"])
            self.assertIn("模拟", result["message"])
            self.assertGreaterEqual(result["progress"], 0)
            self.assertLessEqual(result["progress"], 1)
            if result["status"] != "running":
                self.assertEqual([row["progress"] for row in snapshots],
                                 sorted(row["progress"] for row in snapshots))
                return snapshots
        self.fail("Simulation did not reach a terminal state in fewer than 40 ticks")

    def test_starts_empty_download_does_not_set_current_and_switch_uses_cache(self):
        self.assertIsNone(self.sim.current)
        self.assertIsNone(self.sim.active)
        self.assertEqual((self.sim.cached, self.sim.history), (set(), []))
        self.sim.start(self.first, "download")
        self.assertIn("download", [row["phase"] for row in self.finish()])
        self.assertIsNone(self.sim.current)
        self.assertEqual(self.sim.cached, {self.first})
        self.sim.start(self.first)
        phases = [row["phase"] for row in self.finish()]
        self.assertNotIn("download", phases)
        self.assertIn("write", phases)
        self.assertIn("restart", phases)
        self.assertEqual(self.sim.current, self.first)
        self.assertEqual(self.sim.active["progress"], 1)
        self.assertEqual([row["operation"] for row in self.sim.history], ["switch", "download"])

    def test_uncached_switch_and_restore_complete(self):
        self.sim.start(self.first)
        self.assertIn("download", [row["phase"] for row in self.finish()])
        self.sim.start(self.second, "restore")
        self.assertEqual(self.finish()[-1]["status"], "succeeded")
        self.assertEqual(self.sim.current, self.second)
        self.assertEqual(self.sim.history[0]["operation"], "restore")
        self.assertEqual(self.sim.cached, {self.first, self.second})

    def test_all_failures_preserve_previous_current_and_allow_retry(self):
        self.sim.start(self.first)
        self.finish()
        for operation in ["download", "switch", "restore"]:
            for scenario in ["verification-failure", "disconnected"]:
                with self.subTest(operation=operation, scenario=scenario):
                    self.sim.start(self.second, operation, scenario)
                    terminal = self.finish()[-1]
                    self.assertEqual(terminal["status"], "failed")
                    self.assertEqual(self.sim.current, self.first)
                    self.assertNotIn(self.second, self.sim.cached)
                    previous_history = list(self.sim.history)
                    self.assertEqual(self.sim.tick(), terminal)
                    self.assertEqual(self.sim.cancel(), terminal)
                    self.assertEqual(self.sim.history, previous_history)
        self.sim.start(self.second)
        self.assertEqual(self.finish()[-1]["status"], "succeeded")
        self.assertEqual(self.sim.current, self.second)

    def test_cancel_at_every_step_keeps_state_and_stale_ticks_cannot_complete(self):
        for count in range(25):
            with self.subTest(cancel_after=count):
                self.sim.reset()
                self.sim.start(self.first)
                self.finish()
                self.sim.start(self.second)
                for _ in range(count):
                    self.sim.tick()
                terminal = self.sim.cancel()
                self.assertEqual(terminal["status"], "cancelled")
                self.assertEqual(self.sim.current, self.first)
                self.assertNotIn(self.second, self.sim.cached)
                for _ in range(40):
                    self.assertEqual(self.sim.tick(), terminal)
                self.assertEqual(self.sim.cancel(), terminal)
                self.assertEqual(len(self.sim.history), 2)

    def test_terminal_idempotency_detached_snapshots_and_reset(self):
        self.sim.start(self.first)
        snapshot = self.sim.tick()
        snapshot["id"] = "untrusted"
        self.sim.active["status"] = "succeeded"
        self.assertEqual(self.sim.active["id"], self.first)
        self.assertEqual(self.sim.active["status"], "running")
        terminal = self.finish()[-1]
        for _ in range(40):
            self.assertEqual(self.sim.tick(), terminal)
            self.assertEqual(self.sim.cancel(), terminal)
        self.assertEqual(len(self.sim.history), 1)
        self.sim.reset()
        self.assertEqual((self.sim.cached, self.sim.history), (set(), []))
        self.assertIsNone(self.sim.current)
        self.assertIsNone(self.sim.active)
        with self.assertRaises(ValueError):
            self.sim.tick()
        with self.assertRaises(ValueError):
            self.sim.cancel()

    def test_unknown_busy_invalid_operations_and_scenarios_cannot_bypass(self):
        for args in [("unknown",), (self.first, "flash"), (self.first, "switch", "unknown")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.sim.start(*args)
            self.assertIsNone(self.sim.active)
        self.sim.start(self.first)
        original = self.sim.active
        with self.assertRaises(ValueError):
            self.sim.start(self.second)
        self.assertEqual(self.sim.active, original)
        with self.assertRaises(ValueError):
            Simulation([self.catalog[0], self.catalog[0]])


if __name__ == "__main__":
    unittest.main()
