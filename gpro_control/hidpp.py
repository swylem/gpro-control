"""Minimal HID++ 2.0 client for Logitech mice over Linux hidraw.

No third-party dependencies: devices are discovered through sysfs and spoken
to with plain os.read/os.write on /dev/hidrawN. Works for mice plugged in by
cable (device index 0xFF) and mice paired to a LIGHTSPEED/Unifying/Bolt
receiver (device index 1..6).
"""

import glob
import os
import re
import select
import struct
import time
from dataclasses import dataclass, field

LOGITECH_VID = 0x046D

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
LONG_LEN = 20
SWID = 0x0A  # our software id; the kernel driver uses 0x01

DIRECT_INDEX = 0xFF
RECEIVER_SLOTS = range(1, 7)

F_ROOT = 0x0000
F_DEVICE_NAME = 0x0005
F_BATTERY_STATUS = 0x1000
F_BATTERY_VOLTAGE = 0x1001
F_UNIFIED_BATTERY = 0x1004
F_ADJUSTABLE_DPI = 0x2201
F_EXTENDED_DPI = 0x2202
F_ONBOARD_PROFILES = 0x8100

ERRORS = {
    0x01: "unknown error",
    0x02: "invalid argument",
    0x03: "out of range",
    0x04: "hardware error",
    0x05: "internal error",
    0x06: "invalid feature index",
    0x07: "invalid function",
    0x08: "busy",
    0x09: "unsupported",
}


class HidppError(Exception):
    pass


class Timeout(HidppError):
    pass


class FeatureError(HidppError):
    def __init__(self, code, hidpp10=False):
        self.code = code
        self.hidpp10 = hidpp10
        super().__init__(ERRORS.get(code, f"error 0x{code:02x}"))


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@dataclass
class HidrawNode:
    path: str
    bus: int
    pid: int
    name: str


