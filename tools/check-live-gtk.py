#!/usr/bin/env python3
"""Exercise native live-mode widgets using only an injected fake controller.

Run on a local development display or isolated compositor. No helper invocation,
network request, serial open or hardware result is produced by these tests.
"""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typix_copilot.live_app import LiveCopilotApplication, Gdk, GLib, Gtk
from typix_copilot.live import WRITABLE_FIRMWARE_ID
from typix_copilot.core import load_catalog
from typix_copilot.registry import RegistryError, parse_catalog
from typix_copilot.cache import Cancelled


def public_catalog():
    return parse_catalog((Path(__file__).resolve().parents[1] / "firmware/index.json").read_bytes())


def diy_firmware():
    return next(fw for fw in public_catalog() if fw.publisher == "自研")


class FakeRegistry:
    def __init__(self, error=False):
        self.error = error

    def cached_catalog(self):
        return public_catalog()

    def fetch_catalog(self, cancel):
        if self.error:
            raise RegistryError("测试网络离线")
        return public_catalog()


class FakeCache:
    def cached(self, firmware):
        return None


class FakeController:
    def __init__(self):
        self.can_cancel = True
        self.busy = False
        self.events = None
        self.firmware = None
        self.job_id = None

    def records(self):
        return []

    def pending_record(self):
        return None

    def event(self, phase="prepare", status="running", **fields):
        event = dict(phase=phase, status=status, firmware_id=self.firmware.id, version=self.firmware.version,
                     progress=.05, backup_complete=False, write_started=False, verified=False,
                     reconnected=False, runtime_version_confirmed=False, job_id="a" * 32)
        event.update(fields)
        return event

    def run_write(self, firmware, cancel, on_event):
        self.firmware, self.events = firmware, on_event
        self.can_cancel = True
        on_event(self.event())

    def request_cancel(self, event):
        if self.can_cancel:
            event.set()
            self.events(self.event("failed", "failed", code="cancelled"))
            return True
        return False


