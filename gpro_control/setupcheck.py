"""Setup check: what this desktop is missing, and one-click fixes.

Each check looks at the session the app is running in: the tray, desktop
notifications, start at login and access to the mouse. Where something is
missing it offers a fix. Fixes that edit a config file make a backup first;
fixes that need root go through pkexec (the desktop's password prompt), or
a terminal with sudo when no polkit agent is running.
"""

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

from . import hidpp

HOME = os.path.expanduser("~")
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
AUTOSTART_FILE = os.path.join(CONFIG_HOME, "autostart", "gpro-control.desktop")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APPINDICATOR_UUID = "appindicatorsupport@rgcjonas.gmail.com"
NOTIFICATION_PACKAGE = "dunst"  # D-Bus activated, works on X11 and Wayland

# Desktops that run ~/.config/autostart entries at login. Bare compositors and
# window managers usually don't (unless started through uwsm or dex).
XDG_AUTOSTART_DESKTOPS = {"kde", "gnome", "xfce", "lxqt", "lxde", "mate", "cinnamon", "x-cinnamon", "budgie",
                          "deepin", "pantheon", "unity", "cosmic", "enlightenment", "trinity"}

OK, WARN, INFO = "ok", "warn", "info"


@dataclass
class Check:
    key: str
    title: str
    state: str  # OK, WARN (something is missing) or INFO (a choice, not a problem)
    detail: str
    fix_label: str = ""  # no label: nothing we can fix automatically
    fix: Callable[[], tuple] | None = None  # runs off the UI thread; returns (ok, message)


# --------------------------------------------------------------------------
# Session info
# --------------------------------------------------------------------------


def desktop_names():
    """XDG_CURRENT_DESKTOP components, as reported (e.g. ["Hyprland"])."""
    return [d for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d]


def runs_xdg_autostart():
    desktops = [d.lower() for d in desktop_names()]
    return (any(d in XDG_AUTOSTART_DESKTOPS or d.startswith("budgie") for d in desktops)
            or bool(os.environ.get("UWSM_FINALIZE_VARNAMES")))


def launch_command():
    """How to start the app hidden: the installed launcher, or this checkout's."""
    if shutil.which("gpro-control"):
        return "gpro-control --hidden"
    return f"{shlex.quote(os.path.join(PROJECT_DIR, 'gpro-control'))} --hidden"


def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _dbus_call(method, args=None, reply="(as)"):
    return _bus().call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", method,
                            args, GLib.VariantType(reply), Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]


def short(path):
    return "~" + path[len(HOME):] if path.startswith(HOME + os.sep) else path


def _backup(path):
    backup = f"{path}.gpro-control-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(path, backup)
    return backup


# --------------------------------------------------------------------------
# Running things as root
# --------------------------------------------------------------------------

TERMINALS = [  # (binary, args that run the rest of the command line)
    ("xdg-terminal-exec", []), ("kitty", []), ("foot", []), ("alacritty", ["-e"]), ("wezterm", ["start", "--"]),
    ("ghostty", ["-e"]), ("konsole", ["-e"]), ("gnome-terminal", ["--"]), ("kgx", ["--"]),
    ("xfce4-terminal", ["-x"]), ("terminator", ["-x"]), ("xterm", ["-e"]),
]


def find_terminal():
    preferred = os.environ.get("TERMINAL")
    for name, args in ([(preferred, ["-e"])] if preferred else []) + TERMINALS:
        if name and shutil.which(name):
            return [name, *args]
    return None


