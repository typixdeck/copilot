"""Native GTK3 production interface; never constructs or consumes Simulation."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime
import threading

from .app import CopilotApplication, ChipArt, Gdk, Gio, GLib, Gtk, Pango, add, box, button, label, size_text, styled
from .cache import ArtifactCache, CacheError
from .authority import bundled_catalog
from .registry import FirmwareRegistry, merge_catalog
from . import __version__
from .live import LiveController, LiveError, event_message
from .diagnostics import DiagnosticError, format_record_log, read_job_log


class LiveCopilotApplication(CopilotApplication):
    def __init__(self, fullscreen=False, *, cache=None, controller=None, registry=None, log_reader=None):
        # Reuse layout/search widgets, without calling the preview initializer.
        Gtk.Application.__init__(self, application_id="ai.typixdeck.copilot", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self._bundled_catalog = bundled_catalog()
        self.catalog = list(self._bundled_catalog)
        self.registry = (FirmwareRegistry(Path.home() / ".cache/typix-copilot/registry")
                         if registry is None else registry)
        self.catalog_status = "内置目录"
        if self.registry:
            try:
                snapshot = self.registry.cached_catalog()
                self.catalog = merge_catalog(snapshot, self._bundled_catalog)
                if snapshot:
                    self.catalog_status = "离线目录"
            except CacheError:
                self.catalog_status = "目录缓存不可用 · 使用内置目录"
        self.firmwares = {fw.id: fw for fw in self.catalog}
        self.cache = cache or ArtifactCache(Path.home() / ".cache/typix-copilot/artifacts")
        self.controller = controller or LiveController(self.cache)
        self.log_reader = read_job_log if log_reader is None else log_reader
        self.want_fullscreen = fullscreen
        self.window = self.modal = self.timer = self.task_view = None
        self.navigation_locked = self.importing = False
        self.local_results = []
        self.operation_return = "detail"
        self.page_name = "store"
        self.selected = self.catalog[0].id
        self.selected_firmware = self.catalog[0]
        self.operation_firmware = None
        self.local_firmwares = []
        self.filter_name, self.search_text = "全部", ""
        self.cancel_event = threading.Event()
        self.last_result = None
        self._worker = None
        self._registry_worker = None
        self._registry_cancel = threading.Event()
        self._pending_catalog = None
        self._closed = False

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        GLib.set_application_name("TypixDeck Copilot")
        GLib.set_prgname("ai.typixdeck.copilot")
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Gtk.ApplicationWindow(application=self, title="Copilot · TypixDeck 板载协处理器")
        self.window.set_default_size(800, 600)
        self.window.set_size_request(800, 600)
        self.window.connect("delete-event", self.close_window)
        self.window.connect("key-press-event", self.on_key)
        root = box(spacing=0)
        self.window.add(root)
        top = add(root, box(False, 12, "topbar"))
        add(top, label("◈", "accent"))
        add(top, label("COPILOT", "brand"))
        add(top, box(), True)
        add(top, label("板载 ESP32-S3", "pill"))
        middle = add(root, box(False, 0), True)
        sidebar = add(middle, box(spacing=5, style="sidebar"))
        sidebar.set_size_request(145, -1)
        self.nav = {}
        for name, title in (("store", "固件商店"), ("library", "本地固件"), ("device", "协处理器"), ("history", "写入记录")):
            item = button(title, lambda _b, p=name: self.show_page(p), "nav")
            item.get_child().set_xalign(0)
            add(sidebar, item)
            self.nav[name] = item
        add(sidebar, box(), True)
        target = add(sidebar, box(spacing=5, style="device-card"))
        add(target, label("TYPIXDECK", "eyebrow"))
        add(target, label("板载 ESP32-S3", "small"))
        self.side_state = add(target, label("运行版本待确认", "small"))
        add(sidebar, label(__version__, "small"))
        self.content = add(middle, box(spacing=0), True)
        footer = add(root, box(False, 10, "footer"))
        self.notice = add(footer, label("已就绪", "small"), True)
        self.notice.set_ellipsize(Pango.EllipsizeMode.END)
        add(footer, label("Esc 返回 · F11 全屏", "small"))
        self.show_page("store")
        try:
            records = self.controller.records()
            if records and records[0]["status"] == "running":
                self.selected = records[0]["firmware_id"]
                self.selected_firmware = self.operation_firmware = None
                self.controller.observe_record(records[0])
                self._operation_page()
                self.render_operation({**records[0], "uncertain": True})
        except LiveError:
            self.message("维护状态无法读取，写入前需重新检查")
        self.window.show_all()
        if self.want_fullscreen:
            self.window.fullscreen()
        self.window.present()
        self.refresh_catalog()

    def show_page(self, name):
        if not self.navigation_locked and not self.importing and self._pending_catalog is not None:
            self._apply_catalog(self._pending_catalog)
            self._pending_catalog = None
        super().show_page(name)

    def _apply_catalog(self, remote):
        self.catalog = merge_catalog(remote, self._bundled_catalog)
        # This map belongs only to the visible online catalog. Historical local
        # rows can share an ID and must retain their own immutable image object.
        self.firmwares = {fw.id: fw for fw in self.catalog}
        if self.selected not in self.firmwares:
            self.selected = self.catalog[0].id

    def build_store(self, page):
        row = add(page, box(False, 10))
        self.catalog_label = add(row, label(self.catalog_status, "small"), True)
        self.refresh_button = add(row, button("刷新目录", lambda *_: self.refresh_catalog()))
        self.refresh_button.set_sensitive(bool(self.registry) and not (
            self._registry_worker and self._registry_worker.is_alive()))
        super().build_store(page)

    def refresh_catalog(self):
        if (not self.registry or self._closed or self.navigation_locked or
                self._registry_worker and self._registry_worker.is_alive()):
            return
        self.catalog_status = "正在更新目录…"
        if self.page_name == "store":
            self.catalog_label.set_text(self.catalog_status)
            self.refresh_button.set_sensitive(False)

        def done(remote, error):
            self._registry_worker = None
            if self._closed:
                return GLib.SOURCE_REMOVE
            self.catalog_status = "在线目录已更新" if remote is not None else "更新失败 · 保留离线目录"
            if remote is not None:
                if self.navigation_locked or self.importing or self.page_name not in {"store", "library"}:
                    self._pending_catalog = remote
                else:
                    self._apply_catalog(remote)
            if self.page_name == "store":
                self.catalog_label.set_text(self.catalog_status)
                self.refresh_button.set_sensitive(True)
                self.render_results()
            if error and not self.navigation_locked:
                self.message(error)
            return GLib.SOURCE_REMOVE

        def worker():
            try:
                remote = self.registry.fetch_catalog(self._registry_cancel)
                GLib.idle_add(done, remote, None)
            except CacheError as error:
                GLib.idle_add(done, None, str(error))
        self._registry_worker = threading.Thread(target=worker, daemon=True, name="copilot-catalog")
        self._registry_worker.start()

    @staticmethod
    def publisher(fw):
        return fw.publisher or ("官方" if not fw.download_url else "第三方")

    def render_results(self):
        for child in self.results.get_children():
            self.results.remove(child)
        query = self.search_text.strip().casefold()
        groups = {}
        for fw in self.catalog:
            publisher = self.publisher(fw)
            if self.filter_name != "全部" and publisher != self.filter_name:
                continue
            if query and query not in f"{publisher} {fw.title} {fw.version} {fw.filename}".casefold():
                continue
            title = fw.title if fw.download_url else "TypixDeck 官方固件"
            groups.setdefault((publisher, title), []).append(fw)
        if not groups:
            add(self.results, label("未找到固件", "muted"))
        for (publisher, title), versions in groups.items():
            fw = versions[0]
            card = add(self.results, box(False, 14, "card"))
            add(card, ChipArt(65, 72))
            info = add(card, box(spacing=7), True)
            add(info, label(title, "subheading", True))
            add(info, label(f"{publisher} · {fw.version} · {len(versions)} 个版本", "small"))
            add(card, button("查看 →", lambda _b, key=fw.id: self.show_detail(key), "primary"))
        actions = add(self.results, box(False, 12))
        add(actions, button("本地固件 →", lambda *_: self.show_page("library")), True)
        self.results.show_all()

    def _cached(self, fw):
        try:
            return self.cache.cached(fw)
        except CacheError:
            self.message("缓存目录不可用")
            return None

    def show_detail(self, identifier):
        if self.navigation_locked:
            return
        self.task_view = None
        self.selected = identifier
        fw = self.firmwares[identifier]
        self.selected_firmware = fw
        page = self.make_page("detail")
        head = add(page, box(False, 9))
        add(head, button("←", lambda *_: self.show_page("store"), "flat"))
        add(head, label(fw.title, "title", True), True)
        add(head, label("已缓存" if self._cached(fw) else self.publisher(fw), "pill"))
        row = add(page, box(False, 10))
        add(row, label("选择版本", "muted"))
        self.version_combo = Gtk.ComboBoxText()
        versions = self.catalog if fw in self.catalog else [fw, *self.catalog]
        for item in versions:
            if self.publisher(item) == self.publisher(fw) and (not fw.download_url or item.title == fw.title):
                self.version_combo.append(item.id, f"{item.version} · {size_text(item.size)}")
        self.version_combo.set_active_id(identifier)
        self.version_combo.connect("changed", lambda combo: self.show_detail(combo.get_active_id()) if combo.get_active_id() else None)
        add(row, self.version_combo, True)
        card = add(page, box(spacing=14, style="card"))
        row = add(card, box(False, 16))
        add(row, ChipArt(80, 90))
        info = add(row, box(spacing=10), True)
        add(info, label("完整固件", "title"))
        add(info, label("TypixDeck · 板载 ESP32-S3", "muted"))
        add(info, label("写入将重置设置" if fw.nvs_reset else "设置影响见版本说明", "warning"))
        details = Gtk.Expander(label="来源与校验详情")
        detail_box = box(spacing=8)
        for text in (fw.summary, fw.layout, fw.filename, "SHA256 " + fw.sha256, fw.source_url):
            add(detail_box, label(text, "small", True))
        if fw.download_url:
            add(detail_box, label("发布者声明已验证" if fw.hardware_verified else "待真机验证", "small"))
            add(detail_box, label("源码提交 " + fw.commit if fw.commit else "本地构建 · 暂无对应源码提交", "small"))
        erase_end = (fw.size + 4095) // 4096 * 4096
        add(detail_box, label(f"写入 [0x000000, 0x{fw.size:06x})\n擦除 [0x000000, 0x{erase_end:06x})", "small", True))
        details.add(detail_box)
        add(card, details)
        self.write_button = add(page, button("写入", lambda *_: self.confirm_write(), "primary"))
        self.content.show_all()

    def build_library(self, page):
        add(page, label("本地固件", "heading"))
        self.cache_write_buttons, self.cache_remove_buttons = {}, {}
        try:
            cached = self.cache.list_cached(self.catalog)
        except CacheError as error:
            add(page, label(str(error), "warning", True))
            return
        self.local_firmwares = list(cached)
        add(page, label(f"已验证缓存 · {len(cached)} 个版本", "small"))
        if not cached:
            add(page, label("暂无缓存", "muted"))
        for fw in cached:
            row = add(page, box(False, 12, "card"))
            info = add(row, box(spacing=6), True)
            add(info, label(f"{fw.title} · {fw.version}", "subheading", True))
            add(info, label(size_text(fw.size) + " · SHA256 已核对", "small"))
            key = fw
            self.cache_write_buttons[key] = add(row, button("写入", lambda _b, item=fw: self.prepare_cached_write(item), "primary"))
            self.cache_remove_buttons[key] = add(row, button("移除缓存", lambda _b, item=fw: self.remove_cached(item)))

    def prepare_cached_write(self, firmware):
        if self.navigation_locked or self.importing or self.modal:
            return
        self.selected = firmware.id
        self.selected_firmware = firmware
        self.confirm_write()

    def remove_cached(self, firmware):
        if self.navigation_locked or self.importing or self.modal:
            return
        try:
            removed = self.cache.remove(firmware)
        except CacheError as error:
            self.message(str(error))
            return
        self.show_page("library")
        self.message("缓存已移除" if removed else "此缓存已不存在")

    def build_device(self, page):
        add(page, label("板载协处理器", "heading"))
        from .device import passive_status
        state = passive_status()
        card = add(page, box(False, 18, "featured"))
        add(card, ChipArt(110, 120))
        info = add(card, box(spacing=10), True)
        add(info, label("ESP32-S3", "title"))
        text = "未发现已绑定设备"
        if state["connected"]:
            text = "维护模式" if state["mode"] == "rom" else "板载设备已连接"
        add(info, label(text, "accent"))
        add(info, label("运行版本待确认", "small"))
        try:
            records = self.controller.records()
            if records:
                latest = records[0]
                add(page, label("最近任务 · " + latest["version"], "small"))
                add(page, label(event_message(latest), "warning" if latest["status"] != "succeeded" else "accent", True))
        except LiveError:
            add(page, label("维护状态无法读取", "warning"))
        add(page, button("刷新", lambda *_: self.show_page("device")))

    def build_history(self, page):
        heading = add(page, box(False, 12))
        add(heading, label("写入记录", "heading"), True)
        add(heading, button("刷新", lambda *_: self.show_page("history")))
        try:
            records = self.controller.records()
        except LiveError:
            add(page, label("维护记录无法读取", "warning"))
            records = []
        combined, seen = [], set()
        for record in records + self.local_results:
            key = self._history_key(record)
            if key not in seen:
                seen.add(key)
                combined.append(record)
        self.history_rows = []
        if not combined:
            add(page, label("暂无写入记录", "muted"))
        for record in combined:
            card = add(page, box(spacing=7, style="card"))
            candidates = [*self.catalog, *self.local_firmwares]
            if self.operation_firmware:
                candidates.append(self.operation_firmware)
            firmware = next((fw for fw in candidates if
                            (fw.id, fw.version, fw.sha256, fw.size) == tuple(record.get(key) for key in
                             ("firmware_id", "version", "image_sha256", "image_size"))), None)
            title = add(card, label(
                f"{firmware.title} · {firmware.version}" if firmware else f"固件 · {record['version']}",
                "subheading", True))
            status = {"running": "进行中", "failed": "未完成", "succeeded": "已完成"}[record["status"]]
            add(card, label(f"{self._history_date(record)} · {status}", "small"))
            add(card, label(event_message(record), "small", True))
            if record["status"] == "succeeded":
                add(card, label("运行版本待确认", "small"))
            expander = add(card, Gtk.Expander(label="详细日志"))
            expander.set_can_focus(True)
            detail = box(spacing=8)
            expander.add(detail)
            actions = add(detail, box(False, 12))
            add(actions, label("仅保存在本机", "small"), True)
            copy = add(actions, Gtk.Button(label="复制日志"))
            scroll = add(detail, Gtk.ScrolledWindow())
            scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroll.set_size_request(-1, 240)
            view = Gtk.TextView()
            view.set_editable(False)
            view.set_cursor_visible(True)
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.set_left_margin(10)
            view.set_right_margin(10)
            view.set_top_margin(10)
            view.set_bottom_margin(10)
            scroll.add(view)
            row = dict(record=dict(record), title=title, expander=expander, view=view, copy=copy, text=None)
            self.history_rows.append(row)
            copy.connect("clicked", lambda _button, item=row: self._copy_history_log(item))
            expander.connect("notify::expanded", lambda widget, _property, item=row:
                             self._load_history_log(item) if widget.get_expanded() else None)

    @staticmethod
    def _history_key(record):
        identity = tuple(record.get(key) for key in ("firmware_id", "version", "image_sha256", "image_size"))
        # A root task and its last live event are one attempt. Different images
        # or versions never inherit one another's title or diagnostic details.
        if record.get("job_id"):
            return (record["job_id"], *identity)
        return (None, *identity, record.get("timestamp"), record.get("phase"),
                record.get("status"), record.get("code"))

    @staticmethod
    def _history_date(record):
        timestamp = record.get("timestamp")
        if type(timestamp) is int:
            try:
                return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                pass
        return "时间未记录"

    def _load_history_log(self, row):
        if row["text"] is not None:
            return
        try:
            details = self.log_reader(row["record"])
        except DiagnosticError:
            text = "详细日志暂不可读取；以下为已保存的任务摘要。\n\n" + format_record_log(row["record"])
        else:
            text = format_record_log(row["record"], details)
        row["text"] = text
        row["view"].get_buffer().set_text(text)

    def _copy_history_log(self, row):
        self._load_history_log(row)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(row["text"], -1)
        self.message("日志已复制")

    def show_result_log(self, *_args):
        if self.navigation_locked:
            return
        key = self._history_key(self.last_result) if self.last_result else None
        self.show_page("history")
        for row in self.history_rows:
            if self._history_key(row["record"]) == key:
                row["expander"].set_expanded(True)
                row["expander"].grab_focus()
                break

    def confirm_write(self, *_args):
        if self.navigation_locked or self.modal or self.importing:
            return
        fw = self.selected_firmware
        if fw is None:
            return
        # Bind confirmation and execution to these exact signed bytes/metadata,
        # independent of later catalog updates or another row with the same ID.
        self.operation_firmware = fw
        self.operation_return = "library" if self.page_name == "library" else "detail"
        self.task_view = "confirm"
        page = self.make_page("confirmation")
        add(page, label("确认写入", "heading"))
        card = add(page, box(spacing=16, style="card"))
        add(card, label(f"{fw.title} · {fw.version}", "title", True))
        add(card, label("完整写入将重置设置" if fw.nvs_reset else "设置影响见版本说明", "warning"))
        add(card, label("请连接外部电源。", "muted", True))
        add(card, label("将自动使用本地缓存；没有缓存时下载并校验。", "small", True))
        add(card, label("写入过程中请勿切换、拔线或断电。", "warning", True))
        row = add(page, box(False, 12))
        self.confirm_back = add(row, button("返回", lambda *_: self.return_to_firmware()), True)
        self.confirm_button = add(row, button("确认写入", lambda *_: self.start_operation(), "primary"), True)
        self.content.show_all()
        self.confirm_back.grab_focus()

    def _operation_page(self):
        self.task_view = "running"
        self.set_navigation_locked(True)
        page = self.make_page("operation")
        self.operation_title = add(page, label("准备写入", "heading"))
        card = add(page, box(spacing=16, style="card"))
        fw = self.operation_firmware
        self.operation_firmware_label = add(card, label(
            f"{fw.title} · {fw.version}" if fw else "历史固件写入任务", "title", True))
        self.progress = add(card, Gtk.ProgressBar())
        self.progress.set_show_text(True)
        self.operation_status = add(card, label("准备并校验固件", "subheading", True))
        self.operation_warning = add(card, label("授权开始后无法取消；请勿切换、拔线或断电", "warning", True))
        actions = add(page, box(False, 12))
        self.operation_button = add(actions, button("取消", self.operation_response), True)
        self.operation_log_button = add(actions, button("查看日志", self.show_result_log), True)
        self.operation_log_button.set_no_show_all(True)
        self.operation_log_button.hide()
        self.message(self.operation_status.get_text())
        self.content.show_all()

    def start_operation(self, *_args):
        if self.navigation_locked or self.importing or self.modal:
            return
        fw = self.operation_firmware
        if fw is None:
            return
        self.selected = fw.id
        self.cancel_event = threading.Event()
        self.last_result = None
        self._operation_page()

        def worker():
            try:
                self.controller.run_write(fw, self.cancel_event, lambda event: GLib.idle_add(self.render_operation, event))
            except LiveError as error:
                result = self.controller._base(fw, "failed", "failed", code=error.code)
                GLib.idle_add(self.render_operation, result)
        self._worker = threading.Thread(target=worker, daemon=True, name="copilot-write-client")
        self._worker.start()

    def render_operation(self, result):
        candidates = [*self.catalog, *self.local_firmwares]
        if self.operation_firmware is not None:
            candidates.append(self.operation_firmware)
        actual = next((fw for fw in candidates
                       if (fw.id, fw.version, fw.sha256, fw.size) == (
                           result.get("firmware_id"), result.get("version"),
                           result.get("image_sha256"), result.get("image_size"))), None)
        # An already-running root task can differ from the newly requested
        # firmware. Never reuse the requested version as that task's identity.
        self.operation_firmware_label.set_text(
            f"{actual.title} · {actual.version}" if actual else f"固件 · {result['version']}")
        self.selected = result["firmware_id"]
        if result["status"] == "succeeded" and not (result.get("verified") and result.get("reconnected")):
            result = {**result, "phase": "failed", "status": "failed", "code": "verification_incomplete"}
        self.last_result = dict(result)
        failed_terminal = result["status"] == "failed" and not result.get("uncertain")
        stopped_before_write = failed_terminal and not result.get("write_started")
        self.progress.set_no_show_all(failed_terminal)
        self.progress.set_visible(not failed_terminal)
        self.progress.set_show_text(not failed_terminal)
        self.progress.set_fraction(0. if failed_terminal else result["progress"])
        self.progress.set_text(None if failed_terminal else f"{int(result['progress'] * 100)}%")
        self.operation_status.set_text(event_message(result))
        self.message(self.operation_status.get_text())
        if result.get('exception_used'):
            self.operation_warning.set_text("首次升级 · 供电状态未验证，请勿切换或断电")
        if result.get("uncertain"):
            self.operation_title.set_text("维护状态待确认")
            self.operation_button.set_label("刷新维护状态")
            self.operation_button.set_sensitive(True)
            self.set_navigation_locked(True)
            self._schedule_status()
            return GLib.SOURCE_REMOVE
        if result["status"] == "running":
            cancellable = result["phase"] == "prepare" and self.controller.can_cancel
            self.operation_button.set_sensitive(cancellable)
            self.operation_button.set_label("取消" if cancellable else "维护中，请稍候")
            self.operation_title.set_text("正在写入")
            return GLib.SOURCE_REMOVE
        self.task_view = "result"
        self.set_navigation_locked(False)
        self.stop_timer()
        succeeded = result["status"] == "succeeded"
        title = "写入完成" if succeeded else "未开始写入" if stopped_before_write else (
            "校验未完成" if result.get("failed_phase") == "verify" else "写入未完成")
        self.operation_title.set_text(title)
        recovery_text = "需要恢复；备份保留在本机" if result.get("backup_complete") else "需要恢复；请检查本机维护记录"
        self.operation_warning.set_text("运行版本待确认" if succeeded else (
            recovery_text if result.get("write_started") else "未开始写入；请检查原因后重新确认"))
        if result.get('exception_used'):
            self.operation_warning.set_text(self.operation_warning.get_text() + " · 本次供电寄存器未验证")
        key = self._history_key(result)
        self.local_results = [dict(result)] + [row for row in self.local_results
                                               if self._history_key(row) != key][:19]
        self.operation_button.set_sensitive(True)
        self.operation_button.set_label("返回固件")
        self.operation_log_button.set_no_show_all(False)
        self.operation_log_button.show()
        self.message(self.operation_title.get_text())
        return GLib.SOURCE_REMOVE

    def _schedule_status(self):
        if self.timer is None:
            self.timer = GLib.timeout_add(2000, self._refresh_pending)

    def _refresh_pending(self):
        try:
            record = self.controller.pending_record()
            if record is not None:
                if record["status"] == "running":
                    self.progress.set_fraction(record["progress"])
                    self.progress.set_text(f"{int(record['progress'] * 100)}%")
                    self.message(event_message(record))
                else:
                    self.timer = None
                    self.render_operation(record)
                    return GLib.SOURCE_REMOVE
        except LiveError:
            self.message("维护状态暂不可读取，请勿断电")
        return GLib.SOURCE_CONTINUE

    def operation_response(self, *_args):
        if self.last_result and self.last_result.get("uncertain"):
            self._refresh_pending()
            return
        if self.navigation_locked:
            self.controller.request_cancel(self.cancel_event)
            return
        self.return_to_firmware()

    def return_to_firmware(self):
        if self.operation_return == "library" or self.selected not in self.firmwares:
            self.show_page("library")
        else:
            self.show_detail(self.selected)

    def close_window(self, *_args):
        if self.navigation_locked:
            self.message("维护进行中，请勿切换、退出或断电")
            return True
        self._closed = True
        self._registry_cancel.set()
        self.stop_timer()
        self.quit()
        return False
