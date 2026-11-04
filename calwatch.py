"""Google Calendar on a clock: warnings before each timed event, to pull you out of hyperfocus.

Reads the calendar's secret iCal address (Google Calendar > Settings > the calendar >
"Secret address in iCal format") from the file named in config.toml [clocks.<name>.calendar]
url_file. Read-only; no Google login here.
Each event alerts STAGES minutes before (and at the start, with the light flash) and sits in the unread
cycle from the first warning until EXPIRE_MINUTES after it starts. The events also go on clock.events
for the schedule ring.
"""
import asyncio
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
STAGES = (15, 5, 0)  # minutes before start
EXPIRE_MINUTES = 30
LOOK = {"color": "Aqua", "anim": "Fan"}
ALERT = {"sound": "Rise", "priority": "critical"}

log = logging.getLogger("glanced.calendar")


def upcoming(ics_bytes, hours=24):
    """Timed events starting within the next `hours`: [(uid, start_epoch, title, end_epoch)]."""
    cal = icalendar.Calendar.from_ical(ics_bytes)
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


async def watch(clock, cfg):
    url_file, fired_file = cfg["url_file"], HERE / f"calendar_fired-{clock.name}.json"
    try:
        fired = set(json.loads(fired_file.read_text()))
    except (OSError, ValueError):
        fired = set()
    events, fetched = [], 0.0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        while True:
            now = time.time()
            if now - fetched > FETCH_MINUTES * 60:
                fetched = now
                try:
                    url = url_file.read_text().strip()
                    async with http.get(url) as r:
                        r.raise_for_status()
                        events = upcoming(await r.read())
                    log.info("%s: calendar has %d timed events in the next 24 h", clock.name, len(events))
                except FileNotFoundError:
                    events = []
                except aiohttp.ClientResponseError as e:
                    log.warning("calendar fetch failed: HTTP %s (check calendar_url)", e.status)
                except Exception as e:
                    log.warning("calendar fetch failed: %s", type(e).__name__)
                clock.events = events

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
            tmp = fired_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(fired)))
            tmp.replace(fired_file)
            await asyncio.sleep(20)
