"""Pickups, timers and hard stops: "someone's coming in 40 minutes", "timer 30 minutes", "I need to stop
at 6". Each is an unread item with a due time; clearing the item cancels it.

Each kind alerts when added, at its warning stages (minutes before; a stage already passed is
skipped), and when due. A hard stop keeps repeating after it's due until it's cleared. The ring
counts down from ring_from minutes out (None: from the start).
"""
import asyncio
import datetime
import logging
import re
import time

KINDS = {
    "pickup": {"label": "PICKUP", "look": {"color": "DarkGoldenRod", "anim": "Wheel"}, "warn": (10,), "ring_from": 10},
    "timer": {"label": "TIMER", "look": {"color": "Turquoise", "anim": "Fan"}, "warn": (10,), "ring_from": None},
    "stop": {"label": "STOP", "look": {"color": "Red", "anim": "Pulse"}, "warn": (30, 15, 5), "ring_from": 30,
             "repeat_minutes": 5, "repeats": 6, "due_sound": "Waves"},
}
ALERT = {"sound": "Rise", "priority": "critical"}
DAY_START, DAY_END = 7, 21  # a bare "2:30" means the next one in these hours (14:30), not 2:30 at night

log = logging.getLogger("glanced.pickups")


def parse_when(when, now=None):
    """'40', '40m', '1h', '1h30', '1:30' (from now) or '2:30pm', '14:30', '3pm' (clock time) -> epoch seconds."""
    now = now or datetime.datetime.now()
    w = str(when).strip().lower().replace(" ", "")
    if m := re.fullmatch(r"(\d+)(?:m|min)?", w):
        return (now + datetime.timedelta(minutes=int(m[1]))).timestamp()
    if m := re.fullmatch(r"(\d+)h(?:(\d+)m?)?", w):
        return (now + datetime.timedelta(hours=int(m[1]), minutes=int(m[2] or 0))).timestamp()
    if m := re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", w):
        hour, minute, ampm = int(m[1]), int(m[2] or 0), m[3]
        if hour > 23 or minute > 59 or (ampm and not 1 <= hour <= 12):
            raise ValueError(f"{when!r} isn't a time of day")
        if ampm:
            hours = [hour % 12 + (12 if ampm == "pm" else 0)]
        elif 1 <= hour <= 11:
            hours = [hour, hour + 12]  # "2:30" is whichever 2:30 comes next
        else:
            hours = [hour]
        times = [now.replace(hour=h, minute=minute, second=0, microsecond=0) + datetime.timedelta(days=d)
                 for d in (0, 1) for h in hours]
        upcoming = [t for t in times if t > now]
        daytime = [t for t in upcoming if DAY_START <= t.hour < DAY_END] if len(hours) > 1 else []
        return min(daytime or upcoming).timestamp()
    raise ValueError(f"don't understand time {when!r}")


def clock_time(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime("%I:%M").lstrip("0")


async def add(clock, name, when, kind="pickup"):
    spec = KINDS[kind]
    due = parse_when(when)
    text = " ".join(filter(None, (spec["label"], name, clock_time(due))))[:120]
    item = clock.unread.add({"text": text, **spec["look"]}, kind)
    left = due - time.time()
    item.update(due=due, kind=kind, name=name, fired=[w for w in spec["warn"] if left <= (w + 1) * 60])
    clock.unread._save()
    log.info("%s added: %s", kind, text)
    await start_ring(clock, item)
    try:
        await clock.notify(text=text, **spec["look"], **ALERT)
    except Exception as e:
        log.warning("%s alert failed: %s", kind, e)
    return item


async def start_ring(clock, item):
    spec = KINDS[item["kind"]]
    if not item.get("ringing") and (spec["ring_from"] is None or item["due"] - time.time() <= spec["ring_from"] * 60):
        item["ringing"] = True
        await clock.countdown(item["due"], item["id"], item.get("name") or spec["label"])


def next_stage(item, spec, now):
    """The alert this item owes now, as (stage text, sound), updating its bookkeeping; or None."""
    due, fired = item["due"], item.setdefault("fired", [])
    if now < due:
        reached = [w for w in spec["warn"] if w not in fired and now >= due - w * 60]
        if not reached:
            return None
        fired.extend(reached)  # several reached at once (a late start): say only the nearest
        return f"{min(reached)} MIN", ALERT["sound"]
    if not item.get("done"):
        item["done"], item["repeated"] = True, 0
        return "NOW", spec.get("due_sound", ALERT["sound"])
    every = spec.get("repeat_minutes")
    if every and item["repeated"] < spec["repeats"] and now >= due + (item["repeated"] + 1) * every * 60:
        item["repeated"] += 1
        return "NOW", spec.get("due_sound", ALERT["sound"])
    return None


async def watch(clock):
    while True:
        await asyncio.sleep(20)
        await clock.refresh_ring()
        now = time.time()
        for item in list(clock.unread.items):
            if not item.get("due") or item.get("kind") not in KINDS:
                continue
            spec = KINDS[item["kind"]]
            await start_ring(clock, item)
            owed = next_stage(item, spec, now)
            clock.unread._save()
            if not owed:
                continue
            stage, sound = owed
            name = item.get("name", "")
            try:
                await clock.notify(text=" ".join(filter(None, (spec["label"], stage, name)))[:120],
                                   **spec["look"], sound=sound, priority=ALERT["priority"])
                log.info("%s %s: %s", spec["label"], stage, name)
            except Exception as e:
                log.warning("%s alert failed: %s", spec["label"], e)
