"""Settings shared by the window and the background alert monitor."""

import copy
import json
import os

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "gpro-control")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_ALERTS = {
    # Fires while on battery once the level drops to `at`; heads-up first at `early_at`.
    "low": {"enabled": True, "at": 20, "early": True, "early_at": 30},
    # Fires while charging once the level reaches `at`; heads-up first at `early_at`.
    "charge": {"enabled": True, "at": 80, "early": True, "early_at": 75},
}


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)  # the monitor may be reading it
    except OSError:
        pass


def alert_settings(cfg):
    """The alert settings from `cfg`, with defaults filled in."""
    out = copy.deepcopy(DEFAULT_ALERTS)
    for kind, values in (cfg.get("alerts") or {}).items():
        if kind in out and isinstance(values, dict):
            out[kind].update(values)
    return out
