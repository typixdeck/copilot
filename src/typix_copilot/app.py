"""GTK3 preview UI. All device state and download/write operations are simulated."""
from __future__ import annotations

from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .core import Simulation
from .authority import bundled_catalog


def styled(widget, *classes):
    for name in classes:
        widget.get_style_context().add_class(name)
    return widget


def label(text="", style=None, wrap=False):
    item = Gtk.Label(label=text, xalign=0)
    if style:
        styled(item, style)
    if wrap:
        item.set_line_wrap(True)
        item.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        item.set_max_width_chars(52)
    return item


def box(vertical=True, spacing=10, style=None):
    item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    if style:
        styled(item, style)
    return item


def add(parent, child, expand=False):
    parent.pack_start(child, expand, expand, 0)
    return child


def button(text, callback, style=None):
    item = Gtk.Button(label=text)
    if style:
        styled(item, style)
    item.connect("clicked", callback)
    return item


def size_text(size):
    return f"{size / 1048576:.2f} MiB"


class ChipArt(Gtk.DrawingArea):
    """Small original vector illustration, drawn natively with Cairo."""
    def __init__(self, width=104, height=106):
        super().__init__()
        self.set_size_request(width, height)
        self.connect("draw", self.draw_chip)

    def draw_chip(self, widget, ctx):
        a = self.get_allocation()
        ctx.translate(a.width / 2, a.height / 2)
        ctx.set_line_width(1.2)
        for offset in (-20, -10, 0, 10, 20):
            ctx.set_source_rgba(.40, .84, .76, .45)
            for direction in (-1, 1):
                ctx.move_to(offset, direction * 29)
                ctx.line_to(offset, direction * 42)
                ctx.move_to(direction * 29, offset)
                ctx.line_to(direction * 42, offset)
                ctx.stroke()
        ctx.set_source_rgb(.08, .25, .26)
        ctx.rectangle(-30, -30, 60, 60)
        ctx.fill_preserve()
        ctx.set_source_rgb(.48, .88, .80)
        ctx.stroke()
        ctx.set_source_rgb(.70, .97, .90)
        ctx.select_font_face("sans-serif", 0, 1)
        ctx.set_font_size(17)
        ctx.move_to(-12, 2)
        ctx.show_text("S3")
        ctx.set_font_size(7)
        ctx.move_to(-20, 17)
        ctx.show_text("COPROCESSOR")
        ctx.arc(-23, -23, 2, 0, 6.283)
        ctx.fill()
        return False


