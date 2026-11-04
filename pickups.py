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


def text_of(item):
    return " ".join(filter(None, (KINDS[item["kind"]]["label"], item.get("name"), clock_time(item["due"]))))[:120]


def schedule(item, due):
    """Set (or move) an item's due time: text, warnings already passed, and countdown start afresh."""
    spec = KINDS[item["kind"]]
    left = due - time.time()
    item.update(due=due, text=text_of({**item, "due": due}),
                fired=[w for w in spec["warn"] if left <= (w + 1) * 60])
    for key in ("done", "repeated", "ringing", "ring_due", "ring_text"):
        item.pop(key, None)


async def add(clock, name, when, kind="pickup"):
    spec = KINDS[kind]
    due = parse_when(when)
    item = clock.unread.add({"text": "", **spec["look"]}, kind)
    item.update(kind=kind, name=name)
    schedule(item, due)
    clock.unread._save()
    log.info("%s added: %s", kind, item["text"])
    await start_ring(clock, item)
    try:
        await clock.notify(text=item["text"], **spec["look"], **ALERT)
    except Exception as e:
        log.warning("%s alert failed: %s", kind, e)
    return item


STOPWORDS = ("the", "a", "an", "my", "that", "it", "one", "reminder", "hard")


def matches(clock, what=""):
    """(clock, item) for each timer, pickup or stop on this clock whose text has all the words."""
    words = [w for w in re.findall(r"[a-z0-9]+", what.lower()) if w not in STOPWORDS]
    return [(clock, i) for i in clock.unread.items if i.get("due") and i.get("kind") in KINDS
            and all(w in i["text"].lower() for w in words)]


def best(candidates):
    """The one meant: one going off (due in the last 15 minutes, latest first), else the newest set."""
    now = time.time()
    going_off = [c for c in candidates if 0 <= now - c[1]["due"] <= 15 * 60]
    if going_off:
        return max(going_off, key=lambda c: c[1]["due"])
    return max(candidates, key=lambda c: c[1].get("time", 0), default=None)


def find(clock, what=""):
    """The item on this clock that `what` means (see best), or None."""
    found = best(matches(clock, what))
    return found[1] if found else None


async def change(clock, item, name=None, due=None):
    """Rename and/or reschedule an existing item, then put it back on the ring and show it.
    A rename alone keeps its warnings and nag state; a new time re-arms them."""
    if name is not None:
        item["name"] = name
        if item.get("ring_text"):
            item["ring_text"] = name[:24] or KINDS[item["kind"]]["label"]
    if due is not None:
        schedule(item, due)
    else:
        item["text"] = text_of(item)
    clock.unread._save()
    log.info("%s changed: %s", item["kind"], item["text"])
    await clock.refresh_ring()
    await start_ring(clock, item)
    try:
        await clock.notify(text=item["text"], **KINDS[item["kind"]]["look"], **ALERT)
    except Exception as e:
        log.warning("%s alert failed: %s", item["kind"], e)
    return item


async def move(src, dst, item, name=None, due=None):
    """Move an item to another clock, optionally renamed or rescheduled there; it keeps when it
    was set. Added to the destination before it's removed from the source."""
    moved = dst.unread.add({"text": "", **KINDS[item["kind"]]["look"]}, item["kind"])
    moved.update(kind=item["kind"], name=item.get("name", ""), time=item.get("time", moved["time"]))
    result = await change(dst, moved, name=name, due=due if due is not None else item["due"])
    src.unread.clear(item["id"])
    await src.refresh_ring()
    return result


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