def run_as_root(cmd):
    """Run `cmd` as root: pkexec first, a terminal with sudo as a fallback.
    Returns (ok, message); ok is None when it's now up to the user in a terminal."""
    if shutil.which("pkexec"):
        r = subprocess.run(["pkexec", *cmd], capture_output=True, text=True)
        if r.returncode == 0:
            return True, ""
        if r.returncode == 126:
            return False, "Cancelled."
        # 127: no polkit agent running (common on bare compositors), or not authorised.
    line = shlex.join(["sudo", *cmd])
    terminal = find_terminal()
    if terminal:
        script = f'{line}; echo; read -rp "Done. Press Enter to close this window." _'
        try:
            subprocess.Popen([*terminal, "sh", "-c", script], start_new_session=True)
            return None, "Finish in the terminal that just opened, then press Check again."
        except OSError:
            pass
    return False, f"Run this in a terminal, then press Check again:\n{line}"


def install_package(pkg):
    return run_as_root(["pacman", "-S", "--needed", "--noconfirm", pkg])


# --------------------------------------------------------------------------
# JSON with comments (Waybar's config format)
# --------------------------------------------------------------------------


def _comment_spans(text):
    """(start, end) of every comment, skipping ones that are inside strings."""
    spans, i, n = [], 0, len(text)
    while i < n:
        if text[i] == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            spans.append((i, end))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            spans.append((i, end))
            i = end
        else:
            i += 1
    return spans


def _search_code(pattern, text):
    """First regex match that isn't inside a comment."""
    spans = _comment_spans(text)
    for m in re.finditer(pattern, text):
        if not any(a <= m.start() < b for a, b in spans):
            return m
    return None


def parse_jsonc(text):
    """Parse JSON that may contain // and /* */ comments and trailing commas."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            i = text.find("*/", i + 2)
            i = n if i < 0 else i + 2
        else:
            out.append(c)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


# --------------------------------------------------------------------------
# Tray
# --------------------------------------------------------------------------


def waybar_config_path():
    """The Waybar config Waybar would load, or None if Waybar isn't set up."""
    for d in (os.path.join(CONFIG_HOME, "waybar"), "/etc/xdg/waybar"):
        for name in ("config.jsonc", "config", "config.json"):
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


def _bars(data):
    return data if isinstance(data, list) else [data]


def waybar_has_tray_text(text):
    data = parse_jsonc(text)
    return any("tray" in (bar.get(k) or []) for bar in _bars(data) if isinstance(bar, dict)
               for k in ("modules-left", "modules-center", "modules-right"))


def waybar_has_tray(path):
    try:
        with open(path) as f:
            return waybar_has_tray_text(f.read())
    except (OSError, ValueError):
        return None  # can't tell


def add_waybar_tray(path):
    """Put "tray" at the start of the first bar's modules-right, then reload Waybar."""
    if not path.startswith(CONFIG_HOME):
        # Only a system default exists: give the user their own copy to edit.
        user_path = os.path.join(CONFIG_HOME, "waybar", os.path.basename(path))
        os.makedirs(os.path.dirname(user_path), exist_ok=True)
        shutil.copy2(path, user_path)
        for extra in glob.glob(os.path.join(os.path.dirname(path), "style.css")):
            if not os.path.exists(os.path.join(os.path.dirname(user_path), "style.css")):
                shutil.copy2(extra, os.path.dirname(user_path))
        path = user_path
    with open(path) as f:
        text = f.read()
    try:
        parse_jsonc(text)
    except ValueError as e:
        return False, f"Couldn't read {short(path)}: {e}"

    m = _search_code(r'"modules-right"\s*:\s*\[', text)
    if m:
        rest = text[m.end():]
        sep = "" if rest.lstrip().startswith("]") else ", "
        new = text[: m.end()] + f'"tray"{sep}' + rest
    else:
        brace = _search_code(r"\{", text).end()  # first bar object
        after = text[brace:]
        if after.startswith("\n"):
            indent = re.match(r"\n([ \t]*)", after).group(1) or "    "
            entry = f'\n{indent}"modules-right": ["tray"],'
        else:
            entry = ' "modules-right": ["tray"],'
        new = text[:brace] + entry + after

    # Only write what still parses and now has the tray.
    try:
        ok = waybar_has_tray_text(new)
    except ValueError:
        ok = False
    if not ok:
        return False, f"Couldn't add the tray safely. Add \"tray\" to a modules list in {short(path)} yourself."
    backup = _backup(path)
    with open(path, "w") as f:
        f.write(new)
    subprocess.run(["pkill", "-SIGUSR2", "-x", "waybar"], capture_output=True)  # reload config
    return True, f"Added to {short(path)} (backup: {os.path.basename(backup)}). Waybar reloads on its own."