class CopilotApplication(Gtk.Application):
    def __init__(self, fullscreen=False, tick_ms=500):
        super().__init__(application_id="ai.typixdeck.copilot.preview", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.catalog = bundled_catalog()
        self.firmwares = {item.id: item for item in self.catalog}
        self.simulation = Simulation(self.catalog)
        self.want_fullscreen = fullscreen
        self.tick_ms = tick_ms
        self.window = None
        self.modal = None
        self.timer = None
        self.task_view = None
        self.navigation_locked = False
        self.operation_return = "detail"
        self.importing = False
        self.scenario = "success"
        self.page_name = "store"
        self.selected = self.catalog[0].id
        self.filter_name = "全部"
        self.search_text = ""

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        GLib.set_application_name("TypixDeck Copilot")
        GLib.set_prgname("ai.typixdeck.copilot.preview")
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Gtk.ApplicationWindow(application=self, title="Copilot · TypixDeck 板载协处理器 · 预览模式")
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
        add(top, label("预览模式 · 不写入芯片", "simulation"))
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
        self.side_state = add(target, label("○ 演示设备", "accent"))
        self.side_state.get_style_context().add_class("small")
        add(sidebar, label("0.1 · 开发预览", "small"))
        self.content = add(middle, box(spacing=0), True)
        footer = add(root, box(False, 10, "footer"))
        self.notice = add(footer, label("预览已就绪", "small"), True)
        self.notice.set_ellipsize(Pango.EllipsizeMode.END)
        add(footer, label("Esc 返回 · F11 全屏", "small"))
        self.show_page("store")
        self.window.show_all()
        if self.want_fullscreen:
            self.window.fullscreen()
        self.window.present()

    def message(self, text):
        self.notice.set_text(text)

    def make_page(self, name):
        self.page_name = name
        for child in self.content.get_children():
            self.content.remove(child)
        for key, item in self.nav.items():
            context = item.get_style_context()
            context.add_class("active") if key == name or (name == "detail" and key == "store") else context.remove_class("active")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.content.pack_start(scroll, True, True, 0)
        page = box(spacing=14)
        page.set_border_width(20)
        scroll.add(page)
        return page

    def show_page(self, name):
        if self.navigation_locked:
            self.message("任务进行中，请勿切换或断电")
            return
        self.task_view = None
        if name == "detail":
            self.show_detail(self.selected)
            return
        page = self.make_page(name)
        getattr(self, "build_" + name)(page)
        self.content.show_all()

    def build_store(self, page):
        add(page, label("选择固件", "heading"))
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("搜索固件或版本")
        self.search.set_text(self.search_text)
        self.search.connect("search-changed", self.search_changed)
        add(page, self.search)
        filters = add(page, box(False, 7))
        self.filters = {}
        for name in ("全部", "官方", "自研", "第三方"):
            item = button(name, lambda _b, n=name: self.change_filter(n), "filter")
            if self.filter_name == name:
                styled(item, "active")
            add(filters, item)
            self.filters[name] = item
        self.results = add(page, box(spacing=12))
        self.render_results()

    def search_changed(self, entry):
        self.search_text = entry.get_text()
        self.render_results()

    def change_filter(self, name):
        self.filter_name = name
        for key, item in self.filters.items():
            item.get_style_context().add_class("active") if key == name else item.get_style_context().remove_class("active")
        self.render_results()

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
        add(actions, button("本地固件 →", lambda _b: self.show_page("library")), True)
        self.results.show_all()

    def show_detail(self, identifier):
        if self.navigation_locked:
            return
        self.task_view = None
        self.selected = identifier
        fw = self.firmwares[identifier]
        page = self.make_page("detail")
        head = add(page, box(False, 9))
        add(head, button("←", lambda _b: self.show_page("store"), "flat"))
        add(head, label(fw.title, "title", True), True)
        add(head, label("适配待验证", "pill"))
        version_row = add(page, box(False, 10))
        add(version_row, label("选择版本", "muted"))
        self.version_combo = Gtk.ComboBoxText()
        for item in self.catalog:
            if self.publisher(item) == self.publisher(fw) and (not fw.download_url or item.title == fw.title):
                self.version_combo.append(item.id, f"{item.version}  ·  {size_text(item.size)}")
        self.version_combo.set_active_id(fw.id)
        self.version_combo.connect("changed", lambda combo: self.show_detail(combo.get_active_id()) if combo.get_active_id() else None)
        add(version_row, self.version_combo, True)
        card = add(page, box(spacing=11, style="card"))
        row = add(card, box(False, 12))
        add(row, ChipArt(80, 80))
        info = add(row, box(spacing=7), True)
        add(info, label("完整固件", "title"))
        add(info, label("TypixDeck · 板载 ESP32-S3", "muted"))
        add(info, label("写入将重置设置", "warning"))
        source = Gtk.Expander(label="来源与校验详情")
        source_box = box(spacing=9)
        add(source_box, label(fw.summary, "small", True))
        add(source_box, label(fw.layout, "small", True))
        add(source_box, label(fw.filename, "mono", True))
        add(source_box, label(f"SHA256  {fw.sha256}", "mono", True))
        add(source_box, label(f"文件所在提交  {fw.commit}", "mono", True))
        add(source_box, label(fw.source_url, "mono", True))
        add(source_box, label("文件已核对；不代表发布者签名或板级适配。", "small", True))
        source.add(source_box)
        add(card, source)
        row = add(page, box(False, 10))
        self.write_button = add(row, button("写入", lambda _b: self.confirm_write(), "primary"), True)
        self.content.show_all()

    def build_library(self, page):
        add(page, label("本地固件", "heading"))
        self.cache_write_buttons, self.cache_remove_buttons = {}, {}
        add(page, label(f"预览缓存 · {len(self.simulation.cached)} 个版本", "small"))
        if not self.simulation.cached:
            card = add(page, box(spacing=10, style="card"))
            add(card, label("暂无缓存", "subheading"))
            add(card, button("选择固件 →", lambda _b: self.show_detail(self.catalog[0].id)))
        for fw in self.catalog:
            if fw.id in self.simulation.cached:
                row = add(page, box(False, 10, "card"))
                info = add(row, box(spacing=6), True)
                add(info, label(f"{fw.title} · {fw.version}", "subheading", True))
                add(info, label(f"预览缓存  ·  {size_text(fw.size)}", "small"))
                self.cache_write_buttons[fw.id] = add(row, button("写入", lambda _b, key=fw.id: self.prepare_cached_write(key), "primary"))
                self.cache_remove_buttons[fw.id] = add(row, button("移除缓存", lambda _b, key=fw.id: self.remove_cached(key)))

    def prepare_cached_write(self, identifier):
        if self.navigation_locked or self.importing or self.modal:
            return
        self.selected = identifier
        self.confirm_write()

    def remove_cached(self, identifier):
        if self.navigation_locked or self.importing or self.modal:
            return
        self.simulation.cached.discard(identifier)
        self.show_page("library")
        self.message("预览缓存已移除")

    def build_device(self, page):
        add(page, label("板载协处理器", "heading"))
        card = add(page, box(False, 20, "featured"))
        add(card, ChipArt(118, 126))
        info = add(card, box(spacing=10), True)
        add(info, label("TYPIXDECK", "eyebrow"))
        add(info, label("ESP32-S3 协处理器", "title"))
        current = self.firmwares.get(self.simulation.current)
        add(info, label(f"预览版本：{current.version if current else '尚无'}", "accent"))
        add(info, label("演示设备 · 未连接硬件", "small"))
        scenarios = add(page, box(spacing=10, style="card"))
        add(scenarios, label("预览场景", "subheading"))
        self.scenario_combo = Gtk.ComboBoxText()
        for key, title in (("success", "正常完成"), ("verification-failure", "校验失败"), ("disconnected", "连接中断")):
            self.scenario_combo.append(key, title)
        self.scenario_combo.set_active_id(self.scenario)
        self.scenario_combo.connect("changed", lambda item: setattr(self, "scenario", item.get_active_id()))
        add(scenarios, self.scenario_combo)
        add(page, button("重置本次预览", self.reset_demo))

    def build_history(self, page):
        add(page, label("写入记录", "heading"))
        add(page, label("本次预览记录", "small"))
        if not self.simulation.history:
            card = add(page, box(spacing=10, style="card"))
            add(card, label("暂无记录", "title"))
            add(card, button("选择固件 →", lambda _b: self.show_detail(self.catalog[0].id), "primary"))
        names = {"succeeded": "预览结束", "failed": "预览失败", "cancelled": "预览已取消"}
        for record in self.simulation.history:
            card = add(page, box(spacing=7, style="card"))
            row = add(card, box(False, 10))
            add(row, label(record["version"], "subheading"), True)
            add(row, label(names.get(record["status"], record["status"]), "accent" if record["status"] == "succeeded" else "warning"))
            if record["status"] == "failed":
                reason = "校验失败" if "校验失败" in record["message"] else "连接中断"
                add(card, label(reason, "small"))
            if record["status"] == "succeeded" and record["operation"] in ("switch", "restore") and record["id"] != self.simulation.current:
                add(card, button("写入此版本", lambda _b, key=record["id"]: self.prepare_restore(key)))

    def prepare_restore(self, identifier):
        if self.navigation_locked:
            return
        self.selected = identifier
        self.confirm_write("restore")

    def new_dialog(self, title, width=520):
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.set_default_size(width, -1)
        dialog.set_resizable(False)
        content = dialog.get_content_area()
        content.set_border_width(22)
        content.set_spacing(15)
        self.modal = dialog
        dialog.connect("destroy", lambda *_: setattr(self, "modal", None) if self.modal is dialog else None)
        return dialog, content

    def set_navigation_locked(self, locked):
        self.navigation_locked = locked
        for item in self.nav.values():
            item.set_sensitive(not locked)

    def confirm_write(self, operation="switch"):
        if self.modal or self.importing or self.navigation_locked:
            return
        self.operation_return = self.page_name if self.page_name in {"detail", "history", "library"} else "detail"
        self.task_view = "confirm"
        fw = self.firmwares[self.selected]
        page = self.make_page("confirmation")
        add(page, label("预览写入流程", "heading"))
        card = add(page, box(spacing=16, style="card"))
        row = add(card, box(False, 16))
        add(row, ChipArt(96, 100))
        info = add(row, box(spacing=10), True)
        add(info, label(f"{fw.title} · {fw.version}", "title", True))
        add(info, label("TypixDeck · 板载 ESP32-S3", "muted"))
        add(info, label("此版本仅演示流程，不会写入芯片", "warning", True))
        add(card, label("写入过程中请勿切换、拔线或断电", "warning", True))
        actions = add(page, box(False, 12))
        self.confirm_back = add(actions, button("返回", lambda *_: self.show_page(self.operation_return)), True)
        self.confirm_button = add(actions, button("预览流程", lambda *_: self.start_operation(operation), "primary"), True)
        self.content.show_all()
        self.confirm_back.grab_focus()

    def start_operation(self, operation="switch"):
        if operation not in {"switch", "restore"}:
            raise ValueError("界面仅支持写入流程，下载由缓存检查自动决定")
        if self.modal or self.importing or self.navigation_locked:
            return
        if self.task_view not in {"confirm", "result"}:
            self.operation_return = self.page_name if self.page_name in {"detail", "history", "library"} else "detail"
        try:
            self.simulation.start(self.selected, operation, self.scenario)
        except ValueError as exc:
            self.message(str(exc))
            return
        self.task_view = "running"
        self.set_navigation_locked(True)
        page = self.make_page("operation")
        fw = self.firmwares[self.simulation.active["id"]]
        self.operation_title = add(page, label("流程预览", "heading"))
        card = add(page, box(spacing=16, style="card"))
        row = add(card, box(False, 18))
        add(row, ChipArt(100, 110))
        info = add(row, box(spacing=10), True)
        add(info, label(f"{fw.title} · {fw.version}", "title", True))
        add(info, label("TypixDeck · 板载 ESP32-S3", "muted"))
        self.progress = add(card, Gtk.ProgressBar())
        self.progress.set_show_text(True)
        self.operation_status = add(card, label("准备中…", "subheading", True))
        self.operation_warning = add(card, label("写入过程中请勿切换、拔线或断电", "warning", True))
        self.operation_actions = add(page, box(False, 12))
        self.operation_button = add(self.operation_actions, button("取消", self.operation_response), True)
        self.content.show_all()
        self.render_operation(self.simulation.active)
        self.operation_button.grab_focus()
        self.timer = GLib.timeout_add(self.tick_ms, self.operation_tick)

    def operation_tick(self):
        result = self.simulation.tick()
        self.render_operation(result)
        if result["status"] != "running":
            self.timer = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def render_operation(self, result):
        self.progress.set_fraction(result["progress"])
        self.progress.set_text(f"{int(result['progress'] * 100)}%")
        phases = {"prepare": "检查缓存步骤", "download": "下载步骤", "write": "写入步骤", "verify": "校验步骤", "restart": "重启步骤", "complete": "未写入芯片，未执行重启", "cancelled": "预览已停止，未操作芯片"}
        status_text = phases.get(result["phase"], "未完成，请重试")
        if result["status"] == "failed":
            status_text = "校验失败场景，未操作芯片" if result["scenario"] == "verification-failure" else "连接中断场景，未操作芯片"
        self.operation_status.set_text(status_text)
        if result["status"] == "running":
            cancellable = result["phase"] in {"prepare", "download"}
            self.operation_button.set_sensitive(cancellable)
            self.operation_button.set_label("取消" if cancellable else "演示写入步骤，请稍候")
        if result["status"] != "running":
            self.task_view = "result"
            self.set_navigation_locked(False)
            self.operation_warning.set_text("实际下载、烧录和芯片重启均未执行")
            self.operation_button.set_sensitive(True)
            names = {"succeeded": "预览结束", "failed": "预览未完成", "cancelled": "预览已取消"}
            self.operation_title.set_text(names[result["status"]])
            self.operation_button.set_label("返回固件")
            if result["status"] == "failed":
                self.retry_button = add(self.operation_actions, button("重新预览", lambda *_: self.start_operation(result["operation"]), "primary"), True)
                self.operation_actions.show_all()
            self.operation_button.grab_focus()
            current = self.firmwares.get(self.simulation.current)
            self.side_state.set_text(f"预览 {current.version[5:]}" if current else "○ 演示设备")
            self.message(names[result["status"]])

    def stop_timer(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None

    def operation_response(self, *_args):
        active = self.simulation.active
        if active and active["status"] == "running":
            if active["phase"] not in {"prepare", "download"}:
                self.message("写入进行中，请勿切换或断电")
                return
            self.stop_timer()
            self.render_operation(self.simulation.cancel())
            return
        self.show_page(self.operation_return)

    def reset_demo(self, *_args):
        if self.modal or self.importing or self.navigation_locked:
            return
        self.stop_timer()
        self.simulation.reset()
        self.scenario = "success"
        self.side_state.set_text("○ 演示设备")
        self.show_page("device")
        self.message("预览已重置。")

    def close_window(self, *_args):
        if self.navigation_locked:
            self.message("任务进行中，请勿退出或断电")
            return True
        self.stop_timer()
        if self.simulation.active and self.simulation.active["status"] == "running":
            self.simulation.cancel()
        self.quit()
        return False

    def on_key(self, _window, event):
        if self.navigation_locked and (event.keyval in {Gdk.KEY_Escape, Gdk.KEY_F11} or
                                       (event.keyval == Gdk.KEY_f and event.state & Gdk.ModifierType.CONTROL_MASK)):
            self.message("任务进行中，请勿切换或断电")
            return True
        if event.keyval == Gdk.KEY_F11:
            self.want_fullscreen = not self.want_fullscreen
            self.window.fullscreen() if self.want_fullscreen else self.window.unfullscreen()
            return True
        if event.keyval == Gdk.KEY_Escape and not self.modal:
            if self.task_view in {"confirm", "result"}:
                self.show_page(self.operation_return)
            elif self.page_name != "store":
                self.show_page("store")
            else:
                self.window.close()
            return True
        if event.keyval == Gdk.KEY_f and event.state & Gdk.ModifierType.CONTROL_MASK:
            self.show_page("store")
            self.search.grab_focus()
            return True
        return False
