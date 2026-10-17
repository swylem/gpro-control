"""Low battery and charge limit alerts, sent as desktop notifications.

Runs inside the window, or headless as `gpro-control monitor` (the systemd
user service), so the alerts keep working when the window is closed.
"""

import time

from gi.repository import Gio, GLib

from . import hidpp
from .config import alert_settings, load_config

SERVICE = "gpro-control-monitor.service"
MONITOR_INTERVAL = 30  # seconds between battery reads in the background service

PLUGGED = ("charging", "full", "not charging")

_notification_ids = {}


def notify(key, summary, body, critical=False):
    """Show a desktop notification; one per `key`, replacing the previous one.
    Returns False if no notification server could be reached."""
    hints = {
        "urgency": GLib.Variant("y", 2 if critical else 1),
        "desktop-entry": GLib.Variant("s", "gpro-control"),
    }
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)", ("G PRO Control", _notification_ids.get(key, 0), "input-mouse",
                                             summary, body, [], hints, 0 if critical else -1)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error:
        return False
    _notification_ids[key] = reply.unpack()[0]
    return True


class AlertMonitor:
    """Decides when to alert. Each alert (and its heads-up) fires once, then
    re-arms when the mouse is plugged in (low) or unplugged (charge)."""

    def __init__(self, notify=notify):
        self.notify = notify
        self.fired = {"low": set(), "charge": set()}
        self.last_settings = {}

    def check(self, battery, name="", cfg=None):
        if battery is None or battery.percent is None:
            return
        settings = alert_settings(load_config() if cfg is None else cfg)
        name = name or "your mouse"
        pct = battery.percent
        plugged = battery.status in PLUGGED

        for kind, s in settings.items():
            if s != self.last_settings.get(kind):  # thresholds changed: start over
                self.fired[kind].clear()
                self.last_settings[kind] = dict(s)

        low, charge = settings["low"], settings["charge"]

        if plugged:
            self.fired["low"].clear()
        elif battery.status == "discharging" and low["enabled"]:
            if pct <= low["at"]:
                self._fire("low", "main", f"Mouse battery low: {pct}%", f"Plug in {name} soon.", critical=True)
            elif low["early"] and pct <= low["early_at"]:
                self._fire("low", "early", f"Mouse battery at {pct}%",
                           f"Heads-up: the low battery alert comes at {low['at']}%.")

        if not plugged:
            self.fired["charge"].clear()
        elif charge["enabled"]:
            if pct >= charge["at"]:
                self._fire("charge", "main", f"Mouse charged to {pct}%",
                           f"Unplug {name} to stay at your {charge['at']}% charge limit.", critical=True)
            elif charge["early"] and pct >= charge["early_at"]:
                self._fire("charge", "early", f"Mouse battery at {pct}%",
                           f"Heads-up: your {charge['at']}% charge limit is coming up.")

    def _fire(self, kind, stage, summary, body, critical=False):
        if stage in self.fired[kind]:
            return
        if self.notify(kind, summary, body, critical):
            self.fired[kind].add(stage)
            if stage == "main":
                self.fired[kind].add("early")  # don't send a heads-up after the real alert


def read_battery():
    """(name, Battery) from HID++ if we can open the mouse, else the kernel."""
    try:
        dev, _ = hidpp.open_mouse()
    except OSError:
        dev = None
    if dev:
        try:
            battery = dev.read_battery()
            if battery is not None:
                return dev.read_name(), battery
        except (hidpp.HidppError, OSError):
            pass
        finally:
            dev.close()
    return hidpp.sysfs_battery()


def run_monitor():
    hidpp.SWID = 0x0B  # keep our replies apart from an open window's (0x0A)
    monitor = AlertMonitor()
    while True:
        name, battery = read_battery()
        monitor.check(battery, name)
        time.sleep(MONITOR_INTERVAL)
