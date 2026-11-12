"""GTK4/libadwaita front end. Every action runs the pixel-eub CLI as a child."""

import shlex
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import EubError, __version__  # noqa: E402
from ..downloads import default_cache  # noqa: E402
from ..models import MODELS  # noqa: E402
from . import runner  # noqa: E402

APP_ID = "io.github.hurmichov.PixelEub"
REPOSITORY = "https://github.com/hurmichov/pixel-eub"
HERE = Path(__file__).resolve().parent

PACKS = (
    ("auto", "Automatic", "The selected model's mapped recovery pack"),
    ("gs201", "gs201", "Pixel 7 / 7 Pro recovery pack"),
    ("zuma/shiba", "zuma/shiba", "Pixel 8 recovery pack"),
    ("zuma/husky", "zuma/husky", "Pixel 8 Pro recovery pack"),
    ("path", "Local ZIP or folder", "A recovery pack you already have"),
    ("none", "None", "Use only a stage folder or factory image"),
)
PHASES = (
    "Find the ROM USB device and verify its identity",
    "Serve the signed stages requested by the ROM",
    "Confirm bootloader fastboot",
)
CHECK_ICONS = {
    "OK": ("pixel-eub-check-symbolic", "check-ok"),
    "INFO": ("dialog-information-symbolic", "check-info"),
    "WARN": ("dialog-warning-symbolic", "check-warn"),
    "FAIL": ("dialog-error-symbolic", "check-fail"),
}


def model_status(model):
    if model.soc == "gs101":
        return "GS101 modified-stage recovery"
    if not model.pack:
        return "no public USB BL1"
    if model.experimental:
        return "sibling pack: experimental"
    return "upstream recovery pack"


