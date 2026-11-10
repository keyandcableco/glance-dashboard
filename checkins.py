"""Daily check-ins: things to do at set times every day that the clock keeps asking about until you
say they're done ("hey glance, took my meds"). Each time comes up as an unread item that nags, with
its sound and the light flash, every few minutes until it's confirmed: by voice, or by clearing it on
the panel. Clearing everything leaves check-ins alone. Confirming on one clock clears it on all of
them. Confirmations are logged in checkins.json, so "did I take my meds?" has an answer.

Set on the panel's Daily reminders card (kept in checkins.json, which then wins over the config),
or in config.toml, one table per check-in (24-hour local times):

    [[checkins]]
    name = "MEDS"                     # its name: what the log and the replies call it
    text = "TAKE YOUR MEDS"           # what the clock shows when it asks (default: the name)
    times = ["06:00", "12:00"]
    words = ["meds", "pills"]         # what you call it out loud (the name always counts)
    clocks = ["office"]               # the clocks that ask (default: all)
    sound = "Shine"                   # the nag's sound (default: pickups.KINDS["checkin"])
    on = true                         # false: kept, but doesn't ask
"""
import asyncio
import datetime
import hashlib
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
KEEP_ASKED, KEEP_LOG = 2000, 1000  # asked: two days of every time of every check-in, at the most there can be
MAX_CHECKINS, MAX_TIMES = 20, 24

log = logging.getLogger("glanced.checkins")


class Stale(Exception):
    """The panel's list was out of date: another panel changed the check-ins since it read them."""

QUESTION = re.compile(r"\b(?:did i|have i|had i|when did i|when was|was it)\b")
NOT = re.compile(r"\b(?:not|haven'?t|hadn'?t|didn'?t|never|forgot|forget|yet to|need to|have to|had to|had better|"
                 r"ought|should|going to|gonna|about to|will|remind)\b")
VERB = r"(?:took|taken|had|finished|swallowed)"
DET = r"(?:(?:my|the|all|both|those|these|today'?s|morning|noon|midday)\s+)*"
PRONOUN_ONLY = re.compile(rf"(?:i\s+)?(?:just\s+)?(?:i'?ve\s+)?{VERB}\s+(?:them|it|those)(?:\s+now)?")
LEAD = re.compile(r"^(?:hey\s+)?(?:clock|glance)\b[\s,.:;!-]*")


def clean(entry, clock_names, strict=False):
    """A check-in as written -> {name, text, times [(h, m)], words, clocks, sound, on}, or None if it
    can't ask. From config.toml anything wrong is logged and left out, never raised: a typo mustn't stop
    the bridge. strict (from the panel): a ValueError says what's wrong instead."""
    name = "CHECK-IN"

    def problem(message, then):  # then: what's done about it when it's from config.toml
        if strict:
            raise ValueError(f"{name}: {message}" if name != "CHECK-IN" else message)
        log.warning("check-in %s: %s; %s", name, message, then)

    if not isinstance(entry, dict):
        problem("a check-in should be a table of settings", "left out")
        return None
    name = " ".join(str(entry.get("name") or "").upper().split())[:24]
    if not name:
        if strict:
            raise ValueError("give the reminder a name")
        name = "CHECK-IN"

    def listed(key, default):
        value = entry.get(key, default)
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            problem(f"{key} should be a list", "left out")
            return []
        return value

    times = set()
    for t in listed("times", []):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(t).strip())
        if m and int(m[1]) < 24 and int(m[2]) < 60:
            times.add((int(m[1]), int(m[2])))
        else:
            problem(f"{t!r} isn't a 24-hour HH:MM time", "left out")
    if len(times) > MAX_TIMES:
        problem(f"{MAX_TIMES} times a day at most", "only the first kept")
        times = set(sorted(times)[:MAX_TIMES])
    clocks = [c for c in dict.fromkeys(map(str, listed("clocks", clock_names))) if c in clock_names]
    for c in {str(c) for c in listed("clocks", [])} - set(clocks):
        problem(f"there's no clock named {c!r}", "left out")
    words = list(dict.fromkeys([name.lower()] + [" ".join(str(w).lower().split())[:40] for w in listed("words", []) if str(w).strip()]))
    text = " ".join(str(entry.get("text") or "").upper().split())[:40]  # the clock reads best in capitals
    sound = str(entry.get("sound") or "")
    proper = next((s for s in pb.Sound.keys() if s.lower() == sound.lower()), "")
    if sound and not proper:
        problem(f"there's no sound named {sound!r}", "using the default")
    if not times or not clocks:
        problem("it needs at least one time and one clock", "it won't ask")
        return None
    return {"name": name, "text": "" if text == name else text, "times": sorted(times), "words": words,
            "clocks": clocks, "sound": proper, "on": bool(entry.get("on", True))}


