"""Controller checks with synthetic image bytes and a fake child, never hardware."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typix_copilot import cache as cache_module, live
from typix_copilot.cache import ArtifactCache
from typix_copilot.core import load_catalog
from test_core import merged_bytes
from test_cache import Response


class Child:
    def __init__(self, events=(), returncode=0, alive=False, raw=None):
        self.stdout = io.BytesIO(raw if raw is not None else b"".join(json.dumps(event).encode() + b"\n" for event in events))
        self.returncode = returncode
        self.alive = alive
        self.terminate = Mock(side_effect=AssertionError("must not terminate maintenance"))
        self.kill = Mock(side_effect=AssertionError("must not kill maintenance"))

    def wait(self, timeout=None):
        if self.alive:
            raise subprocess.TimeoutExpired("test-only child", timeout)
        return self.returncode

    def poll(self):
        return None if self.alive else self.returncode


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bytes = merged_bytes()
        # Synthetic fixture accepted only through patched in-process catalogs.
        # It is never submitted to pkexec or represented as an upstream artifact.
        original = load_catalog()[0]
        self.firmware = replace(original, filename="TEST_ONLY.bin", size=len(self.bytes),
                                sha256=hashlib.sha256(self.bytes).hexdigest(),
                                source_url=original.source_url.rsplit("/", 1)[0] + "/TEST_ONLY.bin")
        self.other = load_catalog()[1]
        for module in (live, cache_module):
            mocked = patch.object(module, "load_catalog", return_value=[self.firmware, self.other])
            mocked.start()
            self.addCleanup(mocked.stop)
        self.cache = ArtifactCache(self.root / "cache")
        self.cache._opener = Mock(side_effect=lambda request, **kwargs: Response(self.bytes, request.full_url))
        self.child = Child([self.event("complete", "succeeded", verified=True, reconnected=True)])
        self.spawn = Mock(side_effect=lambda *args, **kwargs: self.child)
        self.controller = live.LiveController(self.cache, popen=self.spawn, status_reader=lambda: ([], None))

    def event(self, phase="prepare", status="running", **fields):
        event = dict(phase=phase, status=status, firmware_id=self.firmware.id, version=self.firmware.version,
                     job_id="a" * 32, progress=1. if status == "succeeded" else .1,
                     backup_complete=False, write_started=False, verified=False, reconnected=False,
                     runtime_version_confirmed=False)
        event.update(fields)
        return event

    def test_real_cache_precedes_fixed_helper_and_only_valid_terminal_succeeds(self):
        events = []

        def spawn(argv, **kwargs):
            self.assertEqual(events[-1]["phase"], "authorize")
            self.assertFalse(self.controller.can_cancel)
            self.assertEqual(argv, ["/usr/bin/pkexec", "/usr/libexec/typix-copilot-write", "official-20260910"])
            self.assertEqual(kwargs["stdin"].read(), self.bytes)
            kwargs["stdin"].seek(0)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("shell", kwargs)
            self.assertNotIn("env", kwargs)
            return self.child

        self.spawn.side_effect = spawn
        result = self.controller.run_write(self.firmware, on_event=events.append)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["verified"] and result["reconnected"])
        self.assertFalse(result["runtime_version_confirmed"])
        self.assertEqual(events[-1], result)
        self.assertEqual(sum(event["status"] == "succeeded" for event in events), 1)
        self.assertFalse(self.controller.busy)
        self.assertTrue(self.child.stdout.closed)
        self.assertIn("prepare", [event["phase"] for event in events])
        self.assertIsNotNone(self.cache.cached(self.firmware))

    def test_cancel_before_or_during_download_never_spawns_helper(self):
        for during in (False, True):
            cancel = threading.Event()
            if not during:
                cancel.set()

            def event(record):
                if during and record["phase"] == "prepare":
                    self.controller.request_cancel(cancel)

            result = self.controller.run_write(self.firmware, cancel, event)
            self.assertEqual(result["code"], "cancelled")
            self.assertFalse(result["write_started"])
        self.spawn.assert_not_called()

    def test_authorization_disables_cancel_and_does_not_interrupt_child(self):
        cancel = threading.Event()

        def event(record):
            if record["phase"] == "authorize":
                self.assertFalse(self.controller.request_cancel(cancel))
                cancel.set()  # Late caller cancellation still cannot kill root work.

        self.assertEqual(self.controller.run_write(self.firmware, cancel, event)["status"], "succeeded")
        self.child.kill.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_auth_denied_nonzero_exit_and_missing_terminal_never_succeed(self):
        for child in (Child(returncode=126), Child(returncode=127), Child(returncode=0),
                      Child([self.event("complete", "succeeded", verified=True, reconnected=True)], returncode=1)):
            self.child = child
            result = self.controller.run_write(self.firmware)
            self.assertEqual(result["status"], "failed")
            if child.returncode in (126, 127):
                self.assertEqual(result["code"], "authorization_denied")

    def test_verified_without_reconnect_and_reconnect_without_verification_fail(self):
        for verified, reconnected in ((True, False), (False, True), (False, False)):
            self.child = Child([self.event("complete", "succeeded", verified=verified, reconnected=reconnected)])
            result = self.controller.run_write(self.firmware)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["code"], "verification_incomplete")

    def test_failure_after_write_preserves_backup_and_requires_recovery(self):
        self.child = Child([self.event("write", backup_complete=True, write_started=True),
                            self.event("failed", "failed", code="verify-mismatch", backup_complete=True,
                                       write_started=True, failed_phase="verify")], returncode=1)
        result = self.controller.run_write(self.firmware)
        self.assertTrue(result["backup_complete"] and result["write_started"])
        self.assertIn("需要恢复", live.event_message(result))
        self.assertEqual(result["failed_phase"], "verify")

    def test_malformed_oversize_extra_terminal_and_other_target_events_fail(self):
        terminal = self.event("complete", "succeeded", verified=True, reconnected=True)
        cases = [Child(raw=b"not json\n"), Child(raw=b"x" * (live.MAX_EVENT_BYTES + 1)),
                 Child([terminal, terminal]), Child([dict(terminal, firmware_id=self.other.id)]),
                 Child([self.event(), dict(terminal, job_id="b" * 32)])]
        for child in cases:
            self.child = child
            with self.subTest(raw=child.stdout.getvalue()[:30]):
                self.assertEqual(self.controller.run_write(self.firmware)["status"], "failed")
                child.kill.assert_not_called()
                child.terminate.assert_not_called()

    def test_pipe_loss_or_wait_timeout_keeps_ongoing_state_and_never_terminates(self):
        for child in (Child(raw=b"broken\n", alive=True), Child(alive=True)):
            self.child = child
            result = self.controller.run_write(self.firmware)
            self.assertTrue(result["uncertain"])
            self.assertIn("维护状态待确认", live.event_message(result))
            child.kill.assert_not_called()
            child.terminate.assert_not_called()

    def test_cache_corruption_cache_failure_and_missing_helper_are_not_write_success(self):
        self.cache._opener = Mock(side_effect=OSError("offline"))
        result = self.controller.run_write(self.firmware)
        self.assertEqual(result["code"], "cache_failed")
        self.spawn.assert_not_called()
        wrong = self.root / "wrong.bin"
        wrong.write_bytes(b"x" * self.firmware.size)
        with patch.object(self.cache, "ensure", return_value=wrong):
            result = self.controller.run_write(self.firmware)
        self.assertEqual(result["code"], "cache_changed")
        self.spawn.assert_not_called()
        self.cache._opener = Mock(side_effect=lambda request, **kwargs: Response(self.bytes, request.full_url))
        self.spawn.side_effect = FileNotFoundError()
        self.assertEqual(self.controller.run_write(self.firmware)["code"], "helper_missing")

    def test_unsupported_target_busy_and_existing_running_record_reject_new_work(self):
        with self.assertRaises(live.LiveError):
            self.controller.run_write(self.other)
        self.controller._lock.acquire()
        try:
            with self.assertRaisesRegex(live.LiveError, "已有维护"):
                self.controller.run_write(self.firmware)
        finally:
            self.controller._lock.release()
        self.controller._status_reader = lambda: ([self.event("write", write_started=True)], (1,))
        self.assertEqual(self.controller.run_write(self.firmware)["code"], "ongoing")
        self.spawn.assert_not_called()
        self.cache._opener.assert_not_called()

    def test_ui_callback_error_cannot_abort_maintenance(self):
        result = self.controller.run_write(self.firmware, on_event=Mock(side_effect=RuntimeError("UI closed")))
        self.assertEqual(result["status"], "succeeded")

    def test_audit_failure_is_visible_without_discarding_actual_verification(self):
        self.child = Child([self.event("complete", "succeeded", verified=True, reconnected=True, audit_degraded=True)])
        result = self.controller.run_write(self.firmware)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["audit_degraded"])
        self.assertIn("维护记录保存不完整", live.event_message(result))

    def test_status_refresh_matches_job_and_never_reuses_old_success(self):
        old = self.event("complete", "succeeded", verified=True, reconnected=True, job_id="b" * 32)
        self.controller.job_id = "a" * 32
        self.controller._status_reader = lambda: ([old], (2,))
        self.assertIsNone(self.controller.pending_record())
        self.controller.job_id = None
        self.controller.previous_jobs = {old["job_id"]}
        self.controller.status_revision = (1,)
        self.assertIsNone(self.controller.pending_record())
        new = self.event("write", write_started=True)
        self.controller._status_reader = lambda: ([new, old], (2,))
        self.assertEqual(self.controller.pending_record(), new)
        self.assertEqual(self.controller.job_id, "a" * 32)

    def test_sanitizer_drops_backend_text_identity_and_unknown_error_codes(self):
        event = self.event("failed", "failed", message="private UART data", serial="unique",
                           code="untrusted backend text", runtime_version_confirmed=True)
        clean = live.sanitize_event(event)
        self.assertNotIn("message", clean)
        self.assertNotIn("serial", clean)
        self.assertFalse(clean["runtime_version_confirmed"])
        self.assertEqual(clean["code"], "helper_failed")
        for fields in ({"progress": float("nan")}, {"progress": 2}, {"progress": True}, {"verified": 1},
                       {"job_id": "device unique id"}, {"version": "untrusted"}):
            with self.subTest(fields=fields), self.assertRaises(live.LiveError):
                live.sanitize_event({**event, **fields})

    def test_root_status_reader_bounds_ownership_links_and_malformed_records(self):
        path = self.root / "status.json"
        self.assertEqual(live.read_status(path, trusted_uid=os.getuid()), ([], None))
        path.write_text(json.dumps({"schema": 1, "records": [self.event()]}))
        path.chmod(0o644)
        records, revision = live.read_status(path, trusted_uid=os.getuid())
        self.assertEqual(records[0]["job_id"], "a" * 32)
        self.assertIsNotNone(revision)
        with self.assertRaises(live.LiveError):
            live.read_status(path, trusted_uid=os.getuid() + 1)
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(live.LiveError):
            live.read_status(link, trusted_uid=os.getuid())
        for payload in ({"schema": 2, "records": []}, {"schema": 1, "records": [self.event()] * 21},
                        {"schema": 1, "records": [{"message": "bad"}]}):
            path.write_text(json.dumps(payload))
            path.chmod(0o644)
            with self.assertRaises(live.LiveError):
                live.read_status(path, trusted_uid=os.getuid())
        path.write_bytes(b"x" * (128 * 1024 + 1))
        with self.assertRaises(live.LiveError):
            live.read_status(path, trusted_uid=os.getuid())


class EntrypointTests(unittest.TestCase):
    def test_default_is_live_and_preview_requires_explicit_flag(self):
        from typix_copilot import __main__ as entry
        live_app, preview_app = Mock(), Mock()
        live_app.return_value.run.return_value = 7
        preview_app.return_value.run.return_value = 9
        modules = {"typix_copilot.live_app": SimpleNamespace(LiveCopilotApplication=live_app),
                   "typix_copilot.app": SimpleNamespace(CopilotApplication=preview_app)}
        with patch.dict(sys.modules, modules):
            with patch.object(sys, "argv", ["typix-copilot", "--fullscreen"]):
                self.assertEqual(entry.main(), 7)
                live_app.assert_called_once_with(fullscreen=True)
                preview_app.assert_not_called()
            with patch.object(sys, "argv", ["typix-copilot", "--preview"]):
                self.assertEqual(entry.main(), 9)
                preview_app.assert_called_once_with(fullscreen=False)


if __name__ == "__main__":
    unittest.main()
