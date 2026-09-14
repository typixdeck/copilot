#!/usr/bin/env python3
"""Exercise native widgets with fake writes and injected read-only status records.

Run on a local development display or isolated compositor. No helper invocation,
network request, serial open or hardware result is produced by these tests.
"""
from dataclasses import asdict, replace
from pathlib import Path
import sys
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typix_copilot.live_app import LiveCopilotApplication, Gdk, GLib, Gtk
from typix_copilot.live import LiveController, LiveError
from typix_copilot import authority
from typix_copilot.authority import bundled_catalog

DEFAULT_FIRMWARE_ID = "official-20260910"
from typix_copilot.registry import RegistryError, parse_catalog
from typix_copilot.cache import CacheError


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
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.removed = []

    def cached(self, firmware):
        return Path("/unused/fake-cache.bin") if firmware in self.rows else None

    def list_cached(self, known_catalog=None):
        return list(self.rows)

    def remove(self, firmware):
        self.removed.append(firmware)
        if firmware not in self.rows:
            return False
        self.rows.remove(firmware)
        return True


class FakeController:
    def __init__(self):
        self.can_cancel = True
        self.busy = False
        self.events = None
        self.firmware = None
        self.job_id = None
        self.observed_record = None

    def records(self):
        return []

    def pending_record(self):
        return None

    def observe_record(self, record):
        self.observed_record = dict(record)
        self.job_id = record["job_id"]
        self.firmware = None

    def event(self, phase="prepare", status="running", **fields):
        event = dict(phase=phase, status=status, firmware_id=self.firmware.id, version=self.firmware.version,
                     image_sha256=self.firmware.sha256, image_size=self.firmware.size,
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
        self.app._apply_catalog([])
        self.app.cache = FakeCache()
        self.app.controller = self.controller
        self.app.show_detail(DEFAULT_FIRMWARE_ID)

    def test_online_refresh_discovers_diy_with_one_write_action(self):
        self.app.registry = FakeRegistry()
        self.app.show_page("store")
        self.app.refresh_catalog()
        self.app._registry_worker.join(timeout=2)
        drain()
        self.assertEqual(len(self.app.catalog), len({fw.id for fw in bundled_catalog() + public_catalog()}))
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
        self.assertFalse(hasattr(self.app, "download_button"))
        self.assertTrue(self.app.write_button.get_sensitive())
        self.app.write_button.emit("clicked")
        self.assertEqual(self.app.page_name, "confirmation")

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
        self.app.show_detail(DEFAULT_FIRMWARE_ID)
        self.app.confirm_write()
        self.app._registry_worker.join(timeout=2)
        drain()
        self.assertEqual(self.app.catalog, bundled_catalog())
        self.assertEqual(self.app.selected, DEFAULT_FIRMWARE_ID)
        self.assertIsNotNone(self.app._pending_catalog)
        self.app.show_page("store")
        self.assertIsNone(self.app._pending_catalog)
        self.assertIn(diy_firmware().id, self.app.firmwares)

    def test_every_catalog_version_enters_same_write_controller(self):
        self.app._apply_catalog(public_catalog())
        self.assertEqual(len(self.app.catalog), 6)
        for firmware in self.app.catalog:
            with self.subTest(firmware=firmware.id):
                self.app.show_detail(firmware.id)
                self.assertTrue(self.app.write_button.get_sensitive())
                self.assertFalse(hasattr(self.app, "download_button"))
                self.start()
                self.assertEqual(self.controller.firmware, firmware)
                self.assertTrue(self.app.navigation_locked)
                self.app.operation_button.emit("clicked")
                drain()
                self.assertFalse(self.app.navigation_locked)
                self.assertFalse(self.app.last_result["write_started"])
                self.assertIn("尚未请求写入", self.app.operation_status.get_text())

    def test_local_cache_lists_old_versions_and_writes_then_returns_to_library(self):
        old = replace(diy_firmware(), id="typixdeck-diy-older-cache", version="0.1.0")
        self.app.cache = FakeCache([old, diy_firmware()])
        self.app.show_page("library")
        self.assertNotIn(old, self.app.catalog)
        self.assertIn(old, self.app.cache_write_buttons)
        self.assertEqual(self.app.nav["library"].get_label(), "本地固件")
        self.assertFalse(hasattr(self.app, "import_button"))
        self.app.cache_write_buttons[old].emit("clicked")
        self.assertEqual(self.app.page_name, "confirmation")
        self.app.confirm_back.emit("clicked")
        self.assertEqual(self.app.page_name, "library")
        self.app.cache_write_buttons[old].emit("clicked")
        self.app.confirm_button.emit("clicked")
        self.app._worker.join(2)
        drain()
        self.assertEqual(self.controller.firmware, old)
        self.app.render_operation(self.controller.event("complete", "succeeded", verified=True, reconnected=True))
        self.app.operation_button.emit("clicked")
        self.assertEqual(self.app.page_name, "library")
        self.app.cache_remove_buttons[old].emit("clicked")
        self.assertEqual(self.app.cache.rows, [diy_firmware()])
        self.assertNotIn(old, self.app.cache_write_buttons)
        self.assertEqual(self.app.notice.get_text(), "缓存已移除")

    def test_cache_remove_is_locked_during_write_and_reports_storage_failure(self):
        firmware = diy_firmware()
        cache = FakeCache([firmware])
        self.app.cache = cache
        self.app.show_page("library")
        self.app.cache_write_buttons[firmware].emit("clicked")
        self.app.confirm_button.emit("clicked")
        self.app._worker.join(2)
        drain()
        self.app.remove_cached(firmware)
        self.assertEqual(cache.removed, [])
        self.assertEqual(cache.rows, [firmware])
        self.app.operation_button.emit("clicked")
        drain()
        self.app.operation_button.emit("clicked")
        def denied(_firmware):
            raise CacheError("缓存目录不可写")
        cache.remove = denied
        self.app.cache_remove_buttons[firmware].emit("clicked")
        self.assertEqual(cache.rows, [firmware])
        self.assertIn("不可写", self.app.notice.get_text())
        self.assertFalse(self.app.navigation_locked)

    def test_same_id_signed_cache_rows_write_and_remove_the_exact_visible_image(self):
        key = Ed25519PrivateKey.generate()
        sources = [fw for fw in public_catalog() if fw.publisher == "自研"]
        old_proofs = authority._proofs.copy()
        try:
            with patch.object(authority, "_public_key", return_value=key.public_key()):
                versions = []
                for source, version in zip(sources, ("2.0.0", "1.0.0")):
                    raw = json.dumps({"schema": 1, "firmwares": [asdict(replace(
                        source, id="community-current", version=version))]}).encode()
                    versions.append(authority.verify_catalog(raw, key.sign(raw))[0])
                newest, older = versions
                self.assertEqual(newest.id, older.id)
                self.assertNotEqual(newest.sha256, older.sha256)

                class SignedRequestController(FakeController):
                    def run_write(self, firmware, cancel, on_event):
                        authority.authorize_firmware(firmware)
                        return super().run_write(firmware, cancel, on_event)
                controller = SignedRequestController()
                self.app.controller = controller
                self.app.cache = FakeCache(versions)
                self.app._apply_catalog([newest])
                self.app.show_page("library")
                self.assertEqual(len(self.app.cache_write_buttons), 2)
                self.assertEqual(self.app.firmwares[newest.id], newest)

                for fw in versions:
                    self.app.cache_write_buttons[fw].emit("clicked")
                    self.assertEqual(self.app.operation_firmware, fw)
                    # A new signed catalog must not mutate an open confirmation.
                    self.app._apply_catalog([older if fw == newest else newest])
                    self.app.confirm_button.emit("clicked")
                    self.app._worker.join(2)
                    drain()
                    self.assertEqual(controller.firmware, fw)
                    self.assertEqual(self.app.last_result["image_sha256"], fw.sha256)
                    self.assertEqual(self.app.operation_firmware_label.get_text(), f"{fw.title} · {fw.version}")
                    self.app.operation_button.emit("clicked")
                    drain()
                    self.app.operation_button.emit("clicked")
                    self.assertEqual(self.app.page_name, "library")

                self.app._apply_catalog([newest])
                self.app.show_page("library")
                self.app.cache_remove_buttons[newest].emit("clicked")
                self.assertEqual(self.app.cache.removed, [newest])
                self.assertEqual(self.app.cache.rows, [older])
                self.app.show_detail(newest.id)
                self.assertEqual(self.app.selected_firmware, newest)
                self.app.confirm_write()
                self.assertEqual(self.app.operation_firmware, newest)
        finally:
            authority._proofs.clear()
            authority._proofs.update(old_proofs)

    def test_cache_listing_failure_leaves_no_stale_actions(self):
        self.app.cache = FakeCache([diy_firmware()])
        self.app.show_page("library")
        self.assertTrue(self.app.cache_remove_buttons)
        def unavailable(_known_catalog):
            raise CacheError("缓存目录不可读取")
        self.app.cache.list_cached = unavailable
        self.app.show_page("library")
        self.assertEqual(self.app.cache_write_buttons, {})
        self.assertEqual(self.app.cache_remove_buttons, {})
        self.assertFalse(self.app.navigation_locked)

    def test_diy_prewrite_error_unlocks_same_embedded_transaction(self):
        class FailedController(FakeController):
            def run_write(self, firmware, cancel, on_event):
                self.firmware = firmware
                raise LiveError("cache_failed")

            def _base(self, firmware, phase, status, **fields):
                return self.event(phase, status, **fields)
        controller = FailedController()
        self.app.controller = controller
        self.app.show_detail(diy_firmware().id)
        self.start()
        self.assertEqual(controller.firmware, diy_firmware())
        self.assertEqual(self.app.operation_title.get_text(), "未开始写入")
        self.assertFalse(self.app.navigation_locked)
        self.assertFalse(self.app.progress.get_visible())
        self.app.operation_button.emit("clicked")
        self.assertTrue(self.app.write_button.get_sensitive())

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
        self.assertEqual(self.app.selected, DEFAULT_FIRMWARE_ID)
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
        self.assertEqual(self.app.notice.get_text(), "准备并校验固件")
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

    def test_existing_root_task_label_uses_actual_identity_not_requested_version(self):
        requested = diy_firmware()
        existing = next(fw for fw in bundled_catalog() if fw.id == DEFAULT_FIRMWARE_ID)
        self.app.show_detail(requested.id)
        self.app.confirm_write()
        self.app._operation_page()
        self.app.render_operation(dict(firmware_id=existing.id, version=existing.version,
            image_sha256=existing.sha256, image_size=existing.size, phase="failed", status="failed",
            code="ongoing", uncertain=True, progress=.4, job_id="a" * 32))
        self.assertEqual(self.app.operation_firmware_label.get_text(), f"{existing.title} · {existing.version}")
        self.assertEqual(self.app.selected, existing.id)
        self.assertTrue(self.app.navigation_locked)
        # An older root record can reuse a catalog ID; absent exact identity we
        # show its recorded version without borrowing the current item's title.
        self.app.render_operation(dict(firmware_id=requested.id, version="0.0.1-retired",
            image_sha256="f" * 64, image_size=123456, phase="failed", status="failed",
            code="ongoing", uncertain=True, progress=.4, job_id="a" * 32))
        self.assertEqual(self.app.operation_firmware_label.get_text(), "固件 · 0.0.1-retired")
        self.assertTrue(self.app.navigation_locked)

    def test_reopening_historical_unknown_job_tracks_identity_until_terminal(self):
        running = dict(firmware_id="retired-community-0.1", version="0.1-retired",
                       image_sha256="c" * 64, image_size=102400,
                       job_id="b" * 32, phase="verify", status="running", progress=.8,
                       write_started=True, backup_complete=True,
                       verified=False, reconnected=False)
        records = [running]
        forbidden_helper = Mock(side_effect=AssertionError("Reopening must only observe"))
        observer = LiveController(self.app.cache, popen=forbidden_helper,
                                  status_reader=lambda: (list(records), None))
        self.app.controller = observer
        self.assertNotIn(running["firmware_id"], self.app.firmwares)
        self.app.window.destroy()
        self.app.window = None
        self.app.do_activate()
        drain()
        self.app.stop_timer()
        self.assertEqual(observer.job_id, running["job_id"])
        self.assertEqual(observer.observed_identity, {key: running[key] for key in
                         ("firmware_id", "version", "image_sha256", "image_size")})
        self.assertEqual(self.app.selected, running["firmware_id"])
        self.assertEqual(self.app.page_name, "operation")
        self.assertTrue(self.app.navigation_locked)
        self.assertEqual(self.app.operation_title.get_text(), "维护状态待确认")
        self.assertEqual(self.app.operation_firmware_label.get_text(), "固件 · 0.1-retired")
        self.assertTrue(all(not item.get_sensitive() for item in self.app.nav.values()))

        terminal = {**running, "phase": "complete", "status": "succeeded", "progress": 1.,
                    "verified": True, "reconnected": True}
        for changed in ({"job_id": "d" * 32}, {"image_sha256": "e" * 64},
                        {"firmware_id": DEFAULT_FIRMWARE_ID}, {"version": "different"}):
            records[:] = [{**terminal, **changed}]
            self.app._refresh_pending()
            self.assertTrue(self.app.navigation_locked)
            self.assertEqual(self.app.last_result["firmware_id"], running["firmware_id"])
            self.assertEqual(self.app.operation_title.get_text(), "维护状态待确认")

        records[:] = [terminal]
        self.app._refresh_pending()
        self.assertFalse(self.app.navigation_locked)
        self.assertEqual(self.app.operation_title.get_text(), "写入完成")
        self.assertEqual(self.app.last_result["image_sha256"], running["image_sha256"])
        self.app.operation_button.emit("clicked")
        self.assertEqual(self.app.page_name, "library")
        self.assertEqual([w for w in Gtk.Window.list_toplevels() if w.get_visible()], [self.app.window])
        forbidden_helper.assert_not_called()

    def test_incomplete_success_is_not_displayed_as_complete_and_audit_note_survives(self):
        self.start()
        self.app.render_operation(self.controller.event("complete", "succeeded", verified=True, reconnected=False,
                                                       write_started=True, backup_complete=True))
        self.assertEqual(self.app.operation_title.get_text(), "写入未完成")
        self.app.render_operation(self.controller.event("complete", "succeeded", verified=True, reconnected=True, audit_degraded=True))
        self.assertIn("维护记录保存不完整", self.app.operation_status.get_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
