"""Native GTK3 production interface; never constructs or consumes Simulation."""
from __future__ import annotations

from pathlib import Path
import threading

from .app import CopilotApplication, ChipArt, Gdk, Gio, GLib, Gtk, Pango, add, box, button, label, size_text, styled
from .cache import ArtifactCache, CacheError
from .core import inspect_local, load_catalog
from .registry import FirmwareRegistry, merge_catalog
from . import __version__
from .live import LiveController, LiveError, WRITABLE_FIRMWARE_ID, event_message


class LiveCopilotApplication(CopilotApplication):
    def __init__(self, fullscreen=False, *, cache=None, controller=None, registry=None):
        # Reuse layout/search widgets, without calling the preview initializer.
        Gtk.Application.__init__(self, application_id="ai.typixdeck.copilot", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.catalog = load_catalog()
        self.registry = (FirmwareRegistry(Path.home() / ".cache/typix-copilot/registry")
                         if registry is None else registry)
        self.catalog_status = "内置目录"
        if self.registry:
            try:
                snapshot = self.registry.cached_catalog()
                self.catalog = merge_catalog(snapshot)
                if snapshot:
                    self.catalog_status = "离线目录"
            except CacheError:
                self.catalog_status = "目录缓存不可用 · 使用内置目录"
        self.firmwares = {fw.id: fw for fw in self.catalog}
        self.cache = cache or ArtifactCache(Path.home() / ".cache/typix-copilot/artifacts")
        self.controller = controller or LiveController(self.cache)
        self.want_fullscreen = fullscreen
        self.window = self.modal = self.timer = self.task_view = None
        self.navigation_locked = self.importing = False
        self.inspections = []
        self.local_results = []
        self.operation_return = "detail"
        self.page_name = "store"
        self.selected = self.catalog[0].id
        self.filter_name, self.search_text = "全部", ""
        self.cancel_event = threading.Event()
        self.last_result = None
        self._worker = None
        self._registry_worker = None
        self._registry_cancel = threading.Event()
        self._pending_catalog = None
        self._closed = self._downloading = False

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
        for name, title in (("store", "固件商店"), ("library", "我的固件"), ("device", "协处理器"), ("history", "写入记录")):
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
                self.controller.job_id = records[0].get("job_id")
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
        self.catalog = merge_catalog(remote)
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
        add(actions, button("＋ 导入固件", self.choose_import), True)
        add(actions, button("我的固件 →", lambda *_: self.show_page("library")), True)
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
        if hasattr(self, "download_button"):
            del self.download_button
        self.selected = identifier
        fw = self.firmwares[identifier]
        page = self.make_page("detail")
        head = add(page, box(False, 9))
        add(head, button("←", lambda *_: self.show_page("store"), "flat"))
        add(head, label(fw.title, "title", True), True)
        add(head, label("已缓存" if self._cached(fw) else self.publisher(fw), "pill"))
        row = add(page, box(False, 10))
        add(row, label("选择版本", "muted"))
        self.version_combo = Gtk.ComboBoxText()
        for item in self.catalog:
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
        if identifier == WRITABLE_FIRMWARE_ID:
            add(detail_box, label("写入 [0x000000, 0x3cfe88)\n擦除 [0x000000, 0x3d0000)\nNVS 设置将重置", "small", True))
        details.add(detail_box)
        add(card, details)
        if identifier == WRITABLE_FIRMWARE_ID:
            self.write_button = add(page, button("写入", lambda *_: self.confirm_write(), "primary"))
        else:
            add(page, label("此版本尚未开放写入", "muted"))
            actions = add(page, box(False, 10))
            self.download_button = add(actions, button("下载固件", lambda *_: self.start_download()), True)
            self.write_button = add(actions, button("写入", lambda *_: self.confirm_write(), "primary"), True)
            self.write_button.set_sensitive(False)
        self.content.show_all()

    def build_library(self, page):
        row = add(page, box(False, 10))
        add(row, label("我的固件", "heading"), True)
        add(row, button("导入 .bin", self.choose_import))
        cached = [fw for fw in self.catalog if self._cached(fw)]
        add(page, label(f"已验证缓存 · {len(cached)} 个版本", "small"))
        if not cached:
            add(page, label("暂无缓存", "muted"))
        for fw in cached:
            row = add(page, box(False, 12, "card"))
            info = add(row, box(spacing=6), True)
            add(info, label(f"{fw.title} · {fw.version}", "subheading", True))
            add(info, label(size_text(fw.size) + " · SHA256 已核对", "small"))
            add(row, button("查看", lambda _b, key=fw.id: self.show_detail(key)))
        if self.inspections:
            add(page, label("本地检查", "small"))
        for item in self.inspections:
            card = add(page, box(spacing=8, style="card"))
            add(card, label(item["name"], "subheading", True))
            add(card, label("目录文件已缓存" if item.get("known") else "仅检查 · 未开放写入", "warning"))
            details = Gtk.Expander(label="检查详情")
            info = box(spacing=6)
            add(info, label(item["sha256"], "mono", True))
            for warning in item["warnings"]:
                add(info, label(warning, "small", True))
            details.add(info)
            add(card, details)

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
        add(page, label("写入记录", "heading"))
        try:
            records = self.controller.records()
        except LiveError:
            add(page, label("维护记录无法读取", "warning"))
            records = []
        if not records and not self.local_results:
            add(page, label("暂无写入记录", "muted"))
        for record in records + self.local_results:
            card = add(page, box(spacing=7, style="card"))
            add(card, label(record["version"], "subheading"))
            add(card, label(event_message(record), "small", True))
            if record["status"] == "succeeded":
                add(card, label("运行版本待确认", "small"))

    def confirm_write(self, *_args):
        if self.navigation_locked or self.modal or self.importing:
            return
        if self.selected != WRITABLE_FIRMWARE_ID:
            self.message("此版本尚未开放写入")
            return
        self.operation_return = "detail"
        self.task_view = "confirm"
        page = self.make_page("confirmation")
        add(page, label("确认写入", "heading"))
        card = add(page, box(spacing=16, style="card"))
        add(card, label(self.firmwares[self.selected].title + " · " + self.firmwares[self.selected].version, "title", True))
        add(card, label("完整写入将重置设置", "warning"))
        add(card, label("请连接外部电源。", "muted", True))
        add(card, label("写入过程中请勿切换、拔线或断电。", "warning", True))
        row = add(page, box(False, 12))
        self.confirm_back = add(row, button("返回", lambda *_: self.show_detail(self.selected)), True)
        self.confirm_button = add(row, button("确认写入", lambda *_: self.start_operation(), "primary"), True)
        self.content.show_all()
        self.confirm_back.grab_focus()

    def _operation_page(self):
        self.task_view = "running"
        self.set_navigation_locked(True)
        page = self.make_page("operation")
        self.operation_title = add(page, label("准备写入", "heading"))
        card = add(page, box(spacing=16, style="card"))
        add(card, label(self.firmwares[self.selected].title + " · " + self.firmwares[self.selected].version, "title", True))
        self.progress = add(card, Gtk.ProgressBar())
        self.progress.set_show_text(True)
        self.operation_status = add(card, label("下载并校验固件", "subheading", True))
        self.operation_warning = add(card, label("授权开始后无法取消，请保持供电与连接", "warning", True))
        self.operation_button = add(page, button("取消", self.operation_response))
        self.message(self.operation_status.get_text())
        self.content.show_all()

    def start_operation(self, *_args):
        if self.navigation_locked or self.importing or self.modal:
            return
        fw = self.firmwares[self.selected]
        if fw.id != WRITABLE_FIRMWARE_ID:
            self.message("此固件尚未开放写入")
            return
        self.cancel_event = threading.Event()
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
        if not result.get("job_id"):
            self.local_results.insert(0, dict(result))
            self.local_results = self.local_results[:20]
        self.operation_button.set_sensitive(True)
        self.operation_button.set_label("返回固件")
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
        if self._downloading:
            self.cancel_event.set()
            self.operation_button.set_sensitive(False)
            self.operation_status.set_text("正在取消…")
            return
        if self.last_result and self.last_result.get("uncertain"):
            self._refresh_pending()
            return
        if self.navigation_locked:
            self.controller.request_cancel(self.cancel_event)
            return
        self.show_detail(self.selected)

    def start_download(self):
        if self.navigation_locked or self.importing or self.modal:
            return
        fw = self.firmwares[self.selected]
        if not fw.download_url and fw not in load_catalog():
            return
        self.cancel_event = threading.Event()
        self.last_result = None
        self._operation_page()
        self._downloading = True
        self.operation_title.set_text("下载固件")
        self.operation_warning.set_text("校验后保存在本机；此版本尚未开放写入")

        def update(received, total):
            if not self._closed and self._downloading:
                fraction = received / total if total else 0
                self.progress.set_fraction(fraction)
                self.progress.set_text(f"{int(fraction * 100)}%")
            return GLib.SOURCE_REMOVE

        def done(error):
            self._downloading = False
            if self._closed:
                return GLib.SOURCE_REMOVE
            self.set_navigation_locked(False)
            self.task_view = "result"
            self.progress.set_no_show_all(bool(error))
            self.progress.set_visible(not error)
            self.operation_title.set_text("下载未完成" if error else "固件已缓存")
            self.operation_status.set_text(error or "文件大小、SHA256 和镜像结构已校验")
            self.operation_button.set_sensitive(True)
            self.operation_button.set_label("返回固件")
            self.message(self.operation_title.get_text())
            return GLib.SOURCE_REMOVE

        def worker():
            try:
                self.cache.ensure(fw, self.cancel_event, lambda received, total: GLib.idle_add(update, received, total))
                GLib.idle_add(done, None)
            except CacheError as error:
                GLib.idle_add(done, str(error))
        self._worker = threading.Thread(target=worker, daemon=True, name="copilot-download")
        self._worker.start()

    def import_path(self, path):
        if self.importing or self.modal or self.navigation_locked:
            return
        self.importing = True
        self.message("正在检查本地文件")

        def worker():
            try:
                data = inspect_local(path)
                known = next((fw for fw in self.catalog if fw.sha256 == data["sha256"] and fw.size == data["size"]), None)
                if known:
                    self.cache.import_known(path, known)
                data["known"] = known is not None
                error = None
            except (CacheError, ValueError, OSError):
                data, error = None, "文件无法导入，请检查内容和存储空间"
            GLib.idle_add(done, data, error)

        def done(data, error):
            self.importing = False
            if data is not None:
                self.inspections = [row for row in self.inspections if row["sha256"] != data["sha256"]]
                self.inspections.insert(0, data)
                self.show_page("library")
                self.message("目录文件已缓存" if data["known"] else "文件已检查，未开放写入")
            else:
                self.message(error)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=worker, daemon=True, name="copilot-file-import").start()

    def close_window(self, *_args):
        if self.navigation_locked:
            self.message("请先取消下载" if self._downloading else "维护进行中，请勿退出或断电")
            return True
        self._closed = True
        self._registry_cancel.set()
        self.stop_timer()
        self.quit()
        return False
