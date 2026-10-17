"""Background worker that owns the mouse and talks to it off the UI thread."""

import json
import os
import queue
import threading
import time
from dataclasses import dataclass

from . import hidpp

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "gpro-control")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

BATTERY_INTERVAL = 20  # seconds between battery reads
SCAN_INTERVAL = 3  # seconds between scans while no mouse is found


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


@dataclass
class Snapshot:
    connected: bool = False
    permission_denied: bool = False
    name: str = ""
    wireless: bool = False
    battery: hidpp.Battery | None = None
    dpi: hidpp.Dpi | None = None
    message: str = ""


class Backend(threading.Thread):
    def __init__(self, on_update):
        super().__init__(daemon=True)
        self.on_update = on_update
        self.commands = queue.Queue()
        self.config = load_config()
        self.device = None
        self.snap = Snapshot()

    # -- called from the UI thread ----------------------------------------

    def set_dpi(self, dpi):
        self.commands.put(("set_dpi", dpi))

    def set_restore(self, enabled):
        self.commands.put(("restore", enabled))

    def use_onboard_profile(self):
        self.commands.put(("onboard", None))

    def refresh(self):
        self.commands.put(("refresh", None))

    # -- worker -------------------------------------------------------------

    def _publish(self):
        self.on_update(Snapshot(**vars(self.snap)))

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

    def _handle(self, cmd, arg):
        if cmd == "restore":
            self.config["restore"] = bool(arg)
            save_config(self.config)
            return
        if cmd == "refresh":
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
