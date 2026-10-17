"""Battery time-left estimation.

Logitech mice don't report a time remaining, so we measure it: every reading
is compared to the start of the current charge/discharge session, and the
observed %/hour is remembered across runs so the next session has a good
guess straight away. Until anything has been measured we fall back to the
model's rated battery life.
"""

import json
import os
import time

STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "gpro-control")
STATE_FILE = os.path.join(STATE_DIR, "battery.json")

MIN_DELTA_PCT = 2  # need at least this much change before trusting a rate
MIN_ELAPSED_S = 10 * 60
NOISE_PCT = 3  # discharging readings that rise less than this are noise, not a recharge

# Rated battery life in hours (Logitech's figures), matched on the device name.
RATED_HOURS = [
    ("SUPERLIGHT 2", 95),
    ("PRO X 2", 95),
    ("PRO 2", 95),
    ("SUPERLIGHT", 70),
    ("PRO", 60),  # G PRO Wireless
]


def rated_hours(name):
    upper = (name or "").upper()
    for key, hours in RATED_HOURS:
        if key in upper:
            return hours
    return None


class Estimator:
    def __init__(self, path=STATE_FILE):
        self.path = path
        self.state = {"discharge_rate": None, "charge_rate": None, "session": None}
        try:
            with open(path) as f:
                self.state.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def update(self, percent, charging, now=None):
        """Record a reading. Returns (seconds_left or None, basis) where basis is
        'measured', 'learned', 'rated' or '' (nothing to go on)."""
        now = now or time.time()
        s = self.state["session"]
        new_session = (
            s is None
            or s["charging"] != charging
            or (not charging and percent > s["last_pct"] + NOISE_PCT)
            or (charging and percent < s["last_pct"] - NOISE_PCT)
        )
        if new_session:
            s = {"charging": charging, "start_t": now, "start_pct": percent, "last_pct": percent}
            self.state["session"] = s
        s["last_pct"] = percent

        rate = None
        delta = abs(percent - s["start_pct"])
        elapsed = now - s["start_t"]
        if delta >= MIN_DELTA_PCT and elapsed >= MIN_ELAPSED_S:
            rate = delta / (elapsed / 3600)
            self.state["charge_rate" if charging else "discharge_rate"] = rate
        self._save()

        remaining = (100 - percent) if charging else percent
        if rate:
            return remaining / rate * 3600, "measured"
        learned = self.state["charge_rate" if charging else "discharge_rate"]
        if learned:
            return remaining / learned * 3600, "learned"
        return None, ""

    def rated_estimate(self, name, percent):
        hours = rated_hours(name)
        if hours is None:
            return None
        return percent / 100 * hours * 3600


def format_duration(seconds):
    if seconds is None:
        return None
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"
