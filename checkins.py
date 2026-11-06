"""Daily check-ins: things to do at set times every day that the clock keeps asking about until you
say they're done ("hey glance, took my meds"). Each time comes up as an unread item that nags, with
its sound and the light flash, every few minutes until it's confirmed: by voice, or by clearing it on
the panel. Clearing everything leaves check-ins alone. Confirming on one clock clears it on all of
them. Confirmations are logged in checkins.json, so "did I take my meds?" has an answer.

In config.toml, one table per check-in (24-hour local times):

    [[checkins]]
    name = "MEDS"                     # what the clock shows
    times = ["06:00", "12:00"]
    words = ["meds", "pills"]         # what you call it out loud (the name always counts)
    clocks = ["office"]               # the clocks that ask (default: all)
    sound = "Shine"                   # the nag's sound (default: pickups.KINDS["checkin"])
"""
import asyncio
import datetime
import json
import logging
import re
import time
from pathlib import Path

import pickups
from glance import pb

PATH = Path(__file__).parent / "checkins.json"
LATE_HOURS = 6  # a time missed while the bridge was down still comes up this long after
EARLY_MINUTES = 60  # done this long before a time counts for that time, which then doesn't ask
AGAIN_MINUTES = 90  # done again this soon after the last with nothing waiting: not logged twice
KEEP_ASKED, KEEP_LOG = 200, 1000

log = logging.getLogger("glanced.checkins")

QUESTION = re.compile(r"\b(?:did i|have i|had i|when did i|when was|was it)\b")
NOT = re.compile(r"\b(?:not|haven'?t|hadn'?t|didn'?t|never|forgot|forget|yet to|need to|have to|had to|had better|"
                 r"ought|should|going to|gonna|about to|will|remind)\b")
VERB = r"(?:took|taken|had|finished|swallowed)"
DET = r"(?:(?:my|the|all|both|those|these|today'?s|morning|noon|midday)\s+)*"
PRONOUN_ONLY = re.compile(rf"(?:i\s+)?(?:just\s+)?(?:i'?ve\s+)?{VERB}\s+(?:them|it|those)(?:\s+now)?")
LEAD = re.compile(r"^(?:hey\s+)?(?:clock|glance)\b[\s,.:;!-]*")


def clean(entry, clock_names):
    """A [[checkins]] table -> {name, times [(h, m)], words, clocks, sound}, or None if it can't ask.
    Anything wrong is logged and left out, never raised: a typo mustn't stop the bridge."""
    if not isinstance(entry, dict):
        log.warning("a [[checkins]] entry isn't a table; left out")
        return None
    name = str(entry.get("name", "")).upper().strip()[:24] or "CHECK-IN"

    def listed(key, default):
        value = entry.get(key, default)
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            log.warning("check-in %s: %s should be a list; left out", name, key)
            return []
        return value

    times = []
    for t in listed("times", []):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(t).strip())
        if m and int(m[1]) < 24 and int(m[2]) < 60:
            times.append((int(m[1]), int(m[2])))
        else:
            log.warning("check-in %s: %r isn't a 24-hour HH:MM time; left out", name, t)
    clocks = [c for c in listed("clocks", clock_names) if c in clock_names]
    for c in {str(c) for c in listed("clocks", [])} - set(clocks):
        log.warning("check-in %s: no clock named %r", name, c)
    words = list(dict.fromkeys([name.lower()] + [str(w).lower().strip() for w in listed("words", []) if str(w).strip()]))
    sound = str(entry.get("sound", ""))
    proper = next((s for s in pb.Sound.keys() if s.lower() == sound.lower()), "")
    if sound and not proper:
        log.warning("check-in %s: no sound named %r; using the default", name, sound)
    if not times or not clocks:
        log.warning("check-in %s: no times or no clocks; it won't ask", name)
        return None
    return {"name": name, "times": sorted(times), "words": words, "clocks": clocks, "sound": proper}


def slot_key(entry, at):
    return f"{at:%Y-%m-%d %H:%M} {entry['name']}"


