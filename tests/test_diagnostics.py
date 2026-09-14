"""Read-only log validation and formatting; temporary files, never device access."""
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typix_copilot import diagnostics
from typix_copilot.diagnostics import (
    DiagnosticError, MAX_ELAPSED_MS, MAX_LOG_BYTES, MAX_LOG_EVENTS,
    format_record_log, read_job_log, sanitize_diagnostics, validate_log,
)


def failed_record(**fields):
    record = dict(job_id="a" * 32, firmware_id="test-diy", version="0.3.0",
                  image_sha256="b" * 64, image_size=3997320,
                  phase="failed", status="failed", progress=.31,
                  failed_phase="backup", code="transport-failed",
                  timestamp=1789393200, started_at=1789393200, elapsed_ms=8000,
                  backup_complete=False, write_started=False, verified=False,
                  reconnected=False, attempted_offset=0xE0000,
                  last_checked_bytes=0xE0000, error_type="esptool.util.FatalError",
                  error_category="stream-stopped",
                  error_frames=[dict(module="typix_copilot.writer", function="read", line=510),
                                dict(module="esptool.loader", function="read_flash", line=1222)])
    record.update(fields)
    return record


def detailed_log(record=None):
    record = failed_record() if record is None else record
    return dict(schema=1, identity={key: record[key] for key in
                    ("job_id", "firmware_id", "version", "image_sha256", "image_size") if key in record},
                events=[dict(phase="enter", status="running", progress=.12, elapsed_ms=0),
                        dict(phase="connect", status="running", progress=.18, elapsed_ms=2000),
                        dict(phase="backup", status="running", progress=.31, elapsed_ms=4000,
                             read_bytes=0xE0000, read_total_bytes=8*1024*1024),
                        dict(phase="failed", status="failed", progress=.31, elapsed_ms=8000,
                             failed_phase="backup", code="transport-failed")], truncated=False)


class LogReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "logs"
        self.root.mkdir(mode=0o700)
        self.record = failed_record()
        self.path = self.root / (self.record["job_id"] + ".json")
        self.write(detailed_log(self.record))

    def write(self, payload):
        self.path.write_bytes(json.dumps(payload).encode())
        self.path.chmod(0o600)

    def read(self, record=None, **kwargs):
        return read_job_log(self.record if record is None else record,
                            root=kwargs.pop("root", self.root),
                            trusted_uid=kwargs.pop("trusted_uid", os.getuid()), **kwargs)

    def test_valid_file_preserves_exact_attempt_identity_and_read_progress(self):
        details = self.read()
        self.assertEqual(details["identity"], detailed_log(self.record)["identity"])
        self.assertEqual([event["phase"] for event in details["events"]],
                         ["enter", "connect", "backup", "failed"])
        self.assertEqual(details["events"][2]["read_bytes"], 0xE0000)
        text = format_record_log(self.record, details)
        for value in (self.record["job_id"], self.record["version"], self.record["image_sha256"],
                      "阶段日志", "连接与芯片检查", "917,504/8,388,608", "8.00s"):
            self.assertIn(value, text)

    def test_stale_partial_log_is_explicit_even_if_old_status_has_no_degraded_flag(self):
        payload = detailed_log(self.record)
        payload['events'].pop()
        self.write(payload)
        text = format_record_log(self.record, self.read())
        self.assertIn('阶段日志未保存到最终状态', text)
        self.assertIn('备份原固件', text)

    def test_missing_task_file_or_directory_is_legacy_without_fake_events(self):
        self.path.unlink()
        self.assertIsNone(self.read())
        self.root.rmdir()
        self.assertIsNone(self.read())
        without_job = {key: value for key, value in self.record.items() if key != "job_id"}
        self.assertIsNone(self.read(without_job))

    def test_invalid_job_ids_cannot_select_paths(self):
        for value in ("../secret", "/tmp/secret", "a" * 31, "A" * 32, "a" * 33, 123, [], "a" * 32 + "\n"):
            with self.subTest(job_id=repr(value)), self.assertRaises(DiagnosticError):
                self.read({**self.record, "job_id": value})

    def test_symlink_file_and_symlink_parent_are_rejected_without_reading_target(self):
        target = self.root.parent / "private-log"
        target.write_text("PRIVATE SERIAL DATA")
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(DiagnosticError) as error:
            self.read()
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn(str(target), str(error.exception))
        self.path.unlink()
        self.write(detailed_log(self.record))
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(DiagnosticError):
            self.read(root=alias)

    def test_hardlinked_fifo_and_directory_log_are_rejected(self):
        link = self.root.parent / "hardlink"
        os.link(self.path, link)
        with self.assertRaises(DiagnosticError):
            self.read()
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(DiagnosticError):
            self.read()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(DiagnosticError):
            self.read()

    def test_group_or_other_writable_file_or_directory_is_rejected(self):
        for mode in (0o620, 0o602, 0o666):
            with self.subTest(file_mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaises(DiagnosticError):
                    self.read()
        self.path.chmod(0o600)
        for mode in (0o720, 0o702, 0o777):
            with self.subTest(directory_mode=oct(mode)):
                self.root.chmod(mode)
                with self.assertRaises(DiagnosticError):
                    self.read()
        self.root.chmod(0o700)

    def test_directory_and_file_owners_must_match_trusted_uid(self):
        with self.assertRaises(DiagnosticError):
            self.read(trusted_uid=os.getuid() + 1)
        real_fstat = os.fstat

        def wrong_file_owner(handle):
            actual = real_fstat(handle)
            if not stat.S_ISREG(actual.st_mode):
                return actual
            return SimpleNamespace(st_mode=actual.st_mode, st_nlink=actual.st_nlink,
                                   st_uid=actual.st_uid + 1, st_size=actual.st_size)

        with patch.object(diagnostics.os, "fstat", side_effect=wrong_file_owner), self.assertRaises(DiagnosticError):
            self.read()

    def test_exact_byte_limit_is_readable_but_empty_oversized_and_bad_json_fail(self):
        data = self.path.read_bytes()
        self.path.write_bytes(data + b" " * (MAX_LOG_BYTES - len(data)))
        self.assertEqual(self.read()["identity"], detailed_log(self.record)["identity"])
        for data in (b"", b" " * (MAX_LOG_BYTES + 1), b"not json", b"\xff", b"[" * 5000 + b"]" * 5000):
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises(DiagnosticError):
                    self.read()

    def test_permission_error_is_fixed_safe_diagnostic(self):
        real_open = os.open

        def denied(path, *args, **kwargs):
            if path == self.path.name:
                raise PermissionError("PRIVATE /secret/path")
            return real_open(path, *args, **kwargs)

        with patch.object(diagnostics.os, "open", side_effect=denied), self.assertRaises(DiagnosticError) as error:
            self.read()
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("/secret/path", str(error.exception))

    def test_file_changed_during_read_is_rejected(self):
        real_fdopen = os.fdopen
        target = self.path

        class ChangedSource:
            def __init__(self, source):
                self.source = source

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.source.close()

            def fileno(self):
                return self.source.fileno()

            def read(self, count):
                data = self.source.read(count)
                target.write_bytes(data + b" ")
                return data

        with patch.object(diagnostics.os, "fdopen", side_effect=lambda *a, **k: ChangedSource(real_fdopen(*a, **k))):
            with self.assertRaises(DiagnosticError):
                self.read()

    def test_signed_firmware_identity_never_borrows_another_attempt_log(self):
        for change in ({"job_id": "c" * 32}, {"version": "0.2.0"}, {"image_sha256": "d" * 64},
                       {"image_size": 3997321}, {"firmware_id": "another-diy"}):
            with self.subTest(change=change):
                payload = detailed_log(self.record)
                payload["identity"].update(change)
                self.write(payload)
                with self.assertRaises(DiagnosticError):
                    self.read()


class LogValidationAndFormattingTests(unittest.TestCase):
    def setUp(self):
        self.record = failed_record()

    def test_old_02_record_shows_backup_address_and_safe_stack_without_invented_timeline(self):
        record = failed_record(version="0.2.0")
        for key in ("image_sha256", "image_size", "started_at", "elapsed_ms", "error_category"):
            record.pop(key)
        text = format_record_log(record)
        for value in ("0.2.0", "失败阶段：备份原固件", "0x000E0000", "917,504 字节",
                      "esptool.util.FatalError", "typix_copilot.writer.read : 510",
                      "此记录未保存完整阶段日志", "实际写入：未开始"):
            self.assertIn(value, text)
        self.assertNotIn("阶段日志（从系统写入组件开始计时）", text)
        self.assertNotIn("+", text)
        self.assertNotIn("写入完成", text)

    def test_packet_boundary_and_digest_wait_diagnostics_are_explicit(self):
        record = failed_record(chunk_received_bytes=4096, chunk_requested_bytes=65536,
                               awaiting_digest=True, last_packet_elapsed_ms=2500)
        text = format_record_log(record)
        self.assertIn("4,096/65,536", text)
        self.assertIn("等待当前块的校验摘要", text)
        self.assertIn("距最后数据包：2.50 秒", text)

    def test_received_invalid_digest_does_not_claim_waiting_for_a_digest(self):
        for category in ('digest-mismatch', 'digest-frame'):
            record = failed_record(chunk_received_bytes=65536, chunk_requested_bytes=65536,
                                   awaiting_digest=True, error_category=category)
            text = format_record_log(record)
            self.assertNotIn('等待当前块的校验摘要', text)
            self.assertIn('通信原因：', text)

    def test_event_count_limit_and_truncation_notice(self):
        payload = detailed_log(self.record)
        event = payload["events"][0]
        payload["events"] = [deepcopy(event) for _ in range(MAX_LOG_EVENTS)]
        payload["truncated"] = True
        self.assertEqual(len(validate_log(payload, self.record)["events"]), MAX_LOG_EVENTS)
        self.assertIn("部分较早进度已省略", format_record_log(self.record, payload))
        payload["events"].append(deepcopy(event))
        with self.assertRaises(DiagnosticError):
            validate_log(payload, self.record)

    def test_timeline_requires_nonnegative_monotonic_bounded_integer_time(self):
        for value in (-1, -2, True, 1.5, "1", MAX_ELAPSED_MS + 1):
            with self.subTest(elapsed=value):
                payload = detailed_log(self.record)
                payload["events"][0]["elapsed_ms"] = value
                with self.assertRaises(DiagnosticError):
                    validate_log(payload, self.record)
                self.assertIn("详细日志校验失败", format_record_log(self.record, payload))
        payload = detailed_log(self.record)
        payload["events"][2]["elapsed_ms"] = 1000
        with self.assertRaises(DiagnosticError):
            validate_log(payload, self.record)

    def test_malformed_envelope_and_events_reject_with_formatter_fallback(self):
        base = detailed_log(self.record)
        malformed = [None, [], {**base, "schema": True}, {**base, "schema": 2},
                     {**base, "truncated": "yes"}, {**base, "events": {}},
                     {**base, "identity": {**base["identity"], "version": "other"}}]
        for value in (None, [], {}, {"phase": "backup"},
                      dict(phase="shell", status="running", progress=.4, elapsed_ms=0),
                      dict(phase="backup", status="running", progress=float("nan"), elapsed_ms=0),
                      dict(phase="backup", status="running", progress=True, elapsed_ms=0),
                      dict(phase="backup", status="other", progress=.4, elapsed_ms=0)):
            malformed.append({**base, "events": [value]})
        for payload in malformed:
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(DiagnosticError):
                    validate_log(payload, self.record)
                if payload is not None:  # None is the documented legacy/no-file state.
                    self.assertIn("详细日志校验失败", format_record_log(self.record, payload))

    def test_unrecognized_exception_paths_bytes_and_identifiers_do_not_leak_to_copy_text(self):
        private = "PRIVATE_UNIQUE_SERIAL_123 /home/pi/private.bin"
        record = failed_record(error_message=private, exception=private, traceback=private,
                               serial_bytes=private, source_path=private, device_id=private,
                               error_frames=[dict(module="typix_copilot.writer", function="read", line=510,
                                                  source_path=private, locals=private),
                                             dict(module="private_module", function="SECRET", line=2),
                                             dict(module="esptool.loader", function="/private/function", line=2)])
        payload = detailed_log(record)
        payload["raw_traceback"] = private
        payload["events"][0].update(serial_bytes=private, exception=private, source_path=private,
                                    error_type="private_module.SecretError")
        text = format_record_log(record, payload)
        self.assertIn("typix_copilot.writer.read : 510", text)
        for forbidden in ("PRIVATE", "/home/pi", "private_module", "SECRET", "/private/function"):
            self.assertNotIn(forbidden, text)
        cleaned = validate_log(payload, record)
        self.assertNotIn(private, repr(cleaned))
        self.assertNotIn("serial_bytes", repr(cleaned))

    def test_numeric_and_stack_fields_are_strictly_bounded(self):
        for name, maximum in diagnostics.NUMBERS.items():
            with self.subTest(name=name):
                self.assertEqual(sanitize_diagnostics({name: maximum}), {name: maximum})
                for invalid in (-1, maximum + 1, True, "1", 1.25):
                    self.assertNotIn(name, sanitize_diagnostics({name: invalid}))
        frames = [dict(module="esptool.loader", function="read_flash", line=100)] * 20
        self.assertEqual(len(sanitize_diagnostics({"error_frames": frames})["error_frames"]), 8)
        for name in ("/secret/Error", "esptool.util.PRIVATE\nVALUE", "unknown.private.Error"):
            self.assertNotIn("error_type", sanitize_diagnostics({"error_type": name}))

    def test_invalid_record_never_produces_untrusted_visible_text(self):
        for record in (None, [], {**self.record, "firmware_id": "/private/secret"},
                       {**self.record, "version": "PRIVATE\nSECRET"},
                       {**self.record, "image_sha256": "PRIVATE"}):
            with self.subTest(record=repr(record)):
                self.assertEqual(format_record_log(record), "写入记录无效，无法显示详细日志。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
