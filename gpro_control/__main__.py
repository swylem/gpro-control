"""gpro-control: GUI by default, plus a tiny CLI for scripts.

  gpro-control                 open the window
  gpro-control status          print battery and DPI
  gpro-control dpi 1600        set DPI (switches the mouse to host mode)
  gpro-control onboard         hand control back to the mouse's onboard profile
  gpro-control monitor         run the battery alerts without a window (systemd service)
  gpro-control features        list HID++ features (debugging)
"""

import sys

from . import hidpp


def _open_or_exit():
    dev, denied = hidpp.open_mouse()
    if dev:
        return dev
    if denied:
        sys.exit("Permission denied on /dev/hidraw*. Install 60-gpro-control.rules (see README) and replug.")
    sys.exit("No Logitech HID++ mouse found.")


def cli(args):
    cmd = args[0]
    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0

    if cmd == "status":
        dev, denied = hidpp.open_mouse()
        if not dev:
            name, bat = hidpp.sysfs_battery()
            if not bat:
                return _open_or_exit()
            print(f"{name} (kernel driver; DPI needs the udev rule)")
            print(f"battery: {bat.percent}%  {bat.status}")
            return 0
        print(f"{dev.read_name()} ({'receiver slot %d' % dev.index if dev.wireless else 'USB cable'})")
        bat = dev.read_battery()
        if bat:
            volts = f"  {bat.voltage_mv} mV" if bat.voltage_mv else ""
            print(f"battery: {'~' if bat.approximate else ''}{bat.percent}%  {bat.status}{volts}")
        dpi = dev.read_dpi()
        if dpi:
            mode = {True: "onboard profile", False: "host mode", None: "no profiles"}[dpi.onboard]
            print(f"dpi: {dpi.current} (default {dpi.default}, range {dpi.minimum}-{dpi.maximum}, {mode})")
        return 0

    if cmd == "dpi" and len(args) == 2:
        dev = _open_or_exit()
        dpi = dev.read_dpi()
        if not dpi:
            sys.exit("This device has no adjustable DPI.")
        value = dpi.snap(int(args[1]))
        dev.set_dpi(value)
        print(f"dpi: {dev.read_dpi().current}")
        return 0

    if cmd == "onboard":
        dev = _open_or_exit()
        if dev.onboard_mode() is None:
            sys.exit("This device has no onboard profiles.")
        dev.set_onboard_mode(True)
        print(f"onboard profile active, dpi: {dev.read_dpi().current}")
        return 0

    if cmd == "monitor":
        from .alerts import run_monitor

        run_monitor()
        return 0

    if cmd == "features":
        dev = _open_or_exit()
        print(f"{dev.read_name()}  HID++ {dev.protocol[0]}.{dev.protocol[1]}  index 0x{dev.index:02x}  {dev.node.path}")
        for idx, fid in dev.list_features():
            print(f"  [{idx:2d}] 0x{fid:04x}")
        return 0

    print(__doc__.strip(), file=sys.stderr)
    return 2


def main():
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    from .app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