def human_size(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def label(text="", *classes, xalign=0.0, wrap=False, selectable=False):
    widget = Gtk.Label(label=text, xalign=xalign, wrap=wrap, selectable=selectable)
    for name in classes:
        widget.add_css_class(name)
    return widget


def icon(name, size=16, *classes):
    image = Gtk.Image.new_from_icon_name(name)
    image.set_pixel_size(size)
    for css in classes:
        image.add_css_class(css)
    return image


def button(text, icon_name=None, *classes, on_click=None):
    widget = Gtk.Button()
    if icon_name and text:
        widget.set_child(Adw.ButtonContent(label=text, icon_name=icon_name))
    elif icon_name:
        widget.set_icon_name(icon_name)
    else:
        widget.set_label(text)
    for name in classes:
        widget.add_css_class(name)
    if on_click:
        widget.connect("clicked", lambda *_: on_click())
    return widget


def card(child, *classes):
    frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    frame.add_css_class("card")
    frame.add_css_class("panel")
    for name in classes:
        frame.add_css_class(name)
    frame.append(child)
    return frame


def vbox(spacing=12, *children):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    for child in children:
        box.append(child)
    return box


def hbox(spacing=12, *children):
    box = Gtk.Box(spacing=spacing)
    for child in children:
        box.append(child)
    return box


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Pixel-EUB")
        self.set_default_size(1240, 860)
        self.set_size_request(380, 520)
        self.options = runner.load_options()
        self.job = None
        self.job_lines = []
        self.job_exit = None
        self.prompt_dialog = None
        self.closing = False
        self.action_buttons = []
        self.stop_buttons = []
        self.soc = None
        self.device_status = False
        self.log = Gtk.TextBuffer()
        self.apply_theme()
        self.build()
        self.poll_device()
        GLib.timeout_add_seconds(2, self.poll_device)
        self.connect("close-request", self.on_close_request)

    # ----- layout -------------------------------------------------------

    def build(self):
        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        pages = (
            ("dashboard", "Dashboard", "go-home-symbolic", self.dashboard_page()),
            ("firmware", "Firmware", "pixel-eub-chip-symbolic", self.firmware_page()),
            (
                "diagnostics",
                "Diagnostics",
                "system-search-symbolic",
                self.diagnostics_page(),
            ),
            ("analyzer", "Analyzer", "pixel-eub-chart-symbolic", self.analyzer_page()),
            (
                "terminal",
                "Terminal",
                "utilities-terminal-symbolic",
                self.terminal_page(),
            ),
            ("settings", "Settings", "emblem-system-symbolic", self.settings_page()),
            ("credits", "Credits", "system-users-symbolic", self.credits_page()),
        )
        for name, title, _, widget in pages:
            self.stack.add_titled(widget, name, title)
        self.toasts.set_child(self.stack)

        content_header = Adw.HeaderBar(show_title=False)
        content_header.pack_start(self.status_chip())
        self.spinner = Adw.Spinner(visible=False)
        content_header.pack_end(self.spinner)
        content_view = Adw.ToolbarView(content=self.toasts)
        content_view.add_top_bar(content_header)
        self.content_page = Adw.NavigationPage(title="Dashboard", child=content_view)

        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("navigation-sidebar")
        self.nav_names = []
        for name, title, icon_name, _ in pages:
            row = Gtk.ListBoxRow()
            row.set_child(hbox(14, icon(icon_name, 18), label(title, xalign=0)))
            row.get_child().set_margin_top(10)
            row.get_child().set_margin_bottom(10)
            row.get_child().set_margin_start(6)
            self.nav.append(row)
            self.nav_names.append(name)
        self.nav.connect("row-selected", self.on_nav)

        dark = Gtk.Switch(active=self.options.dark, valign=Gtk.Align.CENTER)
        dark.connect("notify::active", self.on_dark)
        self.dark_switch = dark
        theme_row = hbox(12, icon("weather-clear-night-symbolic", 18))
        theme_row.append(label("Dark Mode", xalign=0))
        theme_row.get_last_child().set_hexpand(True)
        theme_row.append(dark)
        theme_row.set_margin_start(18)
        theme_row.set_margin_end(18)
        theme_row.set_margin_bottom(18)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.append(Gtk.ScrolledWindow(child=self.nav, vexpand=True))
        sidebar.append(theme_row)
        title = hbox(
            10,
            icon(APP_ID, 28),
            label("Pixel-EUB", "app-title"),
        )
        sidebar_header = Adw.HeaderBar(title_widget=title, show_end_title_buttons=False)
        sidebar_view = Adw.ToolbarView(content=sidebar)
        sidebar_view.add_top_bar(sidebar_header)

        self.split = Adw.NavigationSplitView(
            sidebar=Adw.NavigationPage(title="Pixel-EUB", child=sidebar_view),
            content=self.content_page,
            min_sidebar_width=230,
            max_sidebar_width=280,
        )
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 760sp")
        )
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)
        self.set_content(self.split)
        self.nav.select_row(self.nav.get_row_at_index(0))

    def on_nav(self, _listbox, row):
        if row is None:
            return
        name = self.nav_names[row.get_index()]
        self.stack.set_visible_child_name(name)
        self.content_page.set_title(
            self.stack.get_page(self.stack.get_visible_child()).get_title()
        )
        self.split.set_show_content(True)

    def show_page(self, name):
        self.nav.select_row(self.nav.get_row_at_index(self.nav_names.index(name)))

    def page(self, icon_name, title, subtitle, *children, end=None):
        header = hbox(18)
        # Explicit hexpand=False stops the centred glyph from widening the tile.
        tile = Gtk.Box(halign=Gtk.Align.START, valign=Gtk.Align.CENTER, hexpand=False)
        tile.add_css_class("page-icon")
        glyph = icon(icon_name, 30)
        glyph.set_hexpand(True)
        glyph.set_halign(Gtk.Align.CENTER)
        tile.append(glyph)
        header.append(tile)
        text = vbox(
            2, label(title, "page-title"), label(subtitle, "dim-label", wrap=True)
        )
        text.set_valign(Gtk.Align.CENTER)
        text.set_hexpand(True)
        header.append(text)
        if end:
            end.set_valign(Gtk.Align.CENTER)
            header.append(end)
        body = vbox(18, header, *children)
        for side in ("top", "bottom"):
            getattr(body, f"set_margin_{side}")(24)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900, child=body)
        clamp.set_margin_start(18)
        clamp.set_margin_end(18)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def status_chip(self):
        chip = hbox(8)
        chip.add_css_class("status-chip")
        chip.append(icon("pixel-eub-chip-symbolic", 16))
        self.chip_soc = label("—")
        chip.append(self.chip_soc)
        self.chip_dot = label("●", "status-dot", "absent")
        chip.append(self.chip_dot)
        self.chip_text = label("No ROM device")
        chip.append(self.chip_text)
        return chip

    # ----- device status ------------------------------------------------

    def poll_device(self):
        from ..transport import ports

        try:
            found = ports()
            error = None
        except (EubError, OSError) as exc:
            found, error = [], str(exc)
        connected = bool(found)
        self.chip_dot.remove_css_class("connected" if not connected else "absent")
        self.chip_dot.add_css_class("connected" if connected else "absent")
        if error:
            text = "pyserial missing"
        elif len(found) > 1:
            text = f"{len(found)} ROM devices"
        else:
            text = "Connected" if connected else "No ROM device"
        self.chip_text.set_label(text)
        if not connected:
            self.soc = None
        self.chip_soc.set_label(self.soc.upper() if self.soc else "—")
        # A live recovery hint (release buttons, verifying) wins until the job ends.
        if hasattr(self, "device_title") and not (self.job and self.device_status):
            if connected:
                self.device_title.set_label("Pixel ROM Recovery device connected")
                self.device_detail.set_label(
                    ", ".join(p.device for p in found)
                    + "  ·  USB 18d1:4f00"
                    + (f"  ·  {self.soc}" if self.soc else "")
                )
            else:
                self.device_title.set_label(
                    error or "Waiting for a phone in Pixel ROM Recovery"
                )
                self.device_detail.set_label(
                    "Disconnect the phone, hold Power and both volume buttons, connect "
                    "USB and keep holding for about 15 seconds. The screen stays black. "
                    "Pixel 7 / 7 Pro / Fold / 7a: keep holding until the ROM requests "
                    "BL1; you will be told when to release."
                )
        return GLib.SOURCE_CONTINUE

    # ----- dashboard ----------------------------------------------------

    def dashboard_page(self):
        device = hbox(18)
        device.append(icon("phone-symbolic", 48))
        self.device_title = label("", "title-3", wrap=True)
        self.device_detail = label("", "dim-label", wrap=True)
        texts = vbox(4, self.device_title, self.device_detail)
        texts.set_hexpand(True)
        texts.set_valign(Gtk.Align.CENTER)
        device.append(texts)

        target = Adw.PreferencesGroup(title="Recovery target")
        names = ["Select the phone model…"] + [
            f"{m.name} ({m.codename})" for m in MODELS
        ]
        self.model_row = Adw.ComboRow(
            title="Phone model",
            model=Gtk.StringList.new(names),
            enable_search=True,
        )
        codenames = [m.codename for m in MODELS]
        if self.options.device in codenames:
            self.model_row.set_selected(codenames.index(self.options.device) + 1)
        self.model_row.connect("notify::selected", self.on_model)
        target.add(self.model_row)
        self.sources_row = Adw.ActionRow(title="Boot stage sources")
        change = button(
            "Change", None, "flat", on_click=lambda: self.show_page("firmware")
        )
        change.set_valign(Gtk.Align.CENTER)
        self.sources_row.add_suffix(change)
        target.add(self.sources_row)
        self.gs101_row = self.path_row(
            "GS101 profile folder",
            "gs101_profile",
            folders=True,
            empty="Select devices/<model> from the recovery archive",
        )
        target.add(self.gs101_row)
        wait = Adw.SwitchRow(
            title="Wait for the phone",
            subtitle="Scan for the ROM USB device instead of failing immediately",
            active=self.options.wait,
        )
        wait.connect(
            "notify::active", lambda row, _: self.set_option("wait", row.get_active())
        )
        target.add(wait)

        start = button(
            "Start recovery",
            "media-playback-start-symbolic",
            "suggested-action",
            "pill",
            on_click=lambda: self.start_boot(False),
        )
        dry = button(
            "Dry run",
            "object-select-symbolic",
            "pill",
            on_click=lambda: self.start_boot(True),
        )
        stop = button(
            "Stop",
            "media-playback-stop-symbolic",
            "destructive-action",
            "pill",
            on_click=self.stop_job,
        )
        self.action_buttons += [start, dry]
        self.stop_buttons.append(stop)
        actions = hbox(12, dry, start, stop)
        actions.set_halign(Gtk.Align.END)

        progress = Adw.PreferencesGroup(
            title="Progress",
            description="Images go to RAM. Nothing is flashed, erased or unlocked.",
        )
        self.phase_rows = []
        for number, text in enumerate(PHASES, 1):
            row = Adw.ActionRow(title=f"{number}. {text}")
            status = Gtk.Stack()
            status.add_named(icon("media-record-symbolic", 16, "dim-label"), "pending")
            status.add_named(Adw.Spinner(), "active")
            status.add_named(icon("pixel-eub-check-symbolic", 16, "check-ok"), "done")
            status.add_named(icon("dialog-error-symbolic", 16, "check-fail"), "failed")
            row.add_prefix(status)
            progress.add(row)
            self.phase_rows.append(status)

        self.stage_box = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=8,
            row_spacing=8,
            max_children_per_line=8,
        )
        self.stage_chips = {}
        self.stage_sizes = {}
        self.stage_placeholder = label(
            "Stages appear here as the ROM requests them.", "dim-label"
        )
        stages = vbox(
            10, label("Boot stages", "heading"), self.stage_placeholder, self.stage_box
        )

        self.result_icon = icon("dialog-information-symbolic", 24)
        self.result_text = label("", wrap=True, selectable=True)
        self.result_text.set_hexpand(True)
        self.result = hbox(14, self.result_icon, self.result_text)
        self.result.add_css_class("result-card")
        self.result.set_visible(False)

        log = Gtk.TextView(
            buffer=self.log, editable=False, monospace=True, cursor_visible=False
        )
        log.add_css_class("log-view")
        log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.dashboard_log = Gtk.ScrolledWindow(child=log, min_content_height=200)
        full = button(
            "Open terminal",
            "utilities-terminal-symbolic",
            "flat",
            on_click=lambda: self.show_page("terminal"),
        )
        log_header = hbox(8, label("Output", "heading"))
        log_header.get_first_child().set_hexpand(True)
        log_header.append(full)

        self.update_sources_summary()
        self.on_model(self.model_row, None)
        return self.page(
            "go-home-symbolic",
            "Dashboard",
            "Recover a compatible Tensor Pixel from ROM Recovery to bootloader fastboot.",
            card(device),
            target,
            actions,
            self.result,
            progress,
            card(stages),
            card(vbox(8, log_header, self.dashboard_log)),
        )

    def on_model(self, row, _param):
        index = row.get_selected()
        is_gs101 = (
            index not in (0, Gtk.INVALID_LIST_POSITION)
            and MODELS[index - 1].soc == "gs101"
        )
        if hasattr(self, "gs101_row"):
            self.gs101_row.set_visible(is_gs101)
            self.sources_row.set_visible(not is_gs101)
        if index == 0 or index == Gtk.INVALID_LIST_POSITION:
            self.set_option("device", "")
            row.set_subtitle("ROM identity does not reveal the exact model")
            return
        model = MODELS[index - 1]
        self.set_option("device", model.codename)
        row.set_subtitle(f"{model.soc}  ·  {model_status(model)}")

    def update_sources_summary(self):
        options = self.options
        parts = []
        pack = dict((key, name) for key, name, _ in PACKS)[options.pack]
        if options.pack == "path":
            parts.append(f"pack: {options.pack_path or 'not chosen'}")
        elif options.pack != "none":
            parts.append(f"pack: {pack}")
        if options.directory:
            parts.append(f"stages: {options.directory}")
        if options.image:
            parts.append(f"factory image: {options.image}")
        if options.build:
            parts.append(f"factory build: {options.build}")
        if hasattr(self, "sources_row"):
            self.sources_row.set_subtitle(" · ".join(parts) or "No sources selected")

    def reset_progress(self):
        for status in self.phase_rows:
            status.set_visible_child_name("pending")
        self.stage_box.remove_all()
        self.stage_chips.clear()
        self.stage_sizes.clear()
        self.stage_placeholder.set_visible(True)
        self.result.set_visible(False)

    def show_result(self, kind, text):
        for name in ("success", "error", "info"):
            self.result.remove_css_class(name)
        self.result.add_css_class(kind)
        self.result_icon.set_from_icon_name(
            {
                "success": "pixel-eub-check-symbolic",
                "error": "dialog-error-symbolic",
                "info": "dialog-information-symbolic",
            }[kind]
        )
        self.result_text.set_label(text)
        self.result.set_visible(True)

    def set_phase(self, number):
        for index, status in enumerate(self.phase_rows, 1):
            if index < number:
                status.set_visible_child_name("done")
            elif index == number:
                status.set_visible_child_name("active")

    def set_stage(self, event):
        chip = self.stage_chips.get(event.name)
        if chip is None:
            chip = label("", "stage-chip", xalign=0.5)
            self.stage_box.append(chip)
            self.stage_chips[event.name] = chip
            self.stage_placeholder.set_visible(False)
        for state in ("requested", "sending", "forced", "uploaded", "acknowledged"):
            chip.remove_css_class(state)
        chip.add_css_class(event.state)
        mark = {
            "requested": "…",
            "sending": "↑",
            "forced": "⚠↑",
            "uploaded": "✓",
            "acknowledged": "✓✓",
        }[event.state]
        if event.size:
            self.stage_sizes[event.name] = event.size
        size = self.stage_sizes.get(event.name)
        size = f" {human_size(size)}" if size else ""
        chip.set_label(f"{event.name}{size} {mark}")
        chip.set_tooltip_text(event.state)

    def start_boot(self, dry_run):
        if self.options.device in ("bluejay", "oriole", "raven"):
            return self.start_gs101(dry_run)
        try:
            args = runner.boot_args(self.options, dry_run=dry_run)
        except ValueError as exc:
            self.toast(str(exc))
            return
        self.reset_progress()
        self.boot_dry_run = dry_run
        self.boot_outcome = None

        def on_event(event):
            if isinstance(event, runner.Phase):
                self.set_phase(event.number)
            elif isinstance(event, runner.Stage):
                self.set_stage(event)
            elif isinstance(event, runner.Outcome):
                self.boot_outcome = event

        def on_exit(code):
            outcome = self.boot_outcome
            if code == 0 and dry_run:
                self.show_result(
                    "info",
                    "Dry run passed: sources are ready. USB was not opened, so this "
                    "does not show that the phone will accept them.",
                )
            elif code == 0:
                for status in self.phase_rows:
                    status.set_visible_child_name("done")
                self.show_result(
                    "success", outcome.text if outcome else "Recovery finished."
                )
            else:
                for status in self.phase_rows:
                    if status.get_visible_child_name() == "active":
                        status.set_visible_child_name("failed")
                text = outcome.text if outcome and not outcome.success else None
                if code == 130:
                    text = "Stopped. No automatic flashing was performed."
                self.show_result("error", text or f"Command failed (exit code {code}).")

        self.run(args, on_event=on_event, on_exit=on_exit)

    def start_gs101(self, dry_run):
        from ..gs101 import WARNING

        try:
            args = runner.gs101_args(self.options, dry_run=dry_run)
        except ValueError as exc:
            self.toast(str(exc))
            return

        def launch():
            self.reset_progress()

            def on_exit(code):
                if code == 0 and dry_run:
                    self.show_result(
                        "info",
                        "Profile files and model checked. USB was not opened; boot compatibility is unverified.",
                    )
                elif code == 0:
                    self.show_result(
                        "info",
                        "Fastboot USB detected. Device identity and permanent recovery are not confirmed.",
                    )
                elif code == 130:
                    self.show_result(
                        "info", "Stopped. No automatic flashing was performed."
                    )
                else:
                    self.show_result(
                        "error",
                        self.last_error()
                        or f"GS101 command failed (exit code {code}).",
                    )

            self.run(args, on_exit=on_exit)

        if dry_run:
            launch()
            return
        dialog = Adw.AlertDialog(heading="Start GS101 recovery?", body=WARNING)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("start", "Start recovery")
        dialog.set_response_appearance("start", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _dialog, response: launch() if response == "start" else None,
        )
        dialog.present(self)

    # ----- firmware -----------------------------------------------------

    def firmware_page(self):
        pack_group = Adw.PreferencesGroup(
            title="Recovery pack",
            description="tensor-usbdl v0.2.0 is downloaded once and verified by its "
            "pinned SHA-256. Only selected .img files are read; no upstream "
            "programs are run.",
        )
        keys = [key for key, _, _ in PACKS]
        self.pack_row = Adw.ComboRow(
            title="Pack", model=Gtk.StringList.new([name for _, name, _ in PACKS])
        )
        self.pack_row.set_selected(
            keys.index(self.options.pack) if self.options.pack in keys else 0
        )
        self.pack_row.connect("notify::selected", self.on_pack)
        pack_group.add(self.pack_row)
        self.pack_path_row = self.path_row(
            "Local recovery pack",
            "pack_path",
            files=True,
            folders=True,
        )
        pack_group.add(self.pack_path_row)
        self.on_pack(self.pack_row, None)

        extra = Adw.PreferencesGroup(
            title="Additional signed images",
            description="Priority: stage folder, then recovery pack, then factory image.",
        )
        extra.add(
            self.path_row("Factory ZIP or FBPK bootloader image", "image", files=True)
        )
        extra.add(
            self.path_row("Folder of signed *.img stages", "directory", folders=True)
        )

        factory = Adw.PreferencesGroup(
            title="Official factory images",
            description="For normal flashing after recovery. Downloads are verified "
            "against Google's SHA-256 and nothing is flashed automatically. Google's "
            "factory image terms apply.",
        )
        self.factory_build = Adw.EntryRow(
            title="Build ID (empty = latest generic build)"
        )
        factory.add(self.factory_build)
        use = Adw.SwitchRow(
            title="Also load this factory image during recovery",
            subtitle="Adds --build to recovery; needs the phone model",
            active=bool(self.options.build),
        )
        if self.options.build and self.options.build != "latest":
            self.factory_build.set_text(self.options.build)

        def sync_build(*_):
            text = self.factory_build.get_text().strip()
            self.set_option("build", (text or "latest") if use.get_active() else "")
            self.update_sources_summary()

        use.connect("notify::active", sync_build)
        self.factory_build.connect("changed", sync_build)
        factory.add(use)
        list_builds = button(
            "List builds",
            "view-list-symbolic",
            "pill",
            on_click=lambda: self.factory("builds"),
        )
        download = button(
            "Download",
            "folder-download-symbolic",
            "pill",
            "suggested-action",
            on_click=lambda: self.factory("fetch"),
        )
        self.action_buttons += [list_builds, download]
        factory_actions = hbox(12, list_builds, download)
        factory_actions.set_halign(Gtk.Align.END)
        self.download_status = label("", "dim-label", wrap=True)
        self.builds_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.builds_list.add_css_class("boxed-list")
        self.builds_list.set_visible(False)

        return self.page(
            "pixel-eub-chip-symbolic",
            "Firmware",
            "Choose signed boot-stage sources and get official factory images.",
            pack_group,
            extra,
            factory,
            factory_actions,
            self.download_status,
            self.builds_list,
        )

    def on_pack(self, row, _param):
        key, _, description = PACKS[row.get_selected()]
        row.set_subtitle(description)
        self.set_option("pack", key)
        self.pack_path_row.set_visible(key == "path")
        self.update_sources_summary()

    def path_row(self, title, option, *, files=False, folders=False, empty="Not set"):
        row = Adw.ActionRow(title=title)

        def refresh():
            value = getattr(self.options, option)
            row.set_subtitle(GLib.markup_escape_text(value or empty))
            clear.set_visible(bool(value))

        def chosen(path):
            self.set_option(option, path)
            refresh()
            self.update_sources_summary()

        if files:
            row.add_suffix(
                self.chooser_button("document-open-symbolic", "Choose file", chosen)
            )
        if folders:
            row.add_suffix(
                self.chooser_button(
                    "folder-open-symbolic", "Choose folder", chosen, folder=True
                )
            )
        clear = button("", "edit-clear-symbolic", "flat", on_click=lambda: chosen(""))
        clear.set_valign(Gtk.Align.CENTER)
        clear.set_tooltip_text("Clear")
        row.add_suffix(clear)
        refresh()
        return row

    def chooser_button(self, icon_name, tooltip, callback, *, folder=False):
        def pick():
            dialog = Gtk.FileDialog(title=tooltip)

            def done(source, result):
                try:
                    file = (
                        source.select_folder_finish(result)
                        if folder
                        else source.open_finish(result)
                    )
                except GLib.Error:
                    return
                if file and file.get_path():
                    callback(file.get_path())

            if folder:
                dialog.select_folder(self, None, done)
            else:
                dialog.open(self, None, done)

        widget = button("", icon_name, "flat", on_click=pick)
        widget.set_valign(Gtk.Align.CENTER)
        widget.set_tooltip_text(tooltip)
        return widget

    def factory(self, command):
        if not self.options.device:
            self.toast("Select the phone model on the Dashboard first.")
            return
        build = self.factory_build.get_text().strip() or "latest"
        args = runner.factory_args(command, self.options, self.options.device, build)
        self.download_status.set_label("")

        def on_event(event):
            if isinstance(event, runner.Line) and "MiB downloaded" in event.text:
                self.download_status.set_label(event.text.strip())

        def on_exit(code):
            if command == "fetch":
                path = self.job_lines[-1] if code == 0 and self.job_lines else ""
                self.download_status.set_label(
                    f"Saved and verified: {path}" if code == 0 else self.last_error()
                )
                return
            self.fill_builds(runner.parse_builds(self.job_lines) if code == 0 else [])
            if code:
                self.download_status.set_label(self.last_error())

        self.run(args, on_event=on_event, on_exit=on_exit)

    def fill_builds(self, builds):
        while row := self.builds_list.get_first_child():
            self.builds_list.remove(row)
        for build in builds:
            row = Adw.ActionRow(
                title=build.build,
                subtitle=GLib.markup_escape_text(build.description),
                subtitle_lines=2,
            )
            row.set_tooltip_text(f"SHA-256 {build.sha256}\n{build.url}")
            use = button("Select", None, "flat")
            use.set_valign(Gtk.Align.CENTER)
            use.connect(
                "clicked", lambda *_, b=build.build: self.factory_build.set_text(b)
            )
            row.add_suffix(use)
            self.builds_list.append(row)
        self.builds_list.set_visible(bool(builds))
        if builds:
            self.download_status.set_label(
                f"{len(builds)} builds. Select one, then Download."
            )

    # ----- diagnostics --------------------------------------------------

    def diagnostics_page(self):
        doctor = button(
            "Run doctor",
            "system-search-symbolic",
            "pill",
            "suggested-action",
            on_click=self.run_doctor,
        )
        detect = button(
            "Detect ports",
            "network-transmit-symbolic",
            "pill",
            on_click=self.run_detect,
        )
        self.action_buttons += [doctor, detect]
        self.detect_all = Gtk.CheckButton(label="Include all serial ports")
        actions = hbox(12, self.detect_all, detect, doctor)
        actions.set_halign(Gtk.Align.END)

        self.doctor_group = Adw.PreferencesGroup(
            title="Environment",
            description="Dependencies, ROM USB permissions, fastboot, cache and "
            "optional Linux port-recovery tools.",
        )
        self.doctor_rows = [
            Adw.ActionRow(title="Press Run doctor to check this computer.")
        ]
        self.doctor_group.add(self.doctor_rows[0])
        self.ports_group = Adw.PreferencesGroup(title="Serial ports")
        self.port_rows = [
            Adw.ActionRow(title="Press Detect ports to list USB serial ports.")
        ]
        self.ports_group.add(self.port_rows[0])
        models = Adw.PreferencesGroup(
            title="Models",
            description="Sources available for each model, not hardware validation.",
        )
        for model in MODELS:
            row = Adw.ActionRow(
                title=model.name, subtitle=f"{model.codename}  ·  {model.soc}"
            )
            status = label(model_status(model), "caption")
            status.add_css_class(
                "check-ok"
                if model.pack and not model.experimental
                else "check-warn"
                if model.pack
                else "dim-label"
            )
            row.add_suffix(status)
            models.add(row)
        return self.page(
            "system-search-symbolic",
            "Diagnostics",
            "Read-only checks. No phone is opened or modified.",
            actions,
            self.doctor_group,
            self.ports_group,
            models,
        )

    def replace_rows(self, group, rows, new_rows):
        for row in rows:
            group.remove(row)
        rows[:] = new_rows
        for row in rows:
            group.add(row)

    def run_doctor(self):
        args = ["doctor"]
        if self.options.fastboot != "fastboot":
            args += ["--fastboot", self.options.fastboot]
        if self.options.cache_dir:
            args += ["--cache-dir", self.options.cache_dir]

        def on_exit(code):
            rows = []
            for check in runner.parse_doctor(self.job_lines):
                row = Adw.ActionRow(title=GLib.markup_escape_text(check.message))
                row.set_title_lines(3)
                name, css = CHECK_ICONS[check.status]
                row.add_prefix(icon(name, 18, css))
                rows.append(row)
            self.replace_rows(self.doctor_group, self.doctor_rows, rows)
            self.toast("Doctor: no failures" if code == 0 else "Doctor found failures")

        self.run(args, on_exit=on_exit)

    def run_detect(self):
        args = ["detect"] + (["--all"] if self.detect_all.get_active() else [])

        def on_exit(code):
            rows = []
            for port in runner.parse_detect(self.job_lines):
                row = Adw.ActionRow(
                    title=port.device,
                    subtitle=GLib.markup_escape_text(
                        f"{port.usb}  ·  {port.description or 'no description'}"
                    ),
                )
                rows.append(row)
            if not rows:
                rows.append(
                    Adw.ActionRow(title=self.last_error() or "No matching ports")
                )
            self.replace_rows(self.ports_group, self.port_rows, rows)

        self.run(args, on_exit=on_exit)

    # ----- analyzer -----------------------------------------------------

    def analyzer_page(self):
        self.analyze_path = ""
        group = Adw.PreferencesGroup(
            title="File",
            description="A stage image (.img/.bin), an FBPK bootloader, a factory "
            "ZIP or a recovery pack. Nothing is sent to a phone.",
        )
        self.analyze_row = Adw.ActionRow(title="Image or archive", subtitle="Not set")

        def chosen(path):
            self.analyze_path = path
            self.analyze_row.set_subtitle(GLib.markup_escape_text(path))
            self.analyze()

        self.analyze_row.add_suffix(
            self.chooser_button("document-open-symbolic", "Choose a file", chosen)
        )
        group.add(self.analyze_row)
        analyze = button(
            "Analyze",
            "pixel-eub-chart-symbolic",
            "pill",
            "suggested-action",
            on_click=self.analyze,
        )
        self.action_buttons.append(analyze)
        analyze.set_halign(Gtk.Align.END)
        self.analysis_summary = label("", "title-4", wrap=True, selectable=True)
        self.analysis_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.analysis_list.add_css_class("boxed-list")
        self.analysis_list.set_visible(False)
        return self.page(
            "pixel-eub-chart-symbolic",
            "Analyzer",
            "Check boot-stage images: stage, signed USB-boot flag and SoC family. "
            "FBPK CRC32 values are verified.",
            group,
            analyze,
            self.analysis_summary,
            self.analysis_list,
        )

    def analyze(self):
        if not self.analyze_path:
            self.toast("Choose a bootloader image first.")
            return

        def on_exit(code):
            while row := self.analysis_list.get_first_child():
                self.analysis_list.remove(row)
            if code:
                self.analysis_summary.set_label(self.last_error())
                self.analysis_list.set_visible(False)
                return
            stages = runner.parse_inspect(self.job_lines)
            usable = [s for s in stages if s.usb]
            self.analysis_summary.set_label(
                f"{len(stages)} images, {len(usable)} with the signed USB-boot flag. "
                "The flag cannot be changed without breaking the signature."
            )
            for stage in stages:
                subtitle = " · ".join(
                    filter(None, (human_size(stage.size), stage.family, stage.flags))
                )
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(f"{stage.name} — {stage.stage}"),
                    subtitle=GLib.markup_escape_text(
                        "\n".join([subtitle, *stage.notes])
                    ),
                    subtitle_lines=4,
                )
                if stage.is_stage:
                    verdict = label(
                        "USB boot" if stage.usb else "No USB boot",
                        "caption",
                        "check-ok" if stage.usb else "check-fail",
                    )
                    row.add_suffix(verdict)
                self.analysis_list.append(row)
            self.analysis_list.set_visible(bool(stages))

        self.run(["inspect", self.analyze_path], on_exit=on_exit)

    # ----- terminal -----------------------------------------------------

    def terminal_page(self):
        view = Gtk.TextView(
            buffer=self.log, editable=False, monospace=True, cursor_visible=False
        )
        view.add_css_class("log-view")
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.terminal_scroll = Gtk.ScrolledWindow(
            child=view, min_content_height=480, vexpand=True
        )
        verbose = Adw.SwitchRow(
            title="Verbose ROM messages",
            subtitle="Shows raw ROM lines, including the phone's serial number. "
            "Redact it before sharing logs.",
            active=self.options.verbose,
        )
        verbose.connect(
            "notify::active",
            lambda row, _: self.set_option("verbose", row.get_active()),
        )
        group = Adw.PreferencesGroup()
        group.add(verbose)
        tools = hbox(
            8,
            button("Copy", "edit-copy-symbolic", "pill", on_click=self.copy_log),
            button("Save…", "document-save-symbolic", "pill", on_click=self.save_log),
            button(
                "Clear",
                "edit-clear-symbolic",
                "pill",
                on_click=lambda: self.log.set_text(""),
            ),
        )
        stop = button(
            "Stop",
            "media-playback-stop-symbolic",
            "pill",
            "destructive-action",
            on_click=self.stop_job,
        )
        self.stop_buttons.append(stop)
        tools.append(stop)
        tools.set_halign(Gtk.Align.END)
        return self.page(
            "utilities-terminal-symbolic",
            "Terminal",
            "Complete output of every command the GUI runs, with the exact CLI command.",
            group,
            tools,
            card(self.terminal_scroll),
        )

    def log_text(self):
        return self.log.get_text(
            self.log.get_start_iter(), self.log.get_end_iter(), False
        )

    def copy_log(self):
        self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(self.log_text())
        )
        self.toast("Log copied")

    def save_log(self):
        dialog = Gtk.FileDialog(title="Save log", initial_name="pixel-eub.log")

        def done(source, result):
            try:
                file = source.save_finish(result)
            except GLib.Error:
                return
            Path(file.get_path()).write_text(self.log_text(), encoding="utf-8")
            self.toast("Log saved")

        dialog.save(self, None, done)

    def append_log(self, text):
        self.log.insert(self.log.get_end_iter(), text + "\n")
        for scroll in (self.terminal_scroll, self.dashboard_log):
            adjustment = scroll.get_vadjustment()
            GLib.idle_add(lambda a=adjustment: a.set_value(a.get_upper()))

    # ----- settings -----------------------------------------------------

    def settings_page(self):
        connection = Adw.PreferencesGroup(title="Connection")
        connection.add(self.entry_row("Serial port (empty = automatic)", "port"))
        for title, option, maximum in (
            ("Wait for ROM USB (seconds)", "wait_timeout", 600),
            ("USB re-enumeration window (seconds)", "reconnect_wait", 300),
            ("ROM idle timeout (seconds)", "idle_timeout", 300),
            ("Fastboot confirmation timeout (seconds)", "verify_timeout", 300),
            ("Wait after an upload, e.g. GSA1 (seconds)", "ack_timeout", 3600),
        ):
            connection.add(self.spin_row(title, option, maximum))

        fastboot = Adw.PreferencesGroup(
            title="Fastboot",
            description="Android SDK Platform-Tools confirm the final bootloader state.",
        )
        fastboot.add(self.entry_row("fastboot executable", "fastboot"))
        fastboot.add(
            self.entry_row("Target fastboot serial (optional)", "fastboot_serial")
        )

        downloads = Adw.PreferencesGroup(title="Downloads and cache")
        downloads.add(
            self.path_row(
                "Cache folder",
                "cache_dir",
                folders=True,
                empty=f"Default: {default_cache()}",
            )
        )
        downloads.add(
            self.switch_row("Offline", "offline", "Use verified cached downloads only")
        )
        downloads.add(
            self.switch_row(
                "Refresh factory catalog",
                "refresh",
                "Ignore the cached Google image index",
            )
        )

        protocol = Adw.PreferencesGroup(
            title="Advanced protocol",
            description="Change only for a known chip and a documented reason.",
        )
        protocol.add(
            self.choice_row(
                "EPBL slicing",
                "epbl",
                ("", "full", "split"),
                ("Automatic", "full", "split"),
            )
        )
        protocol.add(
            self.choice_row(
                "Upload checksum",
                "checksum",
                ("ffff", "sum16"),
                ("ffff (default)", "sum16"),
            )
        )

        research = Adw.PreferencesGroup(
            title="Research",
            description="Data for comparing attempts. Nothing extra is sent to the "
            "phone. Files contain the USB serial number; redact it before sharing.",
        )
        research.add(
            self.path_row("Research folder", "research_dir", folders=True, empty="Off")
        )
        research.add(
            self.switch_row(
                "USB descriptors at every connection (--dump-usb)",
                "dump_usb",
                "Read passively from Linux sysfs; changes between stages are logged",
            )
        )
        research.add(
            self.switch_row(
                "Raw serial capture (--capture)",
                "capture",
                "Every ROM byte with timestamps; uploads as length, head and SHA-256",
            )
        )
        listen = self.spin_row(
            "Listen after a ROM failure (seconds, 0 = off)",
            "listen_after_failure",
            120,
            minimum=0,
        )
        listen.set_subtitle("Reports each repeated failure line; nothing is uploaded")
        research.add(listen)

        safety = Adw.PreferencesGroup(
            title="Safety (this session only)",
            description="These switches are never saved. Every YES/NO question is "
            "still shown and must be confirmed by typing YES.",
        )
        safety.add(
            self.switch_row(
                "Allow incompatible selections (--override)",
                "override",
                "Review model/SoC mismatches, and offer the experimental forced chain "
                "after an iROM panic",
            )
        )
        safety.add(
            self.switch_row(
                "Send BL1 without the USB-boot flag (--no-usb-check)",
                "no_usb_check",
                "Experiment only: the ROM still verifies the signature and is expected "
                "to reject a retail BL1. Images go to RAM.",
            )
        )
        safety.add(
            self.switch_row(
                "Continue when DPM comes before BL1 (--allow-dpm-first)",
                "allow_dpm_first",
                "Pixel 7, 7 Pro, Fold: normally recovery stops, because a DPM-first "
                "ROM started BL1 from internal storage",
            )
        )
        safety.add(
            self.switch_row(
                "Allow sibling-model packs (--allow-experimental)",
                "allow_experimental",
                "Pixel 7a, Fold, Tablet and 8a use another model's pack",
            )
        )
        safety.add(
            self.switch_row(
                "Linux port recovery (--port-recovery)",
                "port_recovery",
                "May run setfacl and fuser -k -TERM as administrator on the verified "
                "Pixel port. The system password dialog (polkit) asks for approval.",
            )
        )
        return self.page(
            "emblem-system-symbolic",
            "Settings",
            "Options map one-to-one onto pixel-eub command-line options.",
            connection,
            fastboot,
            downloads,
            protocol,
            research,
            safety,
        )

    def entry_row(self, title, option):
        row = Adw.EntryRow(title=title, text=getattr(self.options, option))
        row.connect("changed", lambda r: self.set_option(option, r.get_text().strip()))
        return row

    def spin_row(self, title, option, maximum, minimum=1):
        row = Adw.SpinRow.new_with_range(minimum, maximum, 1)
        row.set_title(title)
        row.set_value(getattr(self.options, option))
        row.connect(
            "notify::value", lambda r, _: self.set_option(option, r.get_value())
        )
        return row

    def switch_row(self, title, option, subtitle=""):
        row = Adw.SwitchRow(
            title=title, subtitle=subtitle, active=getattr(self.options, option)
        )
        row.connect(
            "notify::active", lambda r, _: self.set_option(option, r.get_active())
        )
        return row

    def choice_row(self, title, option, values, names):
        current = getattr(self.options, option)
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(list(names)))
        row.set_selected(values.index(current) if current in values else 0)
        row.connect(
            "notify::selected",
            lambda r, _: self.set_option(option, values[r.get_selected()]),
        )
        return row

    def set_option(self, name, value):
        setattr(self.options, name, value)

    # ----- credits ------------------------------------------------------

    def credits_page(self):
        about = vbox(10)
        about.set_valign(Gtk.Align.CENTER)
        logo = icon(APP_ID, 140)
        about.append(logo)
        about.append(label("Pixel-EUB", "hero-title", xalign=0.5))
        version = label(f"Version {__version__}", "version-pill", xalign=0.5)
        version.set_halign(Gtk.Align.CENTER)
        about.append(version)
        about.append(
            label(
                "A desktop tool that serves signed boot stages to compatible Tensor "
                "Pixels in ROM Recovery and confirms bootloader fastboot.",
                "dim-label",
                xalign=0.5,
                wrap=True,
            )
        )
        about.append(Gtk.Separator(margin_top=8, margin_bottom=8))
        about.append(
            label("© 2026 pixel-eub contributors\nMIT License", xalign=0.5, wrap=True)
        )
        about.get_last_child().set_justify(Gtk.Justification.CENTER)

        developer = hbox(18)
        developer.append(Adw.Avatar(size=72, text="hurmich", show_initials=False))
        dev_text = vbox(
            4,
            label("hurmich", "title-4"),
            label("Developer & Maintainer", "accent"),
            label(
                "Building tools for embedded systems and reverse engineering.",
                "dim-label",
                wrap=True,
            ),
        )
        dev_text.set_valign(Gtk.Align.CENTER)
        developer.append(dev_text)

        thanks = vbox(14, label("Special Thanks", "heading"))
        for icon_name, color, title, text in (
            (
                "pixel-eub-heart-symbolic",
                "blue",
                "The open source community",
                "For the tools and libraries this project builds on.",
            ),
            (
                "pixel-eub-code-symbolic",
                "violet",
                "JoshuaDoes · tensor-usbdl",
                "Tensor USB download protocol observations and recovery packs.",
            ),
            (
                "pixel-eub-chip-symbolic",
                "green",
                "hack-different · pixel-restore",
                "Pixel ROM Recovery protocol observations.",
            ),
        ):
            badge = Gtk.Box(valign=Gtk.Align.CENTER, hexpand=False)
            badge.add_css_class("thanks-icon")
            badge.add_css_class(color)
            glyph = icon(icon_name, 20)
            glyph.set_hexpand(True)
            badge.append(glyph)
            text_box = vbox(
                2, label(title, "heading"), label(text, "dim-label", wrap=True)
            )
            text_box.set_valign(Gtk.Align.CENTER)
            thanks.append(hbox(14, badge, text_box))

        right = vbox(
            18,
            card(vbox(14, label("Developed By", "heading"), developer)),
            card(thanks),
        )
        right.set_hexpand(True)
        top = Gtk.Box(spacing=18, homogeneous=True)
        top.append(card(about))
        top.append(right)

        def chips(title, items):
            flow = Gtk.FlowBox(
                selection_mode=Gtk.SelectionMode.NONE,
                column_spacing=10,
                row_spacing=10,
                max_children_per_line=6,
            )
            for icon_name, text in items:
                chip = hbox(10, icon(icon_name, 18), label(text))
                chip.add_css_class("tech-chip")
                flow.append(chip)
            return card(vbox(14, label(title, "heading"), flow))

        source = button(
            "Open Source",
            "pixel-eub-code-symbolic",
            "pill",
            "accent",
            on_click=lambda: Gtk.UriLauncher(uri=REPOSITORY).launch(self, None, None),
        )
        footer = hbox(
            8,
            label("Made with"),
            icon("pixel-eub-heart-symbolic", 18, "accent"),
            label("for the embedded community. Not affiliated with Google or Samsung."),
        )
        footer.set_halign(Gtk.Align.CENTER)
        return self.page(
            "system-users-symbolic",
            "Credits",
            "Information about this application and the people behind it.",
            top,
            chips(
                "Libraries & Technologies",
                (
                    ("text-x-generic-symbolic", "Python"),
                    ("applications-engineering-symbolic", "GTK 4"),
                    ("preferences-desktop-appearance-symbolic", "libadwaita"),
                    ("pixel-eub-code-symbolic", "PyGObject"),
                    ("network-transmit-symbolic", "pySerial"),
                    ("phone-symbolic", "Android Platform-Tools"),
                ),
            ),
            chips(
                "AI Assistants",
                (
                    ("pixel-eub-sparkle-symbolic", "Claude"),
                    ("pixel-eub-sparkle-symbolic", "ChatGPT"),
                    ("pixel-eub-sparkle-symbolic", "Gemini"),
                ),
            ),
            card(footer),
            end=source,
        )

    # ----- jobs ---------------------------------------------------------

    def run(self, args, *, on_event=None, on_exit=None):
        if self.job is not None:
            self.toast("Another command is still running.")
            return
        runner.save_options(self.options)
        self.job_lines = []
        self.job_handlers = (on_event, on_exit)
        self.append_log(f"$ pixel-eub {shlex.join(args)}")
        try:
            self.job = runner.Job(
                args, lambda event: GLib.idle_add(self.dispatch, event)
            )
        except OSError as exc:
            self.append_log(f"Cannot start pixel-eub: {exc}")
            self.toast(f"Cannot start pixel-eub: {exc}")
            return
        self.set_busy(True)

    def dispatch(self, event):
        on_event, on_exit = self.job_handlers
        if isinstance(event, runner.Line):
            self.job_lines.append(event.text)
            self.append_log(event.text)
        elif isinstance(event, runner.Identity):
            self.soc = event.soc
            self.poll_device()
        elif isinstance(event, runner.Prompt):
            self.ask(event)
        elif isinstance(event, runner.Hint):
            self.toast(event.text)
            self.device_status = True
            self.device_title.set_label(event.text)
            self.device_detail.set_label("The ROM booted BL1 over USB.")
        elif isinstance(event, runner.Waiting):
            self.device_status = True
            self.device_title.set_label("The phone is verifying — keep it connected")
            self.device_detail.set_label(event.text)
        if on_event and not isinstance(event, runner.Exit):
            on_event(event)
        if isinstance(event, runner.Exit):
            if self.prompt_dialog is not None:
                self.prompt_dialog.force_close()
                self.prompt_dialog = None
            self.append_log(f"[exit code {event.code}]\n")
            self.job = None
            self.device_status = False
            self.poll_device()
            self.set_busy(False)
            if on_exit:
                on_exit(event.code)
            if self.closing:
                self.close()
        return GLib.SOURCE_REMOVE

    def last_error(self):
        for line in reversed(self.job_lines):
            if line.startswith("pixel-eub: "):
                return line.removeprefix("pixel-eub: ")
        return ""

    def set_busy(self, busy):
        for widget in self.action_buttons:
            widget.set_sensitive(not busy)
        for widget in self.stop_buttons:
            widget.set_visible(busy)
        self.spinner.set_visible(busy)

    def stop_job(self):
        if self.job is not None:
            self.append_log("[stop requested: sending interrupt]")
            self.job.interrupt()

    def ask(self, prompt):
        title = (
            "Continue despite the ROM panic?"
            if prompt.kind == "panic"
            else "Continue with incompatible images?"
        )
        dialog = Adw.AlertDialog(
            heading=title,
            body="The command is waiting for an explicit answer. Type YES to "
            "continue; anything else answers NO.",
        )
        context = label(
            "\n".join(prompt.context + [prompt.question]),
            "prompt-context",
            wrap=True,
            selectable=False,
        )
        entry = Gtk.Entry(placeholder_text="Type YES to confirm")
        dialog.set_extra_child(vbox(12, card(context), entry))
        dialog.add_response("no", "NO")
        dialog.add_response("yes", "YES, continue")
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_enabled("yes", False)
        dialog.set_default_response("no")
        dialog.set_close_response("no")
        entry.connect(
            "changed",
            lambda e: dialog.set_response_enabled(
                "yes", e.get_text().strip().upper() == "YES"
            ),
        )
        job = self.job

        def respond(_dialog, response):
            self.prompt_dialog = None
            if job is not None:
                job.answer(response == "yes")

        dialog.connect("response", respond)
        self.prompt_dialog = dialog
        dialog.present(self)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    # ----- appearance and lifetime --------------------------------------

    def apply_theme(self):
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
            if self.options.dark
            else Adw.ColorScheme.FORCE_LIGHT
        )

    def on_dark(self, switch, _param):
        self.set_option("dark", switch.get_active())
        self.apply_theme()

    def on_close_request(self, _window):
        runner.save_options(self.options)
        if self.job is None or self.closing:
            return False
        dialog = Adw.AlertDialog(
            heading="Stop the running command?",
            body="A pixel-eub command is still running. Stopping sends an "
            "interrupt; uploads already in the phone's RAM cannot be undone.",
        )
        dialog.add_response("cancel", "Keep running")
        dialog.add_response("stop", "Stop and quit")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def respond(_dialog, response):
            if response == "stop":
                self.closing = True
                self.stop_job()

        dialog.connect("response", respond)
        dialog.present(self)
        return True


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(HERE / "icons"))
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        Gtk.Window.set_default_icon_name(APP_ID)

    def do_activate(self):
        window = self.get_active_window() or Window(self)
        window.present()


def main(argv=None) -> int:
    return Application().run(argv or [])
