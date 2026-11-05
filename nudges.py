"""Hyperfocus nudges: a break after a long stretch of steady activity, and a wrap-up before closing.

Starting values per clock come from config.toml [clocks.<name>.nudges]; changes made on the panel are
kept in nudges-<name>.json and win over the config.
    breaks = true             # break reminders on
    break_minutes = 75        # nudge after this long active without a pause
    pause_minutes = 10        # this long with no keyboard, mouse or speech counts as a break
    closing = "18:00"         # "wrap up" WRAP_MINUTES before, and "closing" at the time ("" = off)
    closing_days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]   # empty: every day
snooze_until (panel only) silences both until then.

Activity is the X keyboard/mouse idle time (libXss) and, if a listener touches it, $GLANCE_VOICE_FILE.
"""
import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

WRAP_MINUTES = 30
# optional: a file whose modification time is the last moment someone spoke (a dictation listener can touch it)
VOICE = os.environ.get("GLANCE_VOICE_FILE", os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "handy-wake.voice"))
LOOK = {"color": "LimeGreen", "anim": "Flower", "priority": "critical"}
HERE = Path(__file__).parent
DEFAULTS = {"breaks": False, "break_minutes": 75, "pause_minutes": 10, "closing": "",
            "closing_days": [], "snooze_until": 0}

log = logging.getLogger("glanced.nudges")


class Settings:
    """A clock's nudge settings: DEFAULTS, then its config section, then what the panel changed."""

    def __init__(self, name, cfg):
        self.path = HERE / f"nudges-{name}.json"
        self.values = {**DEFAULTS, **({"breaks": True} if cfg else {}), **(cfg or {})}
        try:
            self.values.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    def update(self, changes):
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise ValueError(f"unknown setting {key!r}")
            if key == "closing" and not value:
                value = ""  # null, false, 0: closing nudges off
            if key == "closing" and value:
                hour, minute = map(int, str(value).split(":"))  # ValueError if not HH:MM
                if not (0 <= hour < 24 and 0 <= minute < 60):
                    raise ValueError(f"closing time {value!r} isn't a time of day")
                value = f"{hour:02d}:{minute:02d}"
            if key in ("break_minutes", "pause_minutes") and int(value) < 1:
                raise ValueError(f"{key} must be at least 1")
            self.values[key] = type(DEFAULTS[key])(value) if not isinstance(DEFAULTS[key], list) else list(value)
        saved = {k: v for k, v in self.values.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=1))
        tmp.replace(self.path)
        return self.values


# Prints the desktop's keyboard/mouse idle time in ms, or nothing. Run in its own process each time:
# a bridge started at boot has no desktop until someone logs in, and Xlib exits the whole process
# when its display goes away at logout. A service started at boot has no DISPLAY; the desktop is :0.
X_IDLE = r"""
import ctypes, ctypes.util, os
class Info(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]
x11 = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11"))
xss = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xss"))
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x11.XDefaultRootWindow.restype = ctypes.c_ulong
xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Info)]
display = x11.XOpenDisplay(os.environ.get("DISPLAY", ":0").encode())
info = Info()
if display and xss.XScreenSaverQueryInfo(display, x11.XDefaultRootWindow(display), ctypes.byref(info)):
    print(info.idle)
"""


async def x_idle():
    """Seconds since the last keyboard or mouse input on the desktop; None if there's no desktop or libXss."""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", X_IDLE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        return int(out) / 1000
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    except ValueError:
        pass
    return None


class Idle:
    """Seconds since the last keyboard, mouse or speech; None if none of them can be read."""

    def __init__(self):
        self.desktop = None  # whether the last reading reached the desktop, to log changes once

    async def seconds(self):
        times = []
        x = await x_idle()
        if (x is not None) != self.desktop:
            self.desktop = x is not None
            log.info("X idle time %s", "from the desktop" if self.desktop else "unavailable; using speech only")
        if x is not None:
            times.append(x)
        try:
            times.append(time.time() - os.path.getmtime(VOICE))
        except OSError:
            pass
        return min(times) if times else None


def closing_today(values, now):
    """Today's closing time as a datetime, or None (not set, or not a working day)."""
    if not values.get("closing"):
        return None
    days = values.get("closing_days")
    if days and now.strftime("%a") not in days:
        return None
    hour, minute = map(int, values["closing"].split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


async def watch(clock):
    idle = Idle()
    clock.focus = None
    active_since, said = None, set()  # said: closing nudges already given, as (date, which)
    while True:
        await asyncio.sleep(30)
        v, now = clock.nudges.values, time.time()
        if now < v["snooze_until"]:
            active_since = clock.focus = None
            continue
        quiet = await idle.seconds()
        if not v["breaks"] or quiet is None or quiet >= v["pause_minutes"] * 60:
            active_since = None  # off, or walked away: that was the break
        elif active_since is None:
            active_since = now - quiet
        elif now - active_since >= v["break_minutes"] * 60:
            minutes = int((now - active_since) // 60)
            await say(clock, f"{{smiley}} BREAK - {minutes // 60}:{minutes % 60:02d} STRAIGHT. STAND, DRINK", "Steps")
            active_since = now  # next nudge after another full stretch
        # for the status ring: (start of this active stretch, the break length), or None
        clock.focus = (active_since, v["break_minutes"] * 60) if v["breaks"] and active_since else None

        today = datetime.datetime.now()
        closing = closing_today(v, today)
        if closing:
            for which, at, text, sound in (
                    ("wrap", closing - datetime.timedelta(minutes=WRAP_MINUTES),
                     f"{{clock}} WRAP UP - CLOSING {closing:%I:%M}".replace(" 0", " "), "Rise"),
                    ("close", closing, "{clock} CLOSING TIME", "Bye")):
                key = (today.date(), which)
                if key not in said and at <= today < at + datetime.timedelta(minutes=10):
                    said.add(key)
                    await say(clock, text, sound)


async def say(clock, text, sound):
    try:
        await clock.notify(text=text[:120], sound=sound, **LOOK)
        log.info("%s: nudge: %s", clock.name, text)
    except Exception as e:
        log.warning("%s: nudge failed: %s", clock.name, e)