def _gnome_extension_installed():
    dirs = ["/usr/share/gnome-shell/extensions", os.path.join(HOME, ".local/share/gnome-shell/extensions")]
    return any(os.path.isdir(os.path.join(d, APPINDICATOR_UUID)) for d in dirs)


def fix_gnome_tray():
    if not _gnome_extension_installed():
        ok, msg = install_package("gnome-shell-extension-appindicator")
        if not ok:
            return ok, msg
    subprocess.run(["gnome-extensions", "enable", APPINDICATOR_UUID], capture_output=True)
    return True, "Enabled. If the icon doesn't appear, log out and back in once."


def check_tray(tray_available):
    title = "Tray icon"
    if tray_available:
        return Check("tray", title, OK, "Your panel shows the tray icon.")
    desktops = [d.lower() for d in desktop_names()]
    if "gnome" in desktops:
        return Check("tray", title, WARN, "GNOME needs the AppIndicator extension to show tray icons.",
                     "Install and enable", fix_gnome_tray)
    wb = waybar_config_path()
    if wb:
        has = waybar_has_tray(wb)
        if has is False:
            return Check("tray", title, WARN, "Waybar's tray module is turned off.",
                         "Add tray to Waybar", lambda: add_waybar_tray(wb))
        if has:
            if subprocess.run(["pgrep", "-x", "waybar"], capture_output=True).returncode != 0:
                return Check("tray", title, WARN, "Waybar has a tray, but Waybar isn't running.")
            return Check("tray", title, WARN, "Waybar's tray is on but hasn't shown up yet. "
                         "Press Check again in a moment, or restart Waybar.")
    return Check("tray", title, WARN, "Your panel has no system tray (StatusNotifierItem), "
                 "so closing the window quits the app.")


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


def notifications_available():
    name = "org.freedesktop.Notifications"
    try:
        if _dbus_call("NameHasOwner", GLib.Variant("(s)", (name,)), "(b)"):
            return True
        return name in _dbus_call("ListActivatableNames")
    except GLib.Error:
        return False


def fix_notifications():
    ok, msg = install_package(NOTIFICATION_PACKAGE)
    if not ok:
        return ok, msg
    try:  # make the bus notice the new D-Bus service file
        _bus().call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReloadConfig",
                         None, None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error:
        pass
    return True, f"Installed {NOTIFICATION_PACKAGE}. Press Test on the Alerts page to try it."


def check_notifications():
    title = "Alert notifications"
    if notifications_available():
        return Check("notify", title, OK, "Desktop notifications work.")
    return Check("notify", title, WARN, "No notification service is installed, so alerts can't pop up.",
                 f"Install {NOTIFICATION_PACKAGE}", fix_notifications)


# --------------------------------------------------------------------------
# Start at login
# --------------------------------------------------------------------------


def autostart_enabled():
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled):
    if not enabled:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
    with open(AUTOSTART_FILE, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=G PRO Control\n"
                f"Exec={launch_command()}\nIcon=io.github.gpro_control\nX-GNOME-Autostart-enabled=true\n")


def _niri_line(cmd):
    return "spawn-at-startup " + " ".join(json.dumps(a) for a in shlex.split(cmd))


# name -> (config files relative to ~/.config or ~, how to write the startup line)
COMPOSITORS = {
    "hyprland": (["hypr/hyprland.conf"], lambda c: f"exec-once = {c}"),
    "sway": (["sway/config", "~/.sway/config"], lambda c: f"exec {c}"),
    "i3": (["i3/config", "~/.i3/config"], lambda c: f"exec --no-startup-id {c}"),
    "niri": (["niri/config.kdl"], _niri_line),
    "river": (["river/init"], lambda c: f"riverctl spawn '{c}'"),
    "wayfire": (["wayfire.ini"], lambda c: f"gpro_control = {c}"),
}


