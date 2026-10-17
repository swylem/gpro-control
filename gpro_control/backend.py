"""Background worker that owns the mouse and talks to it off the UI thread."""

import queue
import subprocess
import threading
import time
from dataclasses import dataclass

from . import hidpp
from .alerts import SERVICE, AlertMonitor
from .config import alert_settings, load_config, save_config

BATTERY_INTERVAL = 20  # seconds between battery reads
SCAN_INTERVAL = 3  # seconds between scans while no mouse is found


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class Snapshot:
    connected: bool = False
    permission_denied: bool = False
    name: str = ""
    wireless: bool = False
    battery: hidpp.Battery | None = None
    dpi: hidpp.Dpi | None = None
    message: str = ""
    alert_mode: str = ""  # "service": background service, "window": only while open, "off"


class Backend(threading.Thread):
    def __init__(self, on_update):
        super().__init__(daemon=True)
        self.on_update = on_update
        self.commands = queue.Queue()
        self.config = load_config()
        self.device = None
        self.snap = Snapshot()
        # Installed package: alerts run in the background service. From a
        # source checkout there is no unit, so the window checks them itself.
        self.has_service = _systemctl("cat", SERVICE)
        self.window_alerts = AlertMonitor()
        self._service_lock = threading.Lock()

    # -- called from the UI thread ----------------------------------------

    def set_dpi(self, dpi):
        self.commands.put(("set_dpi", dpi))

    def set_restore(self, enabled):
        self.commands.put(("restore", enabled))

    def use_onboard_profile(self):
        self.commands.put(("onboard", None))

    def set_alerts(self, settings):
        self.commands.put(("alerts", settings))

    def refresh(self):
        self.commands.put(("refresh", None))

    # -- worker -------------------------------------------------------------

    def _publish(self):
        self.snap.alert_mode = self._alert_mode()
        self.on_update(Snapshot(**vars(self.snap)))

    def _alert_mode(self):
        settings = alert_settings(self.config)
        if not any(a["enabled"] for a in settings.values()):
            return "off"
        return "service" if self.has_service else "window"

    def _sync_service(self):
        # systemctl can take a few seconds; don't hold up talking to the mouse.
        if self.has_service:
            threading.Thread(target=self._sync_service_now, args=(self._alert_mode(),), daemon=True).start()

    def _sync_service_now(self, mode):
        with self._service_lock:
            if mode == "service":
                _systemctl("enable", "--now", SERVICE)
                _systemctl("try-restart", SERVICE)  # pick up a newly installed version
            else:
                _systemctl("disable", "--now", SERVICE)

    def _check_alerts(self):
        if self._alert_mode() == "window":
            self.window_alerts.check(self.snap.battery, self.snap.name, self.config)

    def _drop_device(self, message=""):
        if self.device:
            self.device.close()
        self.device = None
        self.snap = Snapshot(message=message)

    def _connect(self):
        dev, denied = hidpp.open_mouse()
        if not dev:
            name, battery = hidpp.sysfs_battery()
            self.snap = Snapshot(permission_denied=denied, name=name or "", battery=battery)
            self._check_alerts()
            return False

        self.device = dev
        dev.read_name()
        self.snap = Snapshot(connected=True, name=dev.name, wireless=dev.wireless)
        self.snap.dpi = dev.read_dpi()

        saved = self.config.get("dpi")
        dpi = self.snap.dpi
        if self.config.get("restore", True) and saved and dpi and (dpi.current != saved or dpi.onboard):
            dev.set_dpi(self.snap.dpi.snap(saved))
            self.snap.dpi = dev.read_dpi()
            self.snap.message = f"Restored {self.snap.dpi.current} DPI"
        return True

    def _read_battery(self):
        battery = self.device.read_battery()
        if battery is None:
            # Device has no HID++ battery feature we know; try the kernel's view.
            _, battery = hidpp.sysfs_battery()
        self.snap.battery = battery
        self._check_alerts()

    def _handle(self, cmd, arg):
        if cmd == "restore":
            self.config["restore"] = bool(arg)
            save_config(self.config)
            return
        if cmd == "refresh":
            return
        if cmd == "alerts":
            self.config["alerts"] = arg
            save_config(self.config)
            self._sync_service()
            return
        if cmd == "onboard" and self.device:
            self.device.set_onboard_mode(True)
            self.snap.dpi = self.device.read_dpi()
            self.config.pop("dpi", None)  # don't fight the profile on reconnect
            save_config(self.config)
            self.snap.message = "Switched back to the mouse's onboard profile"
        if cmd == "set_dpi" and self.device:
            dpi = self.snap.dpi.snap(arg) if self.snap.dpi else arg
            self.device.set_dpi(dpi)
            self.snap.dpi = self.device.read_dpi()
            self.config["dpi"] = self.snap.dpi.current if self.snap.dpi else dpi
            save_config(self.config)
            self.snap.message = f"DPI set to {self.config['dpi']}"

    def run(self):
        self._sync_service()
        next_battery = 0
        while True:
            try:
                if not self.device:
                    if self._connect():
                        next_battery = 0
                    else:
                        self._publish()
                        self._wait(SCAN_INTERVAL)
                        continue

                if time.monotonic() >= next_battery:
                    self._read_battery()
                    next_battery = time.monotonic() + BATTERY_INTERVAL
                self._publish()

                cmd = self._wait(max(0.1, next_battery - time.monotonic()))
                if cmd:
                    self._handle(*cmd)
                    if cmd[0] == "refresh":
                        next_battery = 0
            except hidpp.FeatureError as e:
                if e.hidpp10:  # the receiver says the mouse isn't there
                    self._drop_device("Mouse went to sleep or disconnected")
                else:
                    self.snap.message = f"Mouse rejected the request: {e}"
                self._publish()
            except (hidpp.HidppError, OSError) as e:
                self._drop_device(f"Lost connection: {e}")
                self._publish()
                time.sleep(1)

    def _wait(self, timeout):
        try:
            return self.commands.get(timeout=timeout)
        except queue.Empty:
            return None