def plain(entry):
    """A cleaned check-in as it's saved and shown on the panel: times as HH:MM, words without the name."""
    return {**entry, "times": [f"{h:02d}:{m:02d}" for h, m in entry["times"]],
            "words": [w for w in entry["words"] if w != entry["name"].lower()]}


def slot_key(entry, at):
    return f"{at:%Y-%m-%d %H:%M} {entry['name']}"


class Checkins:
    def __init__(self, entries, clocks, path=PATH):
        """entries: config.toml's [[checkins]]; once the panel has set them, checkins.json's win."""
        self.clocks = clocks
        self.path = path
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = None
        self.fresh = state is None  # first run: times already past don't ask
        self.asked = (state or {}).get("asked", [])  # slot keys put on the clocks, or done early
        self.log = (state or {}).get("log", [])  # {"name", "slot", "time", "taken"[, "missed"]}
        self.from_panel = "entries" in (state or {})
        if self.from_panel:
            entries = state["entries"]
            log.info("check-ins as set on the panel (%s); [[checkins]] in config.toml no longer counts", path.name)
        if not isinstance(entries, list):
            log.warning("checkins should be [[checkins]] tables; none set up")
            entries = []
        self.all = []  # every check-in, on or off
        for entry in filter(None, (clean(e, list(clocks)) for e in entries)):
            if any(e["name"] == entry["name"] for e in self.all):
                log.warning("check-in %s: there's already one by that name; left out", entry["name"])
            else:
                self.all.append(entry)
        self.entries = [e for e in self.all if e["on"]]  # the ones that ask

    def _save(self):
        self.asked, self.log = self.asked[-KEEP_ASKED:], self.log[-KEEP_LOG:]
        state = {"asked": self.asked, "log": self.log}
        if self.from_panel:
            state["entries"] = [plain(e) for e in self.all]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self.path)

    def rev(self):
        """A revision of the list, so a panel can't save over another panel's change it hasn't seen."""
        return hashlib.sha1(json.dumps([plain(e) for e in self.all]).encode()).hexdigest()[:12]

    async def update(self, entries, rev=None, now=None):
        """Replace every check-in with the panel's list, all checked before anything changes (a
        ValueError says what's wrong; Stale if rev isn't the current one). A new one, one switched back
        on, or one with new times doesn't ask for its times already past. An open one follows the edit:
        off the clocks (not logged) when it's deleted, switched off or no longer asks on that clock, else
        it shows the new text and sound. Returns how many open ones came off."""
        if rev is not None and rev != self.rev():
            raise Stale("the reminders were changed on another panel since; showing them now")
        if not isinstance(entries, list):
            raise ValueError("send the reminders as a list")
        if len(entries) > MAX_CHECKINS:
            raise ValueError(f"{MAX_CHECKINS} reminders at most")
        new = [clean(e, list(self.clocks), strict=True) for e in entries]
        names = [e["name"] for e in new]
        if twice := next((n for n in names if names.count(n) > 1), None):
            raise ValueError(f"two reminders are called {twice}")
        now = now or datetime.datetime.now()
        await self.ask_due(now)  # anything just due under the old list goes up first, so an edit can't swallow it
        before = {e["name"]: e["times"] for e in self.entries}
        asking = [e for e in new if e["on"]]
        was = self.all, self.entries, self.from_panel, list(self.asked)
        for entry in asking:
            if before.get(entry["name"]) == entry["times"]:
                continue
            for back in (1, 0):
                for h, m in entry["times"]:
                    at = (now - datetime.timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
                    if at <= now and slot_key(entry, at) not in self.asked:
                        self.asked.append(slot_key(entry, at))
        self.all, self.entries, self.from_panel = new, asking, True
        try:
            self._save()
        except OSError:
            self.all, self.entries, self.from_panel, self.asked = was
            raise
        log.info("check-ins set on the panel: %s", ", ".join(f"{e['name']}{'' if e['on'] else ' (off)'}" for e in new) or "none")
        by_name, gone = {e["name"]: e for e in asking}, []
        for clock, item in self.open_items():
            entry = by_name.get(item["name"])
            if not entry or clock.name not in entry["clocks"]:
                clock.unread.clear(item["id"])
                gone.append(clock)
                log.info("check-in taken off %s: %s", clock.name, item["checkin"])
                continue
            for key in ("say", "sound"):
                item.pop(key, None)
            item.update(**({"say": entry["text"]} if entry["text"] else {}), **({"sound": entry["sound"]} if entry["sound"] else {}))
            item["text"] = pickups.text_of(item)
            clock.unread._save()
        for clock in set(gone):
            try:
                await clock.refresh_ring()
            except Exception as e:
                log.warning("ring refresh failed: %s", e)
        return len(gone)

    def json(self, now=None):
        """For the panel: every check-in as set, with how each of today's times went (taken, skipped,
        missed, open, later, or none: not asked), any still open from earlier ("day": "earlier", or one
        whose time has since changed), and any logged outside a time."""
        now = now or datetime.datetime.now()
        out = []
        for entry in self.all:
            opened = {i["checkin"]: i["due"] for _, i in self.open_items([entry["name"]])}
            keys = {slot_key(entry, now.replace(hour=h, minute=m, second=0, microsecond=0)) for h, m in entry["times"]}
            slots = [{"time": f"{datetime.datetime.fromtimestamp(due):%H:%M}", "state": "open",
                      **({"day": "earlier"} if datetime.date.fromtimestamp(due) < now.date() else {})}
                     for key, due in sorted(opened.items(), key=lambda kd: kd[1]) if key not in keys and due <= now.timestamp()]
            for h, m in entry["times"]:
                at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                key = slot_key(entry, at)
                done = next((l for l in reversed(self.log) if l["slot"] == key), None)
                state = ("taken" if done.get("taken", True) else "missed" if done.get("missed") else "skipped") if done \
                    else "open" if key in opened else "later" if at > now else "none"
                slots.append({"time": f"{h:02d}:{m:02d}", "state": state, **({"at": done["time"]} if done else {})})
            extra = [l["time"] for l in self.log if l["name"] == entry["name"] and l["slot"] is None
                     and l.get("taken", True) and datetime.date.fromtimestamp(l["time"]) == now.date()]
            out.append({**plain(entry), "today": slots, "extra": extra})
        return {"checkins": out, "from": "panel" if self.from_panel else "config", "clocks": list(self.clocks), "rev": self.rev()}

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
                    self.log.append({"name": entry["name"], "slot": item["checkin"], "time": time.time(), "taken": False, "missed": True})
                log.info("check-in missed: %s", item["checkin"])
        look = pickups.KINDS["checkin"]["look"]
        for name in entry["clocks"]:
            clock = self.clocks[name]
            try:
                item = clock.unread.add({"text": "", **look}, "checkin")
                item.update(kind="checkin", name=entry["name"], checkin=key, **({"say": entry["text"]} if entry["text"] else {}),
                            **({"sound": entry["sound"]} if entry["sound"] else {}))
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