def compositor_config(name):
    paths, _ = COMPOSITORS[name]
    for p in paths:
        path = os.path.expanduser(p) if p.startswith("~") else os.path.join(CONFIG_HOME, p)
        if os.path.isfile(path):
            return path
    return None


def add_to_compositor(name, path):
    _, make_line = COMPOSITORS[name]
    line = make_line(launch_command())
    with open(path) as f:
        text = f.read()
    if "gpro-control" in text:
        return True, "Already there."
    if name == "wayfire":
        if re.search(r"^\[autostart\]\s*$", text, re.M):
            new = re.sub(r"^\[autostart\]\s*$", lambda m: m.group(0) + "\n" + line, text, count=1, flags=re.M)
        else:
            new = text.rstrip("\n") + f"\n\n[autostart]\n{line}\n"
    else:
        new = text.rstrip("\n") + f"\n\n# G PRO Control: start in the tray at login\n{line}\n"
    backup = _backup(path)
    with open(path, "w") as f:
        f.write(new)
    return True, f"Added to {short(path)} (backup: {os.path.basename(backup)})."


def check_autostart():
    title = "Start at login"
    if runs_xdg_autostart():
        if autostart_enabled():
            return Check("autostart", title, OK, "Starts in the tray when you log in.")
        return Check("autostart", title, INFO, "Off. The app won't start when you log in.",
                     "Turn on", lambda: (set_autostart(True), (True, "Turned on."))[1])
    names = desktop_names()
    key = names[0].lower() if names else ""
    if key in COMPOSITORS:
        path = compositor_config(key)
        if path:
            with open(path) as f:
                if "gpro-control" in f.read():
                    return Check("autostart", title, OK, f"Starts from your {names[0]} config at login.")
            return Check("autostart", title, INFO, f"{names[0]} doesn't start apps from the autostart folder.",
                         f"Add to {names[0]} config", lambda: add_to_compositor(key, path))
        line = COMPOSITORS[key][1](launch_command())
        return Check("autostart", title, INFO, f"No {names[0]} config file found. To start at login, add:\n{line}")
    return Check("autostart", title, INFO, "Your desktop may not run login apps by itself. "
                 f"To start at login, add this to its startup:\n{launch_command()}")


# --------------------------------------------------------------------------
# Mouse access
# --------------------------------------------------------------------------


def _rule_source():
    for path in ("/usr/lib/udev/rules.d/60-gpro-control.rules", os.path.join(PROJECT_DIR, "60-gpro-control.rules")):
        if os.path.isfile(path):
            return path
    return None


def fix_mouse_access():
    src = _rule_source()
    if not src:
        return False, "The udev rule file is missing; reinstall the package."
    script = (f"install -Dm644 {shlex.quote(src)} /etc/udev/rules.d/60-gpro-control.rules && "
              "udevadm control --reload && udevadm trigger --subsystem-match=hidraw")
    ok, msg = run_as_root(["sh", "-c", script])
    return ok, msg or "Done. If DPI still can't be changed, unplug and replug the receiver."


def check_mouse_access():
    title = "Mouse access"
    nodes = hidpp.find_hidpp_nodes()
    if not nodes:
        return Check("access", title, INFO, "No Logitech receiver or mouse is plugged in.")
    if all(os.access(n.path, os.R_OK | os.W_OK) for n in nodes):
        return Check("access", title, OK, "The app can talk to your mouse.")
    return Check("access", title, WARN, "No permission to talk to the mouse, so DPI can't be changed.",
                 "Allow access", fix_mouse_access)


def run_checks(tray_available):
    return [check_mouse_access(), check_tray(tray_available), check_notifications(), check_autostart()]