def drain():
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class LiveWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller = FakeController()
        cls.app = LiveCopilotApplication(cache=FakeCache(), controller=cls.controller, registry=False)
        cls.app.register(None)
        cls.app.activate()
        drain()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop_timer()
        cls.app.window.destroy()
        cls.app.quit()

    def setUp(self):
        self.app.stop_timer()
        self.app.set_navigation_locked(False)
        self.app.last_result = None
        self.controller.can_cancel = True
        self.app.registry = False
        self.app._pending_catalog = None
        self.app._downloading = False
        self.app._apply_catalog([])
        self.app.cache = FakeCache()
        self.app.show_detail(WRITABLE_FIRMWARE_ID)

    def test_online_refresh_discovers_diy_and_category_without_writer_permission(self):
        self.app.registry = FakeRegistry()
        self.app.show_page("store")
        self.app.refresh_catalog()
        self.app._registry_worker.join(timeout=2)
        drain()
        self.assertEqual(len(self.app.catalog), len({fw.id for fw in load_catalog() + public_catalog()}))
        self.assertEqual(self.app.catalog_status, "在线目录已更新")
        self.app.change_filter("自研")
        texts = []
        def collect(widget):
            if isinstance(widget, Gtk.Label):
                texts.append(widget.get_text())
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    collect(child)
        collect(self.app.results)
        self.assertIn("TypixDeck DIY", texts)
        self.app.show_detail(diy_firmware().id)
        self.assertTrue(self.app.download_button.get_sensitive())
        self.assertFalse(self.app.write_button.get_sensitive())
        self.app.confirm_write()
        self.assertEqual(self.app.page_name, "detail")

    def test_refresh_failure_retains_cached_versions_and_allows_retry(self):
        self.app._apply_catalog(public_catalog())
        previous = list(self.app.catalog)
        self.app.registry = FakeRegistry(error=True)
        self.app.show_page("store")
        self.app.refresh_catalog()
        self.app._registry_worker.join(timeout=2)
        drain()
        self.assertEqual(self.app.catalog, previous)
        self.assertIn("离线目录", self.app.catalog_label.get_text())
        self.assertTrue(self.app.refresh_button.get_sensitive())

    def test_refresh_during_confirmation_defers_selection_changes(self):
        self.app.registry = FakeRegistry()
        self.app.show_page("store")
        self.app.refresh_catalog()
        self.app.show_detail(WRITABLE_FIRMWARE_ID)
        self.app.confirm_write()
        self.app._registry_worker.join(timeout=2)
        drain()
        self.assertEqual(self.app.catalog, load_catalog())
        self.assertEqual(self.app.selected, WRITABLE_FIRMWARE_ID)
        self.assertIsNotNone(self.app._pending_catalog)
        self.app.show_page("store")
        self.assertIsNone(self.app._pending_catalog)
        self.assertIn(diy_firmware().id, self.app.firmwares)

    def test_catalog_download_caches_without_controller_or_write_success(self):
        class DownloadCache(FakeCache):
            def ensure(self, firmware, cancel, progress):
                progress(firmware.size, firmware.size)
                return Path("/unused/fake-cache.bin")
        self.app.cache = DownloadCache()
        self.app._apply_catalog(public_catalog())
        self.app.show_detail(diy_firmware().id)
        original = self.controller.firmware
        self.app.start_download()
        self.app._worker.join(timeout=2)
        drain()
        self.assertEqual(self.app.operation_title.get_text(), "固件已缓存")
        self.assertFalse(self.app.navigation_locked)
        self.assertIsNone(self.app.last_result)
        self.assertIs(self.controller.firmware, original)
        self.assertEqual(self.app.progress.get_fraction(), 1)
        self.app.operation_button.emit("clicked")
        self.assertFalse(self.app.write_button.get_sensitive())

    def test_catalog_download_can_cancel_and_unlocks(self):
        class DownloadCache(FakeCache):
            def ensure(self, firmware, cancel, progress):
                cancel.wait(2)
                if cancel.is_set():
                    raise Cancelled("已取消下载")
                raise AssertionError("Download did not receive cancellation")
        self.app.cache = DownloadCache()
        self.app._apply_catalog(public_catalog())
        self.app.show_detail(diy_firmware().id)
        self.app.start_download()
        self.assertTrue(self.app.navigation_locked)
        self.assertTrue(self.app.close_window())
        self.app.operation_response()
        self.app._worker.join(timeout=2)
        drain()
        self.assertFalse(self.app.navigation_locked)
        self.assertIn("已取消", self.app.operation_status.get_text())
        self.assertFalse(self.app.progress.get_visible())

    def start(self):
        self.app.confirm_write()
        self.app.confirm_button.emit("clicked")
        self.app._worker.join(2)
        drain()

    def test_live_initialization_and_embedded_flow_use_one_window(self):
        self.assertFalse(hasattr(self.app, "simulation"))
        self.assertEqual(self.app.get_application_id(), "ai.typixdeck.copilot")
        self.assertEqual(self.app.write_button.get_label(), "写入")
        window = self.app.window
        self.app.confirm_write()
        self.assertIsNone(self.app.modal)
        self.assertEqual(self.app.task_view, "confirm")
        self.assertEqual(self.app.confirm_button.get_label(), "确认写入")
        self.app.confirm_back.emit("clicked")
        self.assertEqual(self.app.page_name, "detail")
        self.start()
        self.assertEqual(self.app.page_name, "operation")
        self.app.render_operation(self.controller.event("complete", "succeeded", progress=1., verified=True, reconnected=True))
        self.assertEqual(self.app.operation_title.get_text(), "写入完成")
        self.assertEqual(self.app.operation_status.get_text(), "写入已校验，设备已重新连接")
        self.assertEqual(self.app.operation_warning.get_text(), "运行版本待确认")
        self.assertEqual([w for w in Gtk.Window.list_toplevels() if w.get_visible()], [window])
        self.app.operation_button.emit("clicked")
        self.assertEqual(self.app.page_name, "detail")

    def test_navigation_close_and_keyboard_are_locked_after_authorize(self):
        self.start()
        self.controller.can_cancel = False
        self.app.render_operation(self.controller.event("authorize"))
        self.assertFalse(self.app.operation_button.get_sensitive())
        for page in ("store", "library", "history", "device"):
            self.app.show_page(page)
            self.assertEqual(self.app.page_name, "operation")
        self.app.show_detail(self.app.catalog[1].id)
        self.assertEqual(self.app.selected, WRITABLE_FIRMWARE_ID)
        for key, state in ((Gdk.KEY_Escape, 0), (Gdk.KEY_F11, 0), (Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK)):
            self.assertTrue(self.app.on_key(self.app.window, SimpleNamespace(keyval=key, state=state)))
        self.assertTrue(self.app.close_window())
        self.assertTrue(all(not item.get_sensitive() for item in self.app.nav.values()))
        self.app.operation_response()
        self.assertFalse(self.app.cancel_event.is_set())

    def test_pre_authorization_cancel_unlocks_and_does_not_claim_write(self):
        self.start()
        self.assertTrue(self.app.operation_button.get_sensitive())
        self.app.operation_button.emit("clicked")
        drain()
        self.assertFalse(self.app.navigation_locked)
        self.assertIn("尚未请求写入", self.app.operation_status.get_text())
        self.assertFalse(self.app.last_result["write_started"])

    def test_prewrite_failure_hides_percentage_and_clearly_says_not_started(self):
        self.start()
        self.app.render_operation(self.controller.event("failed", "failed", progress=.11, code="power-state-unverified"))
        self.assertFalse(self.app.progress.get_visible())
        self.assertFalse(self.app.progress.get_show_text())
        self.assertIsNone(self.app.progress.get_text())
        self.app.content.show_all()
        self.assertFalse(self.app.progress.get_visible())
        self.assertEqual(self.app.operation_title.get_text(), "未开始写入")
        self.assertEqual(self.app.notice.get_text(), "未开始写入")
        self.assertFalse(self.app.navigation_locked)

    def test_new_running_task_footer_tracks_current_phase_instead_of_previous_failure(self):
        self.start()
        self.app.render_operation(self.controller.event("failed", "failed", progress=.11, code="power-state-unverified"))
        self.app.operation_button.emit("clicked")
        self.start()
        self.assertEqual(self.app.notice.get_text(), "下载并校验固件")
        self.app.render_operation(self.controller.event("backup", progress=.35))
        self.assertEqual(self.app.notice.get_text(), "备份当前固件")
        self.assertTrue(self.app.progress.get_visible())
        self.assertEqual(self.app.progress.get_text(), "35%")
        self.assertTrue(self.app.navigation_locked)
        self.app.render_operation(self.controller.event("write", progress=.6, write_started=True))
        self.assertEqual(self.app.notice.get_text(), "正在写入")

    def test_failure_after_write_shows_recovery_without_automatic_retry(self):
        self.start()
        self.app.render_operation(self.controller.event("failed", "failed", code="verify-mismatch",
                                                       write_started=True, backup_complete=True))
        self.assertIn("需要恢复", self.app.operation_warning.get_text())
        self.assertNotEqual(self.app.operation_title.get_text(), "写入完成")
        self.assertFalse(hasattr(self.app, "retry_button"))

    def test_verify_failure_at_eighty_percent_hides_progress_and_preserves_written_state(self):
        self.start()
        self.app.render_operation(self.controller.event("failed", "failed", progress=.8,
                                                       failed_phase="verify", code="verify-mismatch",
                                                       write_started=True, backup_complete=True))
        self.app.content.show_all()
        self.assertFalse(self.app.progress.get_visible())
        self.assertFalse(self.app.progress.get_show_text())
        self.assertIsNone(self.app.progress.get_text())
        self.assertEqual(self.app.operation_title.get_text(), "校验未完成")
        self.assertEqual(self.app.notice.get_text(), "校验未完成")
        self.assertTrue(self.app.last_result["write_started"])
        self.assertTrue(self.app.last_result["backup_complete"])
        self.assertIn("需要恢复", self.app.operation_warning.get_text())
        self.assertIn("备份保留在本机", self.app.operation_warning.get_text())
        self.assertNotIn("未开始写入", self.app.operation_warning.get_text())
        self.assertFalse(self.app.navigation_locked)

    def test_unknown_ongoing_status_keeps_lock_and_shows_refresh(self):
        self.start()
        self.app.render_operation(self.controller.event("failed", "failed", code="result_unavailable", uncertain=True))
        self.assertTrue(self.app.navigation_locked)
        self.assertEqual(self.app.operation_title.get_text(), "维护状态待确认")
        self.assertEqual(self.app.operation_button.get_label(), "刷新维护状态")
        self.assertTrue(self.app.close_window())

    def test_historical_firmware_can_download_but_has_disabled_write(self):
        self.app.show_detail(self.app.catalog[1].id)
        self.assertEqual(self.app.write_button.get_label(), "写入")
        self.assertFalse(self.app.write_button.get_sensitive())
        self.assertTrue(self.app.download_button.get_sensitive())
        self.app.confirm_write()
        self.assertEqual(self.app.page_name, "detail")

    def test_incomplete_success_is_not_displayed_as_complete_and_audit_note_survives(self):
        self.start()
        self.app.render_operation(self.controller.event("complete", "succeeded", verified=True, reconnected=False,
                                                       write_started=True, backup_complete=True))
        self.assertEqual(self.app.operation_title.get_text(), "写入未完成")
        self.app.render_operation(self.controller.event("complete", "succeeded", verified=True, reconnected=True, audit_degraded=True))
        self.assertIn("维护记录保存不完整", self.app.operation_status.get_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
