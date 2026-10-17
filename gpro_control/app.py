"""GTK4 app: battery status, DPI control, alerts, and a tray icon."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
import bisect  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402

from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .alerts import notify  # noqa: E402
from .backend import Backend, Snapshot  # noqa: E402
from . import icons  # noqa: E402
from . import setupcheck  # noqa: E402
from .config import CONFIG_DIR, alert_settings, load_config  # noqa: E402
from .tray import MenuItem, TrayIcon  # noqa: E402

APP_ID = "io.github.gpro_control"
PRESETS = [400, 800, 1200, 1600, 3200]
SCALE_STEPS = 1000  # the slider is logarithmic: position 0..SCALE_STEPS

CSS = b"""
.card { padding: 16px; border-radius: 12px; background: alpha(currentColor, 0.06); }
.big { font-size: 34px; font-weight: 700; }
.section { font-weight: 700; opacity: 0.8; }
.dim { opacity: 0.65; }
levelbar block.filled.low { background: #e01b24; }
.setup-ok { color: #22c55e; }
.setup-warn { color: #f59e0b; }
.setup-info { opacity: 0.6; }
.setup-title { font-weight: 600; }
"""

STATUS_TEXT = {
    "charging": "Charging",
    "discharging": "On battery",
    "full": "Fully charged",
    "not charging": "Plugged in, not charging",
    "error": "Battery error",
    "unknown": "Unknown",
}

ALERT_MODE_TEXT = {
    "service": "Alerts run in the background, even with this window closed.",
    "window": "Alerts only work while this window is open (install the package for background alerts).",
    "off": "All alerts are off.",
}

CLOSE_ACTIONS = [("tray", "Keep running in the tray"), ("quit", "Quit G PRO Control")]
APPEARANCES = [("system", "System"), ("light", "Light"), ("dark", "Dark")]
TRAY_OUTLINES = [("auto", "Match system"), ("light", "White"), ("dark", "Black")]

THEME_DIRS = [os.path.expanduser("~/.local/share/themes"), os.path.expanduser("~/.themes"), "/usr/share/themes"]
BUILTIN_THEMES = {"Adwaita", "Default", "HighContrast", "HighContrastInverse"}  # compiled into GTK, light + dark


def _theme_has(name, css="gtk.css"):
    if name in BUILTIN_THEMES:
        return True
    return any(os.path.exists(os.path.join(d, name, "gtk-4.0", css)) for d in THEME_DIRS)


USER_GTK_CSS = os.path.join(os.path.dirname(CONFIG_DIR), "gtk-4.0", "colors.css")
_COLOR_RE = re.compile(r"@define-color\s+([\w-]+)\s+([^;]+);")


def _read_css(path, depth=0):
    """A stylesheet's text with its @import url(...) lines inlined."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return ""
    if depth < 3:
        def inline(m):
            return _read_css(os.path.normpath(os.path.join(os.path.dirname(path), m.group(1))), depth + 1)
        text = re.sub(r'@import\s+url\(["\']?([^"\')]+)["\']?\);', inline, text)
    return text


def theme_palette_css(theme, dark):
    """@define-color lines restoring `theme`'s own light or dark colours.

    Desktops write their colour scheme into ~/.config/gtk-4.0 (KDE does), and
    that user stylesheet beats any theme variant. Loaded above it, this
    puts the chosen variant's colours back. Empty if the theme isn't a file.
    """
    names = ["gtk-dark.css", "gtk.css"] if dark else ["gtk.css"]
    for d in THEME_DIRS:
        for name in names:
            path = os.path.join(d, theme, "gtk-4.0", name)
            if os.path.exists(path):
                palette = dict(_COLOR_RE.findall(_read_css(path)))
                break
        else:
            continue
        break
    else:
        return ""
    # Colours the desktop adds that the theme doesn't define (KDE's header bar ones).
    bg = palette.get("theme_bg_color_breeze") or palette.get("theme_bg_color") or "@theme_bg_color"
    fg = palette.get("theme_fg_color_breeze") or palette.get("theme_fg_color") or "@theme_fg_color"
    dim = palette.get("insensitive_fg_color_breeze") or palette.get("insensitive_fg_color") or fg
    for extra in _COLOR_RE.findall(_read_css(USER_GTK_CSS)):
        name = extra[0]
        if name not in palette:
            palette[name] = dim if "insensitive" in name else fg if "foreground" in name else bg
    return "".join(f"@define-color {k} {v};\n" for k, v in palette.items())


UDEV_HINT = (
    "No permission to talk to the mouse, so DPI can't be changed.\n"
    "Install the udev rule (see README) and replug the mouse or receiver."
)


def _label(text="", css=None, xalign=0.0, **kw):
    lbl = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in css or []:
        lbl.add_css_class(c)
    return lbl


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="G PRO Control")
        self.set_default_size(440, -1)
        self.set_resizable(False)
        self.dpi_info = None
        self._syncing = False
        self._spin_prev = 0
        self._syncing_autostart = False
        self.setup_checks = []
        self._setup_msgs = {}

        self.connect("close-request", self._on_close_request)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        header = Gtk.HeaderBar(title_widget=Gtk.StackSwitcher(stack=self.stack))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh now")
        refresh.connect("clicked", lambda *_: self.backend.refresh())
        header.pack_end(refresh)
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(18)
        outer.append(self.stack)
        self.set_child(outer)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.stack.add_titled(root, "mouse", "Mouse")

        # Device ----------------------------------------------------------
        self.name_lbl = _label("Looking for your mouse…", ["title-2"])
        self.conn_lbl = _label("", ["dim"])
        root.append(self.name_lbl)
        root.append(self.conn_lbl)

        # Battery ---------------------------------------------------------
        bat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card"])
        bat.append(_label("Battery", ["section"]))
        row = Gtk.Box(spacing=12)
        self.pct_lbl = _label("–", ["big"])
        self.state_lbl = _label("", hexpand=True)
        row.append(self.pct_lbl)
        row.append(self.state_lbl)
        bat.append(row)
        self.level = Gtk.LevelBar(min_value=0, max_value=100)
        self.level.add_offset_value("low", 15)
        self.level.add_offset_value("high", 50)
        self.level.add_offset_value("full", 100)
        bat.append(self.level)
        self.extra_lbl = _label("", ["dim"])
        bat.append(self.extra_lbl)
        root.append(bat)

        # DPI -------------------------------------------------------------
        self.dpi_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card"])
        head = Gtk.Box()
        head.append(_label("DPI", ["section"], hexpand=True))
        self.cur_dpi_lbl = _label("", ["dim"], xalign=1.0)
        head.append(self.cur_dpi_lbl)
        self.dpi_card.append(head)

        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False)
        self.spin = Gtk.SpinButton(numeric=True, width_chars=6)
        self.scale.connect("value-changed", self._on_scale)
        self.spin.connect("value-changed", self._on_spin)
        self.spin.connect("activate", lambda *_: self._apply())
        srow = Gtk.Box(spacing=8)
        srow.append(self.scale)
        srow.append(self.spin)
        self.dpi_card.append(srow)

        self.presets = Gtk.Box(spacing=6, homogeneous=True)
        self.dpi_card.append(self.presets)

        mrow = Gtk.Box(spacing=8)
        self.mode_lbl = _label("", ["dim"], hexpand=True, wrap=True)
        mrow.append(self.mode_lbl)
        self.onboard_btn = Gtk.Button(label="Use onboard profile",
                                      tooltip_text="Give control back to the profile stored on the mouse")
        self.onboard_btn.connect("clicked", lambda *_: self.backend.use_onboard_profile())
        mrow.append(self.onboard_btn)
        self.dpi_card.append(mrow)

        arow = Gtk.Box(spacing=8)
        self.restore_chk = Gtk.CheckButton(label="Restore on reconnect", hexpand=True,
                                           tooltip_text="Re-apply this DPI whenever the mouse reconnects while the app is open")
        arow.append(self.restore_chk)
        self.apply_btn = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        self.apply_btn.connect("clicked", lambda *_: self._apply())
        arow.append(self.apply_btn)
        self.dpi_card.append(arow)
        root.append(self.dpi_card)

        self.msg_lbl = _label("", ["dim"], wrap=True)

        self.backend = Backend(lambda snap: GLib.idle_add(self._update, snap))
        self.settings = dict(self.backend.config)  # UI-side copy; the backend saves
        self.stack.add_titled(self._page(self._build_alerts(alert_settings(self.backend.config))), "alerts", "Alerts")
        # The setup check can grow; scroll inside the page rather than stretch the window.
        settings_page = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           child=self._page(self._build_setup(), self._build_settings()))
        self.stack.add_titled(settings_page, "settings", "Settings")
        outer.append(self.msg_lbl)
        self.restore_chk.set_active(self.backend.config.get("restore", True))
        self.restore_chk.connect("toggled", lambda b: self.backend.set_restore(b.get_active()))
        self.backend.start()

    @staticmethod
    def _page(*children):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for c in children:
            box.append(c)
        return box

    # -- closing / tray -------------------------------------------------------

    def _on_close_request(self, _win):
        app = self.get_application()
        if self.settings.get("close_action", "tray") == "tray" and app.tray_available():
            self.set_visible(False)
            return True  # keep the window (and its state) for next time
        app.quit()
        return False

    def toggle(self):
        if self.is_visible() and self.get_application().tray_available():
            self.set_visible(False)
        else:
            self.present()

    # -- setup check ------------------------------------------------------------

    def _build_setup(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card"])
        head = Gtk.Box()
        head.append(_label("Setup check", ["section"], hexpand=True))
        again = Gtk.Button(label="Check again")
        again.connect("clicked", lambda *_: self.run_setup_check())
        head.append(again)
        card.append(head)
        self.setup_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.append(self.setup_rows)
        GLib.idle_add(self.run_setup_check)
        return card

    def run_setup_check(self, *_):
        self.setup_checks = setupcheck.run_checks(self.get_application().tray_available())
        while child := self.setup_rows.get_first_child():
            self.setup_rows.remove(child)
        icon_names = {setupcheck.OK: "emblem-ok-symbolic", setupcheck.WARN: "dialog-warning-symbolic",
                      setupcheck.INFO: "dialog-information-symbolic"}
        for check in self.setup_checks:
            row = Gtk.Box(spacing=10)
            icon = Gtk.Image.new_from_icon_name(icon_names[check.state])
            icon.add_css_class(f"setup-{check.state}")
            icon.set_valign(Gtk.Align.START)
            row.append(icon)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            text.append(_label(check.title, ["setup-title"]))
            # Details can carry a command to copy, so keep multi-line ones selectable.
            text.append(_label(check.detail, ["dim"], wrap=True, selectable="\n" in check.detail))
            if check.key in self._setup_msgs:
                text.append(_label(self._setup_msgs[check.key], ["dim"], wrap=True, selectable=True))
            row.append(text)
            if check.fix:
                button = Gtk.Button(label=check.fix_label, valign=Gtk.Align.CENTER)
                button.connect("clicked", lambda b, c=check: self._run_fix(c, b))
                row.append(button)
            self.setup_rows.append(row)
        self._syncing_autostart = True
        self.autostart_chk.set_active(setupcheck.autostart_enabled())
        self._syncing_autostart = False
        return False

    def _run_fix(self, check, button):
        button.set_sensitive(False)
        button.set_label("Working…")

        def done(ok, msg):
            if msg:
                self._setup_msgs[check.key] = msg
            self.run_setup_check()
            GLib.timeout_add_seconds(2, self.run_setup_check)  # trays and bars take a moment to appear
            return False

        def work():
            try:
                ok, msg = check.fix()
            except Exception as e:  # a fix must never take the app down
                ok, msg = False, f"That didn't work: {e}"
            GLib.idle_add(done, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    # -- settings -------------------------------------------------------------

    def _build_settings(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card"])
        card.append(_label("General", ["section"]))

        row = Gtk.Box(spacing=8)
        row.append(_label("Appearance", hexpand=True, tooltip_text="Light or dark for this window only"))
        look_dd = Gtk.DropDown.new_from_strings([text for _, text in APPEARANCES])
        look_keys = [key for key, _ in APPEARANCES]
        look_dd.set_selected(look_keys.index(self.settings.get("appearance", "system")))
        look_dd.connect("notify::selected", lambda dd, _p: self._set_appearance(look_keys[dd.get_selected()]))
        row.append(look_dd)
        card.append(row)

        row = Gtk.Box(spacing=8)
        row.append(_label("When closing the window", hexpand=True))
        self.close_dd = Gtk.DropDown.new_from_strings([text for _, text in CLOSE_ACTIONS])
        keys = [key for key, _ in CLOSE_ACTIONS]
        self.close_dd.set_selected(keys.index(self.settings.get("close_action", "tray")))
        self.close_dd.connect("notify::selected", lambda dd, _p: self._set_setting("close_action", keys[dd.get_selected()]))
        row.append(self.close_dd)
        card.append(row)

        row = Gtk.Box(spacing=8)
        row.append(_label("Tray icon outline", hexpand=True,
                          tooltip_text="Match system follows light/dark mode. Pick one if your panel or bar doesn't follow it."))
        outline_dd = Gtk.DropDown.new_from_strings([text for _, text in TRAY_OUTLINES])
        outline_keys = [key for key, _ in TRAY_OUTLINES]
        outline_dd.set_selected(outline_keys.index(self.settings.get("tray_outline", "auto")))
        outline_dd.connect("notify::selected", lambda dd, _p: self._set_outline(outline_keys[dd.get_selected()]))
        row.append(outline_dd)
        card.append(row)

        self.autostart_chk = Gtk.CheckButton(label="Start at login (in the tray)", active=setupcheck.autostart_enabled())
        self.autostart_chk.connect("toggled", lambda b: self._set_autostart(b.get_active()))
        # Bare compositors ignore the autostart folder; the setup check handles them instead.
        self.autostart_chk.set_visible(setupcheck.runs_xdg_autostart())
        card.append(self.autostart_chk)

        self.tray_note = _label("", ["dim"], wrap=True)
        card.append(self.tray_note)

        quit_btn = Gtk.Button(label="Quit G PRO Control", halign=Gtk.Align.START)
        quit_btn.connect("clicked", lambda *_: self.get_application().quit())
        card.append(quit_btn)
        self.update_tray_note()
        return card

    def update_tray_note(self):
        if self.get_application().tray_available():
            self.tray_note.set_text("Quit from the tray icon's menu, this button, or Ctrl+Q.")
        else:
            self.tray_note.set_text("No system tray found, so closing the window quits the app.")

    def _set_appearance(self, value):
        self._set_setting("appearance", value)
        self.get_application().apply_appearance(value)

    def _set_outline(self, value):
        self._set_setting("tray_outline", value)
        self.get_application().set_tray_outline(value)

    def _set_setting(self, key, value):
        self.settings[key] = value
        self.backend.set_setting(key, value)

    def _set_autostart(self, enabled):
        if self._syncing_autostart:
            return
        setupcheck.set_autostart(enabled)
        self.run_setup_check()

    # -- alerts -------------------------------------------------------------

    def _build_alerts(self, settings):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card"])
        card.append(_label("Alerts", ["section"]))
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        card.append(grid)
        self.alert_widgets = {}
        self._alerts_timer = 0

        rows = [
            ("low", "Low battery", "Alert when the battery drops to this level while unplugged", 5, 50),
            ("charge", "Charge limit", "Alert when charging reaches this level, so you can unplug", 50, 100),
        ]
        for n, (kind, title, tip, lo, hi) in enumerate(rows):
            s = settings[kind]
            on = Gtk.CheckButton(label=title, active=s["enabled"], hexpand=True, tooltip_text=tip)
            at = Gtk.SpinButton.new_with_range(lo, hi, 1)
            at.set_value(s["at"])
            early = Gtk.CheckButton(label="Heads-up first at", active=s["early"], margin_start=24,
                                    tooltip_text="One earlier reminder before the real alert")
            early_at = Gtk.SpinButton.new_with_range(1, 100, 1)
            early_at.set_value(s["early_at"])

            grid.attach(on, 0, n * 2, 1, 1)
            grid.attach(at, 1, n * 2, 1, 1)
            grid.attach(_label("%"), 2, n * 2, 1, 1)
            grid.attach(early, 0, n * 2 + 1, 1, 1)
            grid.attach(early_at, 1, n * 2 + 1, 1, 1)
            grid.attach(_label("%"), 2, n * 2 + 1, 1, 1)

            self.alert_widgets[kind] = (on, at, early, early_at)
            for w in (on, early):
                w.connect("toggled", self._on_alert_changed)
            for w in (at, early_at):
                w.connect("value-changed", self._on_alert_changed)
        self._constrain_alerts()

        foot = Gtk.Box(spacing=8)
        self.alert_mode_lbl = _label("", ["dim"], hexpand=True, wrap=True)
        foot.append(self.alert_mode_lbl)
        test = Gtk.Button(label="Test", tooltip_text="Send a test notification")
        test.connect("clicked", lambda *_: self._test_notification())
        foot.append(test)
        card.append(foot)
        return card

    def _test_notification(self):
        if notify("test", "G PRO Control", "Notifications are working."):
            self.msg_lbl.set_text("Test notification sent.")
        else:
            self.msg_lbl.set_text("No notification service is running, so alerts can't be shown. "
                                  "Install one, e.g. mako, dunst or swaync.")

    def _constrain_alerts(self):
        # A heads-up has to come before the alert: above the low level, below the charge limit.
        for kind, (on, at, early, early_at) in self.alert_widgets.items():
            if kind == "low":
                early_at.set_range(at.get_value() + 1, 99)
            else:
                early_at.set_range(1, at.get_value() - 1)
            early.set_sensitive(on.get_active())
            early_at.set_sensitive(on.get_active() and early.get_active())
            at.set_sensitive(on.get_active())

    def _on_alert_changed(self, *_):
        self._constrain_alerts()
        # Save shortly after the last change rather than on every spin click.
        if self._alerts_timer:
            GLib.source_remove(self._alerts_timer)
        self._alerts_timer = GLib.timeout_add(600, self._save_alerts)

    def _save_alerts(self):
        self._alerts_timer = 0
        self.backend.set_alerts({
            kind: {"enabled": on.get_active(), "at": int(at.get_value()),
                   "early": early.get_active(), "early_at": int(early_at.get_value())}
            for kind, (on, at, early, early_at) in self.alert_widgets.items()
        })
        return False

    # -- DPI widgets ------------------------------------------------------

    def _to_pos(self, dpi):
        lo, hi = math.log(self.dpi_info.minimum), math.log(self.dpi_info.maximum)
        return (math.log(max(dpi, 1)) - lo) / (hi - lo) * SCALE_STEPS if hi > lo else 0

    def _from_pos(self, pos):
        lo, hi = math.log(self.dpi_info.minimum), math.log(self.dpi_info.maximum)
        return self.dpi_info.snap(math.exp(lo + pos / SCALE_STEPS * (hi - lo)))

    def _show_value(self, value):
        self._syncing = True
        self.scale.set_value(self._to_pos(value))
        self.spin.set_value(value)
        self._spin_prev = value
        self._syncing = False

    def _setup_dpi(self, dpi):
        self._syncing = True
        self.scale.set_adjustment(Gtk.Adjustment(lower=0, upper=SCALE_STEPS, step_increment=1, page_increment=50))
        self.spin.set_adjustment(Gtk.Adjustment(lower=dpi.minimum, upper=dpi.maximum,
                                                step_increment=1, page_increment=100))
        self.scale.clear_marks()
        presets = [p for p in PRESETS if dpi.minimum <= p <= dpi.maximum]
        for p in presets:
            self.scale.add_mark(self._to_pos(p), Gtk.PositionType.BOTTOM, None)

        while child := self.presets.get_first_child():
            self.presets.remove(child)
        for p in presets:
            b = Gtk.Button(label=str(p))
            b.connect("clicked", lambda _b, v=p: self._apply(v))
            self.presets.append(b)
        self._syncing = False

    def _on_scale(self, scale):
        if self._syncing or not self.dpi_info:
            return
        value = self._from_pos(scale.get_value())
        self._syncing = True
        self.spin.set_value(value)
        self._spin_prev = value
        self._syncing = False

    def _on_spin(self, spin):
        if self._syncing or not self.dpi_info:
            return
        new, prev, choices = spin.get_value(), self._spin_prev, self.dpi_info.choices
        if choices and abs(new - prev) == 1:
            # +/- buttons: move to the neighbouring value the sensor accepts.
            i = bisect.bisect_right(choices, prev) if new > prev else bisect.bisect_left(choices, prev) - 1
            value = choices[max(0, min(i, len(choices) - 1))]
        else:
            value = self.dpi_info.snap(new)
        self._show_value(value)

    def _apply(self, value=None):
        if not self.dpi_info:
            return
        value = self.dpi_info.snap(value if value is not None else self.spin.get_value())
        self.msg_lbl.set_text(f"Setting {value} DPI…")
        self.backend.set_dpi(int(value))

    # -- state from backend -------------------------------------------------

    def _update(self, s: Snapshot):
        if s.connected:
            self.name_lbl.set_text(s.name or "Logitech mouse")
            self.conn_lbl.set_text("Wireless (via receiver)" if s.wireless else "Connected by USB cable")
        elif s.battery:
            self.name_lbl.set_text(s.name or "Logitech mouse")
            self.conn_lbl.set_text("Battery from kernel driver")
        else:
            self.name_lbl.set_text("No mouse found")
            self.conn_lbl.set_text("Waiting for the mouse or its receiver…")

        b = s.battery
        if b and b.percent is not None:
            self.pct_lbl.set_text(f"{'~' if b.approximate else ''}{b.percent}%")
            self.level.set_value(b.percent)
        else:
            self.pct_lbl.set_text("–")
            self.level.set_value(0)
        self.state_lbl.set_text(STATUS_TEXT.get(b.status, b.status.capitalize()) if b else "")

        extra = []
        if b and b.voltage_mv:
            extra.append(f"{b.voltage_mv / 1000:.2f} V")
        if b and b.level:
            extra.append(f"level: {b.level}")
        self.extra_lbl.set_text("  ·  ".join(extra))
        self.extra_lbl.set_visible(bool(extra))

        dpi = s.dpi
        self.dpi_card.set_sensitive(dpi is not None)
        if dpi:
            changed = (not self.dpi_info or (dpi.minimum, dpi.maximum, dpi.step)
                       != (self.dpi_info.minimum, self.dpi_info.maximum, self.dpi_info.step))
            current_changed = not self.dpi_info or self.dpi_info.current != dpi.current
            self.dpi_info = dpi
            if changed:
                self._setup_dpi(dpi)
            if current_changed or changed:
                self._show_value(dpi.current)
            self.cur_dpi_lbl.set_text(f"current: {dpi.current}  ·  range {dpi.minimum}–{dpi.maximum}")
            self.mode_lbl.set_text({
                True: "Onboard profile active (applying a DPI switches to software control)",
                False: "Software control (host mode)",
            }.get(dpi.onboard, ""))
            self.onboard_btn.set_visible(dpi.onboard is False)
        else:
            self.cur_dpi_lbl.set_text("")

        self.alert_mode_lbl.set_text(ALERT_MODE_TEXT.get(s.alert_mode, ""))

        if s.permission_denied:
            self.msg_lbl.set_text(UDEV_HINT)
        elif s.connected and not dpi:
            self.msg_lbl.set_text("This device doesn't support DPI changes.")
        else:
            self.msg_lbl.set_text(s.message)
        self.get_application().update_tray(s)
        return False


class App(Gtk.Application):
    def __init__(self, hidden=False):
        super().__init__(application_id=APP_ID)
        self.start_hidden = hidden
        self.window = None
        self.tray = None
        self.system_scheme = 0  # portal color-scheme: 0 no preference, 1 dark, 2 light
        self.tray_outline = load_config().get("tray_outline", "auto")
        self._last_snapshot = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        settings = Gtk.Settings.get_default()
        self._system_look = (settings.props.gtk_theme_name, settings.props.gtk_interface_color_scheme)
        self.apply_appearance(load_config().get("appearance", "system"))

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        try:
            self.tray = TrayIcon(APP_ID, "G PRO Control", self._on_tray_click, self._on_tray_available)
            self._watch_color_scheme()
            self.tray.set_icon_pixmaps(None, icons.tray_pixmaps(None, fg=self.tray_fg))
        except GLib.Error:
            self.tray = None
        Gtk.Window.set_default_icon_name(icons.ICON_NAME)
        self.hold()  # keep running while the window is hidden in the tray

    def do_shutdown(self):
        if self.tray:
            self.tray.close()
        Gtk.Application.do_shutdown(self)

    def do_activate(self):
        if not self.window:
            self.window = Window(self)
            GLib.timeout_add_seconds(3, self._prompt_setup)
            if self.start_hidden:
                self.start_hidden = False
                # The tray registers asynchronously, and at login the panel or bar
                # may start after us; show the window if no tray turns up.
                GLib.timeout_add_seconds(10, self._show_if_no_tray)
                return
        self.window.present()

    # The tray icon is monochrome like the rest of the panel: light lines on a
    # dark theme, dark lines on a light one. Follow the desktop's setting live.
    PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Settings")

    def _watch_color_scheme(self):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        try:
            reply = bus.call_sync(*self.PORTAL, "ReadOne", GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
                                  None, Gio.DBusCallFlags.NONE, 2000, None)
            self._set_color_scheme(reply.unpack()[0])
        except GLib.Error:
            pass
        bus.signal_subscribe(self.PORTAL[0], self.PORTAL[2], "SettingChanged", self.PORTAL[1], None,
                             Gio.DBusSignalFlags.NONE, self._on_setting_changed)

    def _on_setting_changed(self, _conn, _sender, _path, _iface, _signal, params):
        namespace, key, value = params.unpack()
        if (namespace, key) == ("org.freedesktop.appearance", "color-scheme"):
            self._set_color_scheme(value)
            self._refresh_tray_icon()

    def _set_color_scheme(self, value):
        self.system_scheme = value

    @property
    def tray_fg(self):
        # Without a preference (or without a portal, common on bare compositors)
        # assume a dark panel; Settings lets the user force either.
        if self.tray_outline == "light":
            return icons.LIGHT_FG
        if self.tray_outline == "dark":
            return icons.DARK_FG
        return icons.DARK_FG if self.system_scheme == 2 else icons.LIGHT_FG

    def set_tray_outline(self, value):
        self.tray_outline = value
        self._refresh_tray_icon()

    def _refresh_tray_icon(self):
        if not self.tray:
            return
        if self._last_snapshot:
            self.update_tray(self._last_snapshot)
        else:
            self.tray.set_icon_pixmaps(("none", self.tray_fg), icons.tray_pixmaps(None, fg=self.tray_fg))

    def apply_appearance(self, mode):
        """Force this app light or dark, or go back to the desktop's setting."""
        settings = Gtk.Settings.get_default()
        theme, scheme = self._system_look
        if mode in ("light", "dark"):
            dark = mode == "dark"
            # Use the theme's base variant (Breeze-Dark -> Breeze) so both modes exist;
            # fall back to a separate "-Dark" theme when the base has no dark stylesheet.
            base = re.sub(r"[-_ ]?dark$", "", theme or "", flags=re.IGNORECASE)
            if base and _theme_has(base):
                theme = base
            if dark and not _theme_has(theme, "gtk-dark.css") and _theme_has(f"{base}-Dark"):
                theme = f"{base}-Dark"
            scheme = Gtk.InterfaceColorScheme.DARK if dark else Gtk.InterfaceColorScheme.LIGHT
        # gtk-interface-color-scheme (GTK 4.20+) replaces the deprecated
        # gtk-application-prefer-dark-theme; setting both makes them fight.
        settings.props.gtk_theme_name = theme
        settings.props.gtk_interface_color_scheme = scheme

        display = Gdk.Display.get_default()
        if getattr(self, "_palette_provider", None):
            Gtk.StyleContext.remove_provider_for_display(display, self._palette_provider)
            self._palette_provider = None
        css = theme_palette_css(theme, mode == "dark") if mode in ("light", "dark") else ""
        if css:
            self._palette_provider = Gtk.CssProvider()
            self._palette_provider.load_from_string(css)
            Gtk.StyleContext.add_provider_for_display(display, self._palette_provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

    def tray_available(self):
        return bool(self.tray and self.tray.available)

    def _prompt_setup(self):
        """Open the setup check once for each new set of problems (first run,
        or after switching desktop), not every launch."""
        w = self.window
        w.run_setup_check()
        problems = sorted(c.key for c in w.setup_checks if c.state == setupcheck.WARN)
        seen = f"{':'.join(setupcheck.desktop_names())}/{','.join(problems)}"
        if problems and w.settings.get("setup_prompted") != seen:
            w.stack.set_visible_child_name("settings")
            w.present()
            w.msg_lbl.set_text("A few things need setting up on this desktop. See Setup check above.")
            w._set_setting("setup_prompted", seen)
        return False

    def _show_if_no_tray(self):
        if not self.tray_available():
            self.window.present()
        return False

    def _on_tray_click(self):
        if self.window:
            self.window.toggle()

    def _on_tray_available(self, available):
        if self.window:
            self.window.update_tray_note()
            self.window.run_setup_check()
            if not available and not self.window.is_visible():
                self.window.present()  # never leave the app running with no way back in

    def _open(self):
        if self.window:
            self.window.present()

    def update_tray(self, s):
        if not self.tray:
            return
        self._last_snapshot = s
        b, title = s.battery, s.name or "G PRO Control"
        lines = []
        if b and b.percent is not None:
            key = (b.percent, b.charging, self.tray_fg)
            self.tray.set_icon_pixmaps(key, icons.tray_pixmaps(*key))
            battery_text = f"{b.percent}% · {STATUS_TEXT.get(b.status, b.status)}"
            lines.append(battery_text)
        else:
            self.tray.set_icon_pixmaps(("none", self.tray_fg), icons.tray_pixmaps(None, fg=self.tray_fg))
            battery_text = "not available"
        if s.dpi:
            lines.append(f"{s.dpi.current} DPI")
        self.tray.set_tooltip(title, "\n".join(lines) or "Looking for your mouse…")
        self.tray.set_menu([
            MenuItem("Open G PRO Control", self._open),
            MenuItem(separator=True),
            MenuItem(f"Battery: {battery_text}", enabled=False),
            MenuItem(f"DPI: {s.dpi.current}" if s.dpi else "DPI: –", enabled=False),
            MenuItem(separator=True),
            MenuItem("Quit", self.quit),
        ])


def run(hidden=False):
    return App(hidden).run(None)
