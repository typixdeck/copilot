#!/usr/bin/env python3
"""Exercise the real GTK widgets with deterministic, hardware-free transactions.
Run on a development display or an isolated CM4 compositor, not a busy session.
"""
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from typix_copilot.app import CopilotApplication, Gdk, GLib, Gtk


class EmbeddedTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = CopilotApplication(tick_ms=60000)
        cls.app.register(None)
        cls.app.activate()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop_timer()
        cls.app.window.destroy()
        cls.app.quit()

    def setUp(self):
        self.app.stop_timer()
        self.app.set_navigation_locked(False)
        self.app.simulation.reset()
        self.app.scenario = 'success'
        self.ids = [fw.id for fw in self.app.catalog]
        self.app.show_detail(self.ids[0])

    def start(self, operation='switch'):
        self.app.confirm_write(operation)
        self.app.confirm_button.emit('clicked')
        self.app.stop_timer()

    def finish(self):
        for _ in range(40):
            if self.app.simulation.active['status'] != 'running':
                return
            self.app.operation_tick()
        self.fail('transaction never finished')

    def test_confirmation_progress_result_stay_in_one_window(self):
        window = self.app.window
        self.app.write_button.emit('clicked')
        self.assertEqual(self.app.task_view, 'confirm')
        self.assertIsNone(self.app.modal)
        self.assertEqual([w for w in Gtk.Window.list_toplevels() if w.get_visible()], [window])
        self.app.confirm_back.emit('clicked')
        self.assertEqual(self.app.page_name, 'detail')
        self.assertIsNone(self.app.simulation.active)
        self.start()
        self.assertEqual(self.app.page_name, 'operation')
        self.assertIs(self.app.window, window)
        self.finish()
        self.assertEqual(self.app.task_view, 'result')
        self.assertEqual([w for w in Gtk.Window.list_toplevels() if w.get_visible()], [window])
        self.app.operation_button.emit('clicked')
        self.assertEqual(self.app.page_name, 'detail')

    def test_navigation_shortcuts_and_normal_close_cannot_interrupt_write(self):
        self.start()
        while self.app.simulation.active['phase'] != 'write':
            self.app.operation_tick()
        self.assertFalse(self.app.operation_button.get_sensitive())
        self.assertIn('请勿切换', self.app.operation_warning.get_text())
        self.assertTrue(all(not item.get_sensitive() for item in self.app.nav.values()))
        for page in ('store', 'device', 'library', 'history'):
            self.app.show_page(page)
            self.assertEqual(self.app.page_name, 'operation')
        self.app.show_detail(self.ids[1])
        self.app.prepare_restore(self.ids[1])
        self.app.reset_demo()
        self.app.confirm_write()
        self.app.choose_import()
        self.assertIsNone(self.app.modal)
        for key, state in ((Gdk.KEY_Escape, 0), (Gdk.KEY_F11, 0), (Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK)):
            self.assertTrue(self.app.on_key(self.app.window, SimpleNamespace(keyval=key, state=state)))
        self.assertTrue(self.app.close_window())
        self.app.operation_response()
        self.assertEqual(self.app.simulation.active['status'], 'running')
        self.assertEqual(self.app.simulation.active['id'], self.ids[0])
        self.assertEqual(self.app.selected, self.ids[0])
        self.assertEqual(self.app.page_name, 'operation')
        self.finish()
        self.assertFalse(self.app.navigation_locked)
        self.assertTrue(all(item.get_sensitive() for item in self.app.nav.values()))
        self.app.show_page('library')
        self.assertEqual(self.app.page_name, 'library')

    def test_early_cancel_releases_ui_and_preserves_current(self):
        self.start()
        self.assertTrue(self.app.operation_button.get_sensitive())
        self.app.operation_button.emit('clicked')
        self.assertEqual(self.app.simulation.active['status'], 'cancelled')
        self.assertIsNone(self.app.simulation.current)
        self.assertFalse(self.app.navigation_locked)
        self.assertIsNone(self.app.timer)
        self.app.operation_button.emit('clicked')
        self.assertEqual(self.app.page_name, 'detail')

    def test_failure_releases_ui_and_retry_succeeds(self):
        self.start()
        self.finish()
        self.app.show_detail(self.ids[1])
        self.app.scenario = 'verification-failure'
        self.start()
        self.finish()
        self.assertEqual(self.app.simulation.active['status'], 'failed')
        self.assertEqual(self.app.simulation.current, self.ids[0])
        self.assertFalse(self.app.navigation_locked)
        self.app.scenario = 'success'
        self.app.retry_button.emit('clicked')
        self.app.stop_timer()
        self.assertTrue(self.app.navigation_locked)
        self.finish()
        self.assertEqual(self.app.simulation.current, self.ids[1])
        self.assertEqual(self.app.simulation.active['status'], 'succeeded')

    def test_restore_returns_to_history_with_selected_version(self):
        self.start()
        self.finish()
        self.app.show_detail(self.ids[1])
        self.start()
        self.finish()
        self.app.show_page('history')
        self.app.prepare_restore(self.ids[0])
        self.app.confirm_button.emit('clicked')
        self.app.stop_timer()
        self.finish()
        self.assertEqual(self.app.simulation.current, self.ids[0])
        self.assertEqual(self.app.simulation.history[0]['operation'], 'restore')
        self.app.operation_button.emit('clicked')
        self.assertEqual(self.app.page_name, 'history')

    def test_preview_never_claims_real_chip_write_or_reboot(self):
        self.app.confirm_write()
        self.assertEqual(self.app.confirm_button.get_label(), '预览流程')
        self.app.confirm_button.emit('clicked')
        self.app.stop_timer()
        self.assertEqual(self.app.operation_title.get_text(), '流程预览')
        self.finish()
        self.assertEqual(self.app.operation_title.get_text(), '预览结束')
        self.assertEqual(self.app.operation_status.get_text(), '未写入芯片，未执行重启')
        self.assertIn('均未执行', self.app.operation_warning.get_text())
        self.assertIn('预览', self.app.side_state.get_text())

    def test_uncached_write_downloads_cached_write_skips_download(self):
        self.assertFalse(hasattr(self.app, 'download_button'))
        self.assertEqual(self.app.write_button.get_label(), '写入')
        self.start()
        phases = []
        while self.app.simulation.active['status'] == 'running':
            self.app.operation_tick()
            phases.append(self.app.simulation.active['phase'])
        self.assertIn('download', phases)
        self.assertIn('write', phases)
        self.assertLess(phases.index('download'), phases.index('write'))
        self.assertIn(self.ids[0], self.app.simulation.cached)
        self.app.operation_button.emit('clicked')
        self.start()
        phases = []
        while self.app.simulation.active['status'] == 'running':
            self.app.operation_tick()
            phases.append(self.app.simulation.active['phase'])
        self.assertNotIn('download', phases)
        self.assertIn('write', phases)

    def test_disconnect_preserves_current_and_releases_navigation(self):
        self.app.scenario = 'disconnected'
        self.start()
        self.finish()
        self.assertEqual(self.app.simulation.active['status'], 'failed')
        self.assertIsNone(self.app.simulation.current)
        self.assertFalse(self.app.navigation_locked)



if __name__ == '__main__':
    unittest.main(verbosity=2)
