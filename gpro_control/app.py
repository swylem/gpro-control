"""GTK4 window: battery status and DPI control."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
import bisect  # noqa: E402
import math  # noqa: E402

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .backend import Backend, Snapshot  # noqa: E402
from .estimate import format_duration  # noqa: E402

APP_ID = "io.github.gpro_control"
PRESETS = [400, 800, 1200, 1600, 3200]
SCALE_STEPS = 1000  # the slider is logarithmic: position 0..SCALE_STEPS

CSS = b"""
.card { padding: 16px; border-radius: 12px; background: alpha(currentColor, 0.06); }
.big { font-size: 34px; font-weight: 700; }
.section { font-weight: 700; opacity: 0.8; }
.dim { opacity: 0.65; }
levelbar block.filled.low { background: #e01b24; }
"""

STATUS_TEXT = {
    "charging": "Charging",
    "discharging": "On battery",
    "full": "Fully charged",
    "not charging": "Plugged in, not charging",
    "error": "Battery error",
    "unknown": "Unknown",
}

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

        header = Gtk.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh now")
        refresh.connect("clicked", lambda *_: self.backend.refresh())
        header.pack_end(refresh)
        self.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(18)
        self.set_child(root)

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
        self.time_lbl = _label("", wrap=True)
        self.extra_lbl = _label("", ["dim"])
        bat.append(self.time_lbl)
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
        root.append(self.msg_lbl)

        self.backend = Backend(lambda snap: GLib.idle_add(self._update, snap))
        self.restore_chk.set_active(self.backend.config.get("restore", True))
        self.restore_chk.connect("toggled", lambda b: self.backend.set_restore(b.get_active()))
        self.backend.start()

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

        self.time_lbl.set_text(self._time_text(s))
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

        if s.permission_denied:
            self.msg_lbl.set_text(UDEV_HINT)
        elif s.connected and not dpi:
            self.msg_lbl.set_text("This device doesn't support DPI changes.")
        else:
            self.msg_lbl.set_text(s.message)
        return False

    @staticmethod
    def _time_text(s):
        b = s.battery
        if not b or b.percent is None:
            return ""
        if b.status == "full":
            return "Ready to go"
        if b.status == "not charging":
            return ""
        what = "Time to full" if b.charging else "Time left"
        if s.time_left is None:
            return f"{what}: measuring… (needs ~10 min and a 2% change)"
        txt = f"{what}: ~{format_duration(s.time_left)}"
        return txt + {"measured": "", "learned": " (from earlier use)", "rated": " (based on rated battery life)"}.get(s.time_basis, "")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


def run():
    return App().run(None)
