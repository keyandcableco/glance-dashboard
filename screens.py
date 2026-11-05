"""Which screens a clock shows in turn, besides its own time and date: the status ring, the forecast and
the schedule ring. Each is a saved scene in its own slot; a screen that's off is deleted from the clock
by its watcher, which then leaves it off.

Starting values per clock come from config.toml (status_ring = false, schedule_ring = false; the
forecast exists only with a [forecast] section); changes made on the panel are kept in
screens-<name>.json and win over the config.
"""
import json
from pathlib import Path

import forecast
import schedulering
import statusring

HERE = Path(__file__).parent
SLOTS = {"status": statusring.SLOT, "forecast": forecast.SLOT, "schedule": schedulering.SLOT}
WORDS = {"true": True, "on": True, "1": True, "false": False, "off": False, "0": False}


class Screens:
    def __init__(self, name, cfg):
        self.path = HERE / f"screens-{name}.json"
        self.available = {"status": True, "forecast": "forecast" in cfg, "schedule": True}
        self.start = {"status": bool(cfg.get("status_ring", True)), "forecast": True,
                      "schedule": bool(cfg.get("schedule_ring", True))}
        self.saved = {}  # only the screens switched on the panel, so the config still decides the rest
        try:
            saved = json.loads(self.path.read_text())
            self.saved = {k: v for k, v in saved.items() if k in SLOTS and isinstance(v, bool)}
        except (OSError, ValueError, AttributeError):
            pass

    def on(self, screen):
        return self.available[screen] and self.saved.get(screen, self.start[screen])

    def update(self, changes):
        """{screen: true or false}; raises ValueError for an unknown screen, a value that isn't on or off,
        or turning on a screen this clock doesn't have; OSError if it can't be saved (then nothing
        changes). Returns the changes as booleans."""
        new = {}
        for screen, value in changes.items():
            if screen not in SLOTS:
                raise ValueError(f"unknown screen {screen!r}; have {', '.join(SLOTS)}")
            if isinstance(value, str):
                value = WORDS.get(value.strip().lower(), value)
            if not isinstance(value, (bool, int)) or value not in (0, 1):
                raise ValueError(f"{screen} must be true or false")
            if not self.available[screen]:
                if value:
                    raise ValueError(f"this clock has no {screen} screen set up in config.toml")
                continue  # already off; saving it would keep it off once it's set up
            new[screen] = bool(value)
        if new:
            saved = {**self.saved, **new}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(saved, indent=1))
            tmp.replace(self.path)
            self.saved = saved
        return new

    def json(self):
        return {s: {"on": self.on(s), "available": self.available[s]} for s in SLOTS}
