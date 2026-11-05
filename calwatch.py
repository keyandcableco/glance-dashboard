"""Google Calendar on a clock: warnings before each timed event, to pull you out of hyperfocus.

Reads every calendar in clock.calendars (calendars.py: one from config.toml, any number added on the
panel), each by its secret iCal address (Google Calendar > Settings > the calendar > "Secret address in
iCal format"). Read-only; no Google login here. Their events are merged, the same event in two calendars
once. Each event alerts STAGES minutes before (and at the start, with the light flash) and sits in the
unread cycle from the first warning until EXPIRE_MINUTES after it starts. The events also go on
clock.events for the schedule ring.
"""
import asyncio
import contextlib
import datetime
import json
import logging
import time
from pathlib import Path

import aiohttp
import icalendar
import recurring_ical_events

HERE = Path(__file__).parent
FETCH_MINUTES = 5
MAX_BYTES = 20_000_000  # a calendar's .ics; Google's run to a few MB with years of history
STAGES = (15, 5, 0)  # minutes before start
EXPIRE_MINUTES = 30
LOOK = {"color": "Aqua", "anim": "Fan"}
ALERT = {"sound": "Rise", "priority": "critical"}

log = logging.getLogger("glanced.calendar")


def upcoming(ics_bytes, hours=24):
    """Timed events starting within the next `hours`: [(uid, start_epoch, title, end_epoch)]."""
    cal = icalendar.Calendar.from_ical(ics_bytes)
    # one event without a start would make recurring_ical_events fail the whole calendar
    cal.subcomponents[:] = [c for c in cal.subcomponents if c.name != "VEVENT" or c.get("DTSTART") is not None]
    now = datetime.datetime.now().astimezone()
    events = []
    for ev in recurring_ical_events.of(cal).between(now - datetime.timedelta(minutes=EXPIRE_MINUTES),
                                                     now + datetime.timedelta(hours=hours)):
        start = ev.get("DTSTART").dt
        if not isinstance(start, datetime.datetime):  # all-day event
            continue
        if start.tzinfo is None:
            start = start.astimezone()
        end = ev.get("DTEND").dt if ev.get("DTEND") else start
        if not isinstance(end, datetime.datetime):
            end = start
        elif end.tzinfo is None:
            end = end.astimezone()
        title = " ".join(str(ev.get("SUMMARY", "Event")).split())
        events.append((str(ev.get("UID", title)), start.timestamp(), title, max(start, end).timestamp()))
    return events


def stage_text(stage, start, title):
    when = "NOW" if stage == 0 else f"{stage} MIN"
    clock = datetime.datetime.fromtimestamp(start).strftime("%I:%M").lstrip("0")
    return f"{{clock}} {when} {title} {clock}"[:120]


async def fetch(http, url):
    """The calendar's timed events in the next 24 h. Raises on failure: say why with why(), since
    aiohttp's own messages quote the address."""
    async with http.get(url) as r:
        r.raise_for_status()
        data = bytearray()
        async for chunk in r.content.iter_chunked(1 << 16):
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("too big for a calendar")
    return await asyncio.to_thread(upcoming, bytes(data))  # a big calendar takes a while to parse; the links keep going


