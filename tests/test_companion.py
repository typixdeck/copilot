"""CDC service coordination without a real user manager or device."""
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import Mock, patch

from typix_copilot.companion import CompanionLease, CompanionError, UNIT


def result(code, text=b""):
    return SimpleNamespace(returncode=code, stdout=text)


class CompanionLeaseTests(unittest.TestCase):
    def lease(self, replies):
        runner = Mock(side_effect=replies)
        with patch("typix_copilot.companion.pwd.getpwuid", return_value=SimpleNamespace(pw_dir="/home/pi", pw_gid=1000)):
            lease = CompanionLease("1000", runner=runner)
        return lease, runner

    def test_pause_and_restore_only_originally_active_actor_service(self):
        lease, runner = self.lease([result(0, b"active\n"), result(0), result(3, b"inactive\n"), result(0)])
        lease.pause()
        board = SimpleNamespace(probe=lambda: SimpleNamespace(mode="runtime"))
        self.assertTrue(lease.restore(board))
        self.assertEqual([call.args[0] for call in runner.call_args_list],
                         [["/usr/bin/systemctl", "--user", action, UNIT] for action in ("is-active", "stop", "is-active", "start")])
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs["user"], 1000)
            self.assertEqual(call.kwargs["group"], 1000)
            self.assertEqual(call.kwargs["extra_groups"], [])
            self.assertEqual(call.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/1000/bus")
            self.assertLessEqual(call.kwargs["timeout"], 15)
            self.assertNotIn("shell", call.kwargs)

    def test_inactive_and_missing_units_never_start(self):
        for reply in (result(3, b"inactive\n"), result(4, b"unknown\n"), result(3, b"failed\n")):
            with self.subTest(reply=reply):
                lease, runner = self.lease([reply])
                lease.pause()
                board = Mock()
                self.assertTrue(lease.restore(board))
                board.probe.assert_not_called()
                self.assertEqual(runner.call_count, 1)

    def test_stop_timeout_or_failure_cannot_be_treated_as_paused(self):
        for reply in (result(1), subprocess.TimeoutExpired("fixed", 15)):
            lease, _ = self.lease([result(0, b"active\n"), reply])
            with self.assertRaisesRegex(CompanionError, "companion-stop"):
                lease.pause()

    def test_rom_unbound_or_missing_target_defers_resume_without_start(self):
        for probe in (Mock(return_value=SimpleNamespace(mode="rom")), Mock(side_effect=ValueError("private device path"))):
            lease, runner = self.lease([result(0, b"active\n"), result(0), result(3, b"inactive\n")])
            lease.pause()
            self.assertFalse(lease.restore(SimpleNamespace(probe=probe)))
            self.assertEqual(runner.call_count, 3)

    def test_missing_invalid_or_root_actor_never_controls_another_user(self):
        for actor in ("", "0", "-1", "1;reboot", "99999999999", None, "１２３"):
            runner = Mock()
            with patch.dict("os.environ", {}, clear=True):
                lease = CompanionLease(actor, runner)
                lease.pause()
                self.assertTrue(lease.restore(Mock()))
                runner.assert_not_called()

    def test_start_failure_defers_resume(self):
        lease, _ = self.lease([result(0, b"active\n"), result(0), result(3, b"inactive\n"), result(1)])
        lease.pause()
        self.assertFalse(lease.restore(SimpleNamespace(probe=lambda: SimpleNamespace(mode="runtime"))))


class WriterLeaseOrderingTests(unittest.TestCase):
    def test_pause_before_enter_and_restore_after_closed_device_in_success_and_failure(self):
        from test_writer import WriterTests, Transport
        from typix_copilot.writer import execute
        for failure in (None, "backup"):
            test = WriterTests("test_success_requires_backup_write_readback_restart_in_order")
            test.setUp()
            try:
                transport = Transport(test.data, failure)
                board = Mock()
                transport.board = board
                lease = Mock()
                lease.pause.side_effect = lambda: transport.calls.append("pause")
                lease.restore.side_effect = lambda actual: transport.calls.append("restore") or actual is board
                transport.companion = lease
                self.assertEqual(execute(test.fw, test.data, test.journal, transport), int(failure is not None))
                self.assertLess(transport.calls.index("pause"), transport.calls.index("enter"))
                self.assertGreater(transport.calls.index("restore"), transport.calls.index("close"))
            finally:
                test.tearDown()

    def test_pause_failure_cannot_open_the_port_or_write(self):
        from test_writer import WriterTests, Transport
        from typix_copilot.writer import execute
        test = WriterTests("test_success_requires_backup_write_readback_restart_in_order")
        test.setUp()
        try:
            transport = Transport(test.data)
            transport.board = Mock()
            transport.companion = Mock()
            transport.companion.pause.side_effect = CompanionError("companion-stop")
            self.assertEqual(execute(test.fw, test.data, test.journal, transport), 1)
            self.assertEqual(transport.calls, ["close"])
            self.assertEqual(test.events[-1]["code"], "companion-stop")
        finally:
            test.tearDown()