def _read(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def _has_hidpp_reports(descriptor: bytes) -> bool:
    # Vendor usage page 0xFF00 plus the HID++ long report id 0x11.
    return b"\x06\x00\xff" in descriptor and b"\x85\x11" in descriptor


def find_hidpp_nodes():
    nodes = []
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = _read(os.path.join(sys_path, "device", "uevent"))
            descriptor = _read(os.path.join(sys_path, "device", "report_descriptor"), "rb")
        except OSError:
            continue
        info = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        try:
            bus, vid, pid = (int(x, 16) for x in info["HID_ID"].split(":"))
        except (KeyError, ValueError):
            continue
        if vid != LOGITECH_VID or not _has_hidpp_reports(descriptor):
            continue
        if re.search(r"/input\d+:\d+$", info.get("HID_PHYS", "")):
            # Per-device node made by the receiver driver (logitech-dj): the
            # same mouse is reachable through the receiver node, and probing
            # this one only costs timeouts.
            continue
        nodes.append(HidrawNode("/dev/" + os.path.basename(sys_path), bus, pid, info.get("HID_NAME", "")))
    return nodes


# --------------------------------------------------------------------------
# Data returned to callers
# --------------------------------------------------------------------------


@dataclass
class Battery:
    percent: int | None
    status: str  # discharging | charging | full | not charging | error | unknown
    level: str | None = None  # critical | low | good | full (coarse level, if reported)
    voltage_mv: int | None = None
    approximate: bool = False  # percent derived from voltage or a coarse level
    source: str = ""

    @property
    def charging(self):
        return self.status in ("charging", "full")


@dataclass
class Dpi:
    current: int
    default: int | None
    choices: list = field(default_factory=list)  # every DPI value the sensor accepts, sorted
    onboard: bool | None = None  # True: the mouse's own profile is in charge; None: no onboard profiles

    @property
    def minimum(self):
        return self.choices[0] if self.choices else self.current

    @property
    def maximum(self):
        return self.choices[-1] if self.choices else self.current

    @property
    def step(self):
        if len(self.choices) < 2:
            return 50
        return min(b - a for a, b in zip(self.choices, self.choices[1:]))

    def snap(self, value):
        if not self.choices:
            return value
        return min(self.choices, key=lambda c: abs(c - value))


def parse_dpi_list(raw: bytes):
    """Decode a HID++ DPI list: big-endian u16 values, 0-terminated.

    A value with the top three bits set (0xE000) is a step marker meaning
    "every `step` DPI from the previous value up to the next value".
    """
    values = []
    i = 0
    while i + 1 < len(raw):
        (val,) = struct.unpack(">H", raw[i : i + 2])
        if val == 0:
            break
        if val >> 13 == 0b111 and values and i + 3 < len(raw):
            step = val & 0x1FFF
            (last,) = struct.unpack(">H", raw[i + 2 : i + 4])
            if step:
                values.extend(range(values[-1] + step, last + 1, step))
            i += 4
        else:
            values.append(val)
            i += 2
    return sorted(set(values))


def _dpi_list_terminated(raw: bytes):
    """True once a 0x0000 terminator appears at a value boundary."""
    i = 0
    while i + 1 < len(raw):
        (val,) = struct.unpack(">H", raw[i : i + 2])
        if val == 0:
            return True
        i += 4 if val >> 13 == 0b111 else 2
    return False


# Li-ion voltage (mV) -> percent, from the kernel's hid-logitech-hidpp driver.
_VOLTAGE_CURVE = [
    4186, 4156, 4143, 4133, 4122, 4113, 4103, 4094, 4086, 4075,
    4067, 4059, 4051, 4043, 4035, 4027, 4019, 4011, 4003, 3997,
    3989, 3983, 3976, 3969, 3961, 3955, 3949, 3942, 3935, 3929,
    3922, 3916, 3909, 3902, 3896, 3890, 3883, 3877, 3870, 3865,
    3859, 3853, 3848, 3842, 3837, 3833, 3828, 3824, 3819, 3815,
    3811, 3808, 3804, 3800, 3797, 3793, 3790, 3787, 3784, 3781,
    3778, 3775, 3772, 3770, 3767, 3764, 3762, 3759, 3757, 3754,
    3751, 3748, 3744, 3741, 3737, 3734, 3730, 3726, 3724, 3720,
    3717, 3714, 3710, 3706, 3702, 3697, 3693, 3688, 3683, 3677,
    3671, 3666, 3662, 3658, 3654, 3646, 3633, 3612, 3579, 3537,
]


def voltage_to_percent(mv):
    for i, threshold in enumerate(_VOLTAGE_CURVE):
        if mv >= threshold:
            return 100 - i
    return 0


# --------------------------------------------------------------------------
# Device
# --------------------------------------------------------------------------


class Device:
    def __init__(self, node: HidrawNode, fd: int, index: int, protocol):
        self.node = node
        self.fd = fd
        self.index = index
        self.protocol = protocol
        self._features = {}
        self.name = node.name
        self.wireless = index != DIRECT_INDEX

    # -- low level ---------------------------------------------------------

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def request(self, feature_index, function, params=b"", timeout=1.0):
        return _request(self.fd, self.index, feature_index, function, params, timeout)

    def feature(self, feature_id):
        """Index of a feature on this device, or None if unsupported."""
        if feature_id not in self._features:
            reply = self.request(0x00, 0, struct.pack(">H", feature_id))
            self._features[feature_id] = reply[0] or None
        return self._features[feature_id]

    def call(self, feature_id, function, params=b""):
        idx = self.feature(feature_id)
        if idx is None:
            raise HidppError(f"feature 0x{feature_id:04x} not supported")
        return self.request(idx, function, params)

    def list_features(self):
        out = []
        set_idx = self.feature(0x0001)
        if set_idx is None:
            return out
        count = self.request(set_idx, 0)[0]
        for i in range(1, count + 1):
            r = self.request(set_idx, 1, bytes([i]))
            out.append((i, (r[0] << 8) | r[1]))
        return out

    # -- high level --------------------------------------------------------

    def read_name(self):
        if self.feature(F_DEVICE_NAME) is None:
            return self.name
        length = self.call(F_DEVICE_NAME, 0)[0]
        chars = b""
        while len(chars) < length:
            chars += self.call(F_DEVICE_NAME, 1, bytes([len(chars)]))[: length - len(chars)]
        self.name = chars.rstrip(b"\0").decode("utf-8", "replace").strip() or self.name
        return self.name

    def has_dpi(self):
        return self.feature(F_ADJUSTABLE_DPI) is not None or self.feature(F_EXTENDED_DPI) is not None

    def has_battery(self):
        return any(self.feature(f) is not None for f in (F_UNIFIED_BATTERY, F_BATTERY_STATUS, F_BATTERY_VOLTAGE))

    def read_battery(self) -> Battery | None:
        if self.feature(F_UNIFIED_BATTERY) is not None:
            caps = self.call(F_UNIFIED_BATTERY, 0)
            r = self.call(F_UNIFIED_BATTERY, 1)
            soc, level_bits, charging, external = r[0], r[1], r[2], r[3]
            level = next((name for bit, name in ((8, "full"), (4, "good"), (2, "low"), (1, "critical")) if level_bits & bit), None)
            status = {0: "discharging", 1: "charging", 2: "charging", 3: "full", 4: "error"}.get(charging, "unknown")
            if status == "discharging" and external:
                status = "not charging"
            if caps[1] & 0x02:  # state of charge in percent is supported
                return Battery(soc, status, level, source="unified")
            approx = {"full": 90, "good": 50, "low": 20, "critical": 5}.get(level)
            return Battery(approx, status, level, approximate=True, source="unified")

        if self.feature(F_BATTERY_STATUS) is not None:
            r = self.call(F_BATTERY_STATUS, 0)
            status = {0: "discharging", 1: "charging", 2: "charging", 3: "full", 4: "charging"}.get(r[2], "error")
            return Battery(r[0], status, source="status")

        if self.feature(F_BATTERY_VOLTAGE) is not None:
            r = self.call(F_BATTERY_VOLTAGE, 0)
            mv, flags = (r[0] << 8) | r[1], r[2]
            if flags & 0x80:
                status = {0: "charging", 1: "full", 2: "not charging"}.get(flags & 0x07, "error")
            else:
                status = "discharging"
            return Battery(voltage_to_percent(mv), status, voltage_mv=mv, approximate=True, source="voltage")

        return None

    def read_dpi(self) -> Dpi | None:
        if self.feature(F_ADJUSTABLE_DPI) is not None:
            raw = self.call(F_ADJUSTABLE_DPI, 1, b"\x00")[1:]
            r = self.call(F_ADJUSTABLE_DPI, 2, b"\x00")
            current = (r[1] << 8) | r[2]
            default = (r[3] << 8) | r[4]
            return Dpi(current, default or None, parse_dpi_list(raw), self.onboard_mode())

        if self.feature(F_EXTENDED_DPI) is not None:
            # The list spans several pages of 13 bytes; values can straddle pages.
            raw = b""
            for page in range(32):
                raw += self.call(F_EXTENDED_DPI, 2, bytes([0, 0, page]))[3:]
                if _dpi_list_terminated(raw):
                    break
            r = self.call(F_EXTENDED_DPI, 5, b"\x00")
            return Dpi((r[1] << 8) | r[2], ((r[3] << 8) | r[4]) or None, parse_dpi_list(raw), self.onboard_mode())

        return None

    def onboard_mode(self):
        """True if the onboard profile is active, False in host mode, None if unsupported."""
        if self.feature(F_ONBOARD_PROFILES) is None:
            return None
        return self.call(F_ONBOARD_PROFILES, 2)[0] == 1

    def set_onboard_mode(self, onboard: bool):
        self.call(F_ONBOARD_PROFILES, 1, bytes([1 if onboard else 2]))

    def set_dpi(self, dpi: int):
        # While the onboard profile is active the mouse rejects DPI writes (or
        # overrides them), so hand control to the host first. The polling rate
        # is kept; switching back to onboard mode restores the profile's DPI.
        if self.onboard_mode():
            self.set_onboard_mode(False)
        if self.feature(F_ADJUSTABLE_DPI) is not None:
            self.call(F_ADJUSTABLE_DPI, 3, struct.pack(">BH", 0, dpi))
        elif self.feature(F_EXTENDED_DPI) is not None:
            r = self.call(F_EXTENDED_DPI, 5, b"\x00")
            lod = r[9]
            self.call(F_EXTENDED_DPI, 6, struct.pack(">BHHB", 0, dpi, dpi, lod))
        else:
            raise HidppError("this device has no adjustable DPI")


def _request(fd, index, feature_index, function, params, timeout):
    func_swid = ((function & 0x0F) << 4) | SWID
    msg = bytes([REPORT_LONG, index, feature_index, func_swid]) + bytes(params)
    os.write(fd, msg.ljust(LONG_LEN, b"\0")[:LONG_LEN])

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Timeout("device did not answer")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise Timeout("device did not answer")
        data = os.read(fd, 64)
        if len(data) < 5 or data[0] not in (REPORT_SHORT, REPORT_LONG) or data[1] != index:
            continue
        if data[2] == 0xFF and data[3] == feature_index and data[4] == func_swid:
            raise FeatureError(data[5] if len(data) > 5 else 0)
        if data[2] == 0x8F and data[3] == feature_index and data[4] == func_swid:
            raise FeatureError(data[5] if len(data) > 5 else 0, hidpp10=True)
        if data[2] == feature_index and data[3] == func_swid:
            return data[4:]


def _probe(fd, index, timeout):
    """Return the HID++ protocol version at `index`, or None if nothing answers."""
    try:
        r = _request(fd, index, 0x00, 1, b"\x00\x00\x5a", timeout)
        return (r[0], r[1])
    except FeatureError as e:
        return (1, 0) if e.hidpp10 and e.code == 0x01 else None
    except (Timeout, OSError):
        return None


def open_devices():
    """Find every HID++ 2.0 device reachable right now.

    Returns (devices, permission_denied) where permission_denied is True if
    at least one Logitech HID++ node exists but could not be opened.
    """
    devices = []
    denied = False
    for node in find_hidpp_nodes():
        try:
            fd = os.open(node.path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError:
            denied = True
            continue
        except OSError:
            continue

        found = []
        proto = _probe(fd, DIRECT_INDEX, 0.5)
        if proto and proto[0] >= 2:
            found.append(Device(node, fd, DIRECT_INDEX, proto))
        else:
            # Probably a receiver: look for paired devices that are online.
            for slot in RECEIVER_SLOTS:
                # A sleeping mouse can take ~0.5 s to answer; empty slots fail fast.
                proto = _probe(fd, slot, 1.5)
                if proto and proto[0] >= 2:
                    found.append(Device(node, fd, slot, proto))

        if found:
            devices.extend(found)
        else:
            os.close(fd)
    return devices, denied


def open_mouse():
    """Open the best candidate: a device with adjustable DPI, else one with a battery."""
    devices, denied = open_devices()
    chosen = None
    for want in ("dpi", "battery"):
        for dev in devices:
            try:
                ok = dev.has_dpi() if want == "dpi" else dev.has_battery()
            except HidppError:
                ok = False
            if ok:
                chosen = dev
                break
        if chosen:
            break
    # Several devices may share one receiver fd; only close fds nobody keeps.
    keep_fd = chosen.fd if chosen else None
    for fd in {d.fd for d in devices} - {keep_fd}:
        os.close(fd)
    return chosen, denied


# --------------------------------------------------------------------------
# sysfs fallback (works without the udev rule, battery only)
# --------------------------------------------------------------------------


def sysfs_battery():
    for path in sorted(glob.glob("/sys/class/power_supply/hidpp_battery_*")):
        try:
            status = _read(os.path.join(path, "status")).strip().lower()
            model = _read(os.path.join(path, "model_name")).strip()
            try:
                percent = int(_read(os.path.join(path, "capacity")).strip())
            except (OSError, ValueError):
                percent = None
        except OSError:
            continue
        return model, Battery(percent, status, source="sysfs")
    return None, None