async def check(url):
    """Read a calendar once, to try an address before it's saved."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        return await fetch(http, url)


def why(e):
    """A failed read in a few words, never with the address in them."""
    if isinstance(e, aiohttp.ClientResponseError):
        return f"HTTP {e.status}" + (": the address is wrong, or was reset in Google Calendar" if e.status in (401, 403, 404) else "")
    if isinstance(e, TimeoutError):  # asyncio's too, and aiohttp's
        return "it didn't answer in time"
    if isinstance(e, aiohttp.InvalidURL):
        return "that isn't a web address"
    if isinstance(e, aiohttp.ClientConnectionError):
        return "couldn't reach it"
    if isinstance(e, ValueError):
        return "that's not a calendar: use the secret address in iCal format"
    return type(e).__name__


async def read_all(clock, http, cache):
    """Read each of the clock's calendars into cache {id: events}; a calendar that fails keeps the events
    it had. Returns the merged events, soonest first."""
    cals = clock.calendars
    entries = cals.entries()

    def failed(entry, reason):
        if cals.status.get(entry["id"], {}).get("error") != reason:  # once, not every 5 minutes
            log.warning("%s: calendar %r: %s", clock.name, entry["name"], reason)
        cals.status[entry["id"]] = {**cals.status.get(entry["id"], {}), "error": reason}

    async def one(entry):
        try:
            url = cals.url(entry)
        except (OSError, ValueError):
            failed(entry, "its url_file can't be read")
            return []
        try:
            events = await fetch(http, url)
        except Exception as e:
            failed(entry, why(e))
            return cache.get(entry["id"], [])
        cals.status[entry["id"]] = {"events": len(events), "error": None, "fetched": time.time()}
        return events

    results = await asyncio.gather(*(one(e) for e in entries))
    ids = {e["id"] for e in cals.entries()}  # one removed while it was being read is dropped
    cache.clear()
    cache.update((e["id"], events) for e, events in zip(entries, results) if e["id"] in ids)
    for gone in set(cals.status) - ids:
        del cals.status[gone]
    merged, seen = [], set()
    for events in cache.values():
        for event in events:
            if (event[0], int(event[1])) not in seen:
                seen.add((event[0], int(event[1])))
                merged.append(event)
    if entries:
        log.info("%s: %d calendar%s, %d timed events in the next 24 h", clock.name, len(entries),
                 "" if len(entries) == 1 else "s", len(merged))
    return sorted(merged, key=lambda event: event[1])


async def watch(clock):
    fired_file = HERE / f"calendar_fired-{clock.name}.json"
    try:
        fired = set(json.loads(fired_file.read_text()))
    except (OSError, ValueError):
        fired = set()
    saved = sorted(fired)
    events, fetched, cache = [], 0.0, {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        while True:
            now = time.time()
            if clock.calendars.changed.is_set() or now - fetched > FETCH_MINUTES * 60:
                clock.calendars.changed.clear()
                fetched = now
                events = clock.events = await read_all(clock, http, cache)
                now = time.time()
                if not any(clock.calendars.status.get(e["id"], {}).get("error") for e in clock.calendars.entries()):
                    # every calendar read: an item whose event is gone (its calendar removed, or the event
                    # moved or deleted) leaves the unread list, and its countdown the ring
                    keys = {f"{uid}|{int(start)}" for uid, start, _title, _end in events}
                    for item in [i for i in clock.unread.items if i.get("cal") and i["cal"] not in keys]:
                        clock.unread.clear(item["id"])

            for uid, start, title, _end in events:
                if now > start + EXPIRE_MINUTES * 60:
                    continue
                due = [s for s in STAGES if now >= start - s * 60]  # stages already reached
                if not due:
                    continue
                event = f"{uid}|{int(start)}"
                stage = min(due)  # only the latest reached stage alerts; earlier ones are skipped
                if f"{event}|{stage}" in fired:
                    continue
                fired.update(f"{event}|{s}" for s in due)
                if not any(i.get("cal") == event for i in clock.unread.items):
                    label = datetime.datetime.fromtimestamp(start).strftime("%I:%M").lstrip("0")
                    item = clock.unread.add({"text": f"{{clock}} {title} {label}"[:120], **LOOK}, "calendar")
                    item.update(cal=event, expires=start + EXPIRE_MINUTES * 60)
                    clock.unread._save()
                if stage > 0:  # the ring counts down to the start from the first warning
                    owner = next(i["id"] for i in clock.unread.items if i.get("cal") == event)
                    await clock.countdown(start, owner, title)
                text = stage_text(stage, start, title)
                try:
                    await clock.notify(text=text, **LOOK, **ALERT, flash=stage == 0)
                    log.info("calendar alert: %s", text)
                except Exception as e:
                    log.warning("calendar alert failed: %s", e)

            expired = [i for i in clock.unread.items if i.get("expires") and i["expires"] < now]
            for item in expired:
                clock.unread.clear(item["id"])
            # forget fired keys for events long past
            fired = {k for k in fired if float(k.rsplit("|", 2)[1]) > now - 86400}  # a UID may contain "|"
            if sorted(fired) != saved:
                saved = sorted(fired)
                tmp = fired_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(saved))
                tmp.replace(fired_file)
            with contextlib.suppress(asyncio.TimeoutError):  # an added or removed calendar is read at once
                await asyncio.wait_for(clock.calendars.changed.wait(), 20)
