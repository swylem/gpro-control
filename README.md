# G PRO Control

Small GTK4 app for Logitech G PRO wireless mice (G PRO Wireless, PRO X Superlight / Superlight 2 / "PRO X 2") on Arch Linux:

- battery percentage and charging state (charging / on battery / full), as reported by the mouse
- change DPI (slider, exact value, or presets), optionally re-applied when the mouse reconnects

It talks HID++ 2.0 directly over `/dev/hidraw*`, both over the USB cable and through the LIGHTSPEED receiver. Dependencies: `python`, `python-gobject` and `gtk4`, nothing else.

## Install (Arch)

```bash
git clone https://github.com/swylem/gpro-control.git
cd gpro-control
makepkg -si
```

This installs `/usr/bin/gpro-control`, a menu entry, and a udev rule that gives your logged-in user access to Logitech HID devices. After installing, unplug and replug the mouse or receiver once (or run `sudo udevadm trigger`).

## Run from source

```bash
sudo install -Dm644 60-gpro-control.rules /usr/lib/udev/rules.d/60-gpro-control.rules
sudo udevadm control --reload && sudo udevadm trigger
./gpro-control
```

Without the udev rule the app still shows the battery (it reads it from the kernel's `hidpp_battery_*` node), but it can't change DPI.

## CLI

```
gpro-control status        # battery + DPI
gpro-control dpi 1600      # set DPI
gpro-control features      # list HID++ features (debugging)
```

## Notes

- **Onboard profile vs host mode:** out of the box the mouse runs its onboard profile (the one G HUB edits), and in that mode it rejects DPI changes. When you apply a DPI, the app switches the mouse to host (software) mode. Polling rate is not affected. "Use onboard profile" (or `gpro-control onboard`) switches back and restores the profile's DPI.
- **DPI persistence:** after a power cycle the mouse may come back on its onboard profile. While the app is open, "Restore on reconnect" sets your DPI again.
