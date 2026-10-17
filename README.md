# G PRO Control

Small GTK4 app for Logitech G PRO wireless mice (G PRO Wireless, PRO X Superlight / Superlight 2 / "PRO X 2") on Arch Linux:

- battery percentage and charging state (charging / on battery / full), as reported by the mouse
- change DPI (slider, exact value, or presets), optionally re-applied when the mouse reconnects
- battery alerts as desktop notifications, each with an optional heads-up before it:
  - **Low battery**: on battery and the level drops to X% (default 20%, heads-up at 30%)
  - **Charge limit**: charging and the level reaches X% (default 80%, heads-up at 75%), so you can unplug
- lives in the system tray: the icon shows the battery level, the tooltip shows battery and DPI, and left-click shows or hides the window
- settings: what closing the window does (keep running in the tray, or quit), and start at login

It talks HID++ 2.0 directly over `/dev/hidraw*`, both over the USB cable and through the LIGHTSPEED receiver. Dependencies: `python`, `python-gobject` and `gtk4`, nothing else.

## Install (Arch)

```bash
git clone https://github.com/swylem/gpro-control.git
cd gpro-control
makepkg -si
```

This installs `/usr/bin/gpro-control`, a menu entry, a systemd user service for the alerts, and a udev rule that gives your logged-in user access to Logitech HID devices. After installing, unplug and replug the mouse or receiver once (or run `sudo udevadm trigger`).

## Run from source

```bash
sudo install -Dm644 60-gpro-control.rules /usr/lib/udev/rules.d/60-gpro-control.rules
sudo udevadm control --reload && sudo udevadm trigger
./gpro-control
```

Without the udev rule the app still shows the battery (it reads it from the kernel's `hidpp_battery_*` node), but it can't change DPI.

## CLI

```
gpro-control --hidden      # start in the tray without opening the window
gpro-control status        # battery + DPI
gpro-control dpi 1600      # set DPI
gpro-control onboard       # hand DPI back to the mouse's onboard profile
gpro-control monitor       # run the alerts without a window
gpro-control features      # list HID++ features (debugging)
```

## Notes

- **Onboard profile vs host mode:** out of the box the mouse runs its onboard profile (the one G HUB edits), and in that mode it rejects DPI changes. When you apply a DPI, the app switches the mouse to host (software) mode. Polling rate is not affected. "Use onboard profile" (or `gpro-control onboard`) switches back and restores the profile's DPI.
- **DPI persistence:** after a power cycle the mouse may come back on its onboard profile. While the app is open, "Restore on reconnect" sets your DPI again.
- **Alerts run in the background.** When any alert is on, the app enables `gpro-control-monitor.service` (a systemd user service), which checks the battery every 30 seconds, even with the window closed. Turning every alert off disables it again. Each alert fires once, then re-arms when you plug in (low battery) or unplug (charge limit).
- **Why an alert and not a real charge limit?** The mouse handles charging in its own firmware, and HID++ has no command to stop charging at a set level. Desktop USB ports generally can't switch off their 5 V power from software either. So the charge limit is a reminder to unplug.
- **Tray:** closing the window keeps the app in the tray by default. Quit from the tray menu, with Settings → "Quit G PRO Control", or with Ctrl+Q. If no system tray is running, closing the window always quits, so the app is never left running with no way back in. The tray uses the StatusNotifierItem protocol: KDE Plasma supports it out of the box, and GNOME needs the AppIndicator extension.