class Checkins:
    def __init__(self, entries, clocks, path=PATH):
        self.clocks = clocks
        if not isinstance(entries, list):
            log.warning("checkins should be [[checkins]] tables; none set up")
            entries = []
        self.entries = [e for e in (clean(e, list(clocks)) for e in entries) if e]
        self.path = path
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = None
        self.fresh = state is None  # first run: times already past don't ask
        self.asked = (state or {}).get("asked", [])  # slot keys put on the clocks, or done early
        self.log = (state or {}).get("log", [])  # {"name", "slot", "time", "taken"}

    def _save(self):
        self.asked, self.log = self.asked[-KEEP_ASKED:], self.log[-KEEP_LOG:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"asked": self.asked, "log": self.log}, indent=1))
        tmp.replace(self.path)

    def said(self, text):
        """The entries whose spoken words (or name) are in the text."""
        return [e for e in self.entries if any(re.search(rf"\b{re.escape(w)}\b", text.lower()) for w in e["words"])]

    def open_items(self, names=None):
        """[(clock, item)] for every open check-in, newest first; only these entries' if names."""
        found = [(c, i) for c in self.clocks.values() for i in c.unread.items
                 if i.get("kind") == "checkin" and i.get("checkin") and (names is None or i.get("name") in names)]
        return sorted(found, key=lambda ci: ci[1]["due"], reverse=True)

    def parse(self, text):
        """Spoken text -> ("/checkin" or "/checkin/ask", {"what": text}, None), or None if it isn't one.
        Only plain "took my meds" / "meds taken" / "took them" (with one open) count as done."""
        if not self.entries:
            return None
        t = LEAD.sub("", text.lower().replace("’", "'").strip(" ,.;:!?"))
        for name in self.clocks:  # "family, took my meds"
            t = re.sub(rf"^{re.escape(name.lower())}(?:'s)?(?: clock)?\b[\s,.:;!-]*", "", t)
        mine = self.said(t)
        pronoun = re.search(r"\b(?:them|it|those)\b", t)
        if QUESTION.search(t):
            return ("/checkin/ask", {"what": t}, None) if mine or (pronoun and self.open_items()) else None
        if NOT.search(t):
            return None
        words = "|".join(re.escape(w) for e in mine for w in e["words"])
        if mine and (re.search(rf"\b{VERB}\s+{DET}(?:{words})\b", t)
                     or re.search(rf"\b(?:{words})\s+(?:are\s+|is\s+)?(?:taken|done|down)\b", t)):
            return "/checkin", {"what": t}, None
        if PRONOUN_ONLY.fullmatch(t) and self.open_items():
            return "/checkin", {"what": t}, None
        return None

    async def ask_due(self, now=None):
        """Put up each check-in whose time has come (today, or late from yesterday) and hasn't been asked."""
        now = now or datetime.datetime.now()
        changed = self.fresh
        for entry in self.entries:
            for back in (1, 0):
                day = now - datetime.timedelta(days=back)
                for h, m in entry["times"]:
                    at = day.replace(hour=h, minute=m, second=0, microsecond=0)
                    key = slot_key(entry, at)
                    if at > now or key in self.asked:
                        continue
                    if not self.fresh and now - at <= datetime.timedelta(hours=LATE_HOURS):
                        await self.put_up(entry, at, key)
                    self.asked.append(key)
                    changed = True
        self.fresh = False
        if changed:
            self._save()

    async def put_up(self, entry, at, key):
        """An unread item on each of the entry's clocks; pickups.watch does the nagging. Ones still open
        from an earlier day come off first, logged as missed, so they can't take the next confirmation."""
        for clock, item in self.open_items([entry["name"]]):
            if datetime.date.fromtimestamp(item["due"]) < at.date():
                clock.unread.clear(item["id"])
                if not any(l["slot"] == item["checkin"] for l in self.log):
                    self.log.append({"name": entry["name"], "slot": item["checkin"], "time": time.time(), "taken": False})
                log.info("check-in missed: %s", item["checkin"])
        look = pickups.KINDS["checkin"]["look"]
        for name in entry["clocks"]:
            clock = self.clocks[name]
            try:
                item = clock.unread.add({"text": "", **look}, "checkin")
                item.update(kind="checkin", name=entry["name"], checkin=key, **({"sound": entry["sound"]} if entry["sound"] else {}))
                pickups.schedule(item, at.timestamp())
                item["ringing"] = True  # due now: no countdown
                clock.unread._save()
            except Exception as e:  # one clock's failure mustn't keep the check-in off the others
                log.warning("check-in %s on %s failed: %s", key, name, e)
        log.info("check-in up: %s", key)

    async def done(self, what="", key=None, taken=True):
        """Mark a check-in done (taken=False: skipped): the one with `key`, else the newest open one of
        those named in `what` (any, if none is named). It comes off every clock and is logged.
        Done with none open counts for the next time within EARLY_MINUTES. Returns what to say."""
        now = time.time()
        if key is None:
            named = [e["name"] for e in self.said(what)] or None
            openings = self.open_items(named)
            if not openings:
                return self.early(named, now) if taken else "There's no check-in waiting."
            key = openings[0][1]["checkin"]
        found = [(c, i) for c, i in self.open_items() if i["checkin"] == key]
        if not found:
            return "That check-in is already done."
        name, due = found[0][1]["name"], found[0][1]["due"]
        for clock, item in found:  # all off before any await, so a second confirm finds none open
            clock.unread.clear(item["id"])
        self.log.append({"name": name, "slot": key, "time": now, "taken": taken})
        self._save()
        log.info("check-in %s: %s", "done" if taken else "skipped", key)
        text = f"{{smiley}} {name} DONE" if taken else f"{name} SKIPPED"
        for clock in {c for c, _ in found}:
            try:
                await clock.refresh_ring()
                await clock.alert("checkin", text=text, color="Lime", anim="Pulse", sound="NoneSound", priority="critical")
            except Exception as e:
                log.warning("check-in confirm alert failed: %s", e)
        reply = f"{'Logged' if taken else 'Skipped'} the {pickups.clock_time(due)} {name.lower()}."
        still = [i for _, i in self.open_items([name]) if i["checkin"] != key]
        if still:
            reply += f" The {pickups.clock_time(still[0]['due'])} one is still open."
        return reply

    def early(self, names, now):
        """Done with none waiting: counts for a time due within EARLY_MINUTES, which then won't ask;
        else logged as taken then, once (not again within AGAIN_MINUTES)."""
        entries = [e for e in self.entries if e["name"] in names] if names else self.entries
        when = datetime.datetime.fromtimestamp(now)
        for entry in entries:
            for ahead in (0, 1):
                day = when + datetime.timedelta(days=ahead)
                for h, m in entry["times"]:
                    at = day.replace(hour=h, minute=m, second=0, microsecond=0)
                    key = slot_key(entry, at)
                    if datetime.timedelta(0) < at - when <= datetime.timedelta(minutes=EARLY_MINUTES) and key not in self.asked:
                        self.asked.append(key)
                        self.log.append({"name": entry["name"], "slot": key, "time": now, "taken": True})
                        self._save()
                        log.info("check-in done early: %s", key)
                        return f"Logged. That counts for the {pickups.clock_time(at.timestamp())} {entry['name'].lower()}."
        if len(entries) != 1:
            return "There's no check-in waiting. Say which one."
        name = entries[0]["name"]
        last = max((l["time"] for l in self.log if l["name"] == name and l.get("taken", True)), default=0)
        if now - last < AGAIN_MINUTES * 60:
            return f"Nothing's waiting. {name.capitalize()} already logged at {pickups.clock_time(last)}."
        self.log.append({"name": name, "slot": None, "time": now, "taken": True})
        self._save()
        return f"Logged {name.lower()} at {pickups.clock_time(now)}. None was waiting."

    def status(self, what=""):
        """What's logged today for the check-ins named in `what` (all if none), and what's open."""
        today = datetime.date.today()
        parts = []
        for entry in self.said(what) or self.entries:
            name = entry["name"].lower()
            times = [pickups.clock_time(l["time"]) for l in self.log if l["name"] == entry["name"]
                     and l.get("taken", True) and datetime.date.fromtimestamp(l["time"]) == today]
            parts.append(f"{name.capitalize()} logged today at {' and '.join(times)}." if times
                         else f"No {name} logged today.")
            waiting = list(dict.fromkeys(pickups.clock_time(i["due"]) for _, i in self.open_items([entry["name"]])))
            if waiting:
                parts.append(f"The {' and '.join(waiting)} {'ones are' if len(waiting) > 1 else 'one is'} waiting.")
        return " ".join(parts)


async def watch(checkins):
    while True:
        await checkins.ask_due()
        await asyncio.sleep(20)
