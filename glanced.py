#!/usr/bin/env python3
"""Glance Clock bridge: holds the BLE links to the clocks in config.toml, serves them the time,
and takes alerts over HTTP. Every call takes an optional clock name; the first clock is the default.

  POST http://127.0.0.1:8740/notify  {"text": "{bell} ORDER", "color": "Red", "clock": "shop"}
  GET  http://127.0.0.1:8740/status
  GET  http://127.0.0.1:8740/  control panel
"""
import asyncio
import contextlib
import base64
import hmac
import datetime
import logging
import os
import re
import time

from aiohttp import web
from bleak import BleakClient
from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

import animscreen
import brain
import calendars
import calwatch
import forecast
import shopapp
import statusring
import clockvoice
import config
import mailwatch
import notes
import nudges
import pickups
import pickupsync
import ringwords
import schedulering
import screens
import screensaver
import textwatch
import unread
from glance import CHAR, NOTIFY, PRIORITIES, build_notice, clock_text, enum_value
import glance_pb2 as pb  # on sys.path via glance

PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
SETTINGS_CMD = bytes([5, 0, 0, 0])
TIMER, TIMER_STOP = 3, 10
FLASH_S = 3.5  # the light flash runs 3 s; its scene is deleted after
SOUND = 45  # [45, 0, 0, n] plays the clock's sound n, [45, 0, 0, 0] stops it
SOUND_S = 5  # a played sound is stopped after this, since not every sound may end by itself
RESYNC_AT, RESYNC_BY = datetime.time(3, 30), datetime.time(6)  # nightly, after a 2 am daylight-saving change
PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel_password")
LOCAL = ("127.0.0.1", "::1")
CTS_SERVICE = "00001805-0000-1000-8000-00805f9b34fb"
CTS_CURRENT_TIME = "00002a2b-0000-1000-8000-00805f9b34fb"
CTS_LOCAL_TIME_INFO = "00002a0f-0000-1000-8000-00805f9b34fb"

log = logging.getLogger("glanced")


def current_time_bytes():
    now = datetime.datetime.now()
    return bytearray([
        now.year & 0xFF, now.year >> 8, now.month, now.day,
        now.hour, now.minute, now.second, now.isoweekday(),
        now.microsecond * 256 // 1_000_000, 0,
    ])


def local_time_info_bytes():
    now = datetime.datetime.now().astimezone()
    offset = now.utcoffset() or datetime.timedelta(0)
    dst = now.dst() or datetime.timedelta(0)
    std_quarters = int((offset - dst).total_seconds() // 900)
    dst_code = 4 if dst else 0  # 4 = +1h daylight time
    return bytearray([std_quarters & 0xFF, dst_code])


def on_read(characteristic, **kwargs):
    uuid = str(characteristic.uuid).lower()
    if uuid == CTS_CURRENT_TIME:
        log.info("clock read the time")
        return current_time_bytes()
    if uuid == CTS_LOCAL_TIME_INFO:
        return local_time_info_bytes()
    return characteristic.value


def adapter():
    """The first Bluetooth adapter: after a reset it can come back as hci1, and bless looks only for hci0."""
    try:
        names = [n for n in os.listdir("/sys/class/bluetooth") if re.fullmatch(r"hci\d+", n)]
    except OSError:
        return None
    return min(names, key=lambda n: int(n[3:]), default=None)


async def start_time_server():
    server = BlessServer(name="glanced", adapter=adapter())
    server.read_request_func = on_read
    await server.add_new_service(CTS_SERVICE)
    for uuid, value in ((CTS_CURRENT_TIME, current_time_bytes()), (CTS_LOCAL_TIME_INFO, local_time_info_bytes())):
        await server.add_new_characteristic(
            CTS_SERVICE, uuid,
            GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
            value, GATTAttributePermissions.readable,
        )
    try:
        await server.start()
    except Exception as e:
        # bless registers the GATT app before advertising; the clock reads CTS over the
        # link we open, so a failed advertisement doesn't matter
        log.warning("advertising failed (%s); time service still registered", e)
    log.info("Current Time Service up")
    return server


class Clock:
    def __init__(self, name, addr):
        self.name = name
        self.addr = addr
        self.client = None
        self.lost = asyncio.Event()
        self.lock = asyncio.Lock()
        self.unread = unread.Unread(name)
        self.ring = None  # (due epoch, unread item id) of the countdown on the ring
        self.ring_unknown = True  # after a start the clock may still run a countdown we no longer know about
        self.last_alert = 0.0  # when an alert or countdown was last sent (the screens wait after it)
        self.events = []  # calendar events (uid, start, title, end) in the next 24 h, from calwatch
        self.flashing = asyncio.Lock()  # one flash and its notice at a time: flashes share one slot
        self.sound_stop = None  # the task that stops a played sound
        self.sounding = asyncio.Lock()  # one play or stop at a time, so a stop never cuts the next sound short
        self.linked_at = None  # when the link was made: the clock read the time then

    async def countdown(self, due, owner, text):
        """Ask for a ring countdown to `due` for unread item `owner`. The clock has one ring
        timer, so refresh_ring decides who has it (a running staged timer, else the soonest)."""
        for item in self.unread.items:
            if item["id"] == owner:
                item.update(ring_due=due, ring_text=clock_text(text)[:24] or "TIME")
        self.unread._save()
        await self.refresh_ring()

    async def refresh_ring(self):
        """Put the wanted countdown on the ring (a running staged timer first, else the soonest);
        stop it if none is left. Called on every change and every 20 s, so the next one takes over
        when one ends or is cleared."""
        now = time.time()
        wanted = [i for i in self.unread.items if i.get("ring_due", 0) > now]
        # a running staged timer first (its stage labels are the point), then the soonest due
        best = min(wanted, key=lambda i: (not i.get("stages"), i["ring_due"]), default=None)
        if best is None:
            if self.ring_unknown or (self.ring and self.ring[0] > now):  # cleared before it finished, or left from before a restart
                with contextlib.suppress(Exception):  # not connected yet: try again on the next check
                    await self.write(bytes([TIMER_STOP, 0, 0, 0]))
                    self.ring, self.ring_unknown = None, False
            return
        if self.ring == (best["ring_due"], best["id"]) and self.ring[0] > now:
            return
        try:
            # a staged timer sends the stages still ahead, each with its label; others one interval
            stages = pickups.stages_left(best, now) or [("", max(1, int(best["ring_due"] - now)))]
            await self.write(pickups.timer_payload(stages, best["ring_text"]))
            self.ring, self.ring_unknown = (best["ring_due"], best["id"]), False
        except Exception as e:
            log.warning("countdown failed: %s", e)

    async def write(self, data):
        if not self.connected:
            raise ConnectionError("clock not connected")
        async with self.lock:
            await self.client.write_gatt_char(CHAR, data, response=True)
        if data[:1] in (bytes([NOTIFY]), bytes([TIMER])):
            self.last_alert = time.time()

    @property
    def connected(self):
        return bool(self.client and self.client.is_connected)

    async def paired(self):
        proc = await asyncio.create_subprocess_exec("bluetoothctl", "info", self.addr,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return b"Paired: yes" in out

    async def keep_connected(self):
        warned = False
        while True:
            # connecting to an unpaired clock makes the desktop pop a Bluetooth notice every try
            if not await self.paired():
                if not warned:
                    log.warning("%s: %s isn't paired; waiting (checks every minute)", self.name, self.addr)
                    warned = True
                await asyncio.sleep(60)
                continue
            warned = False
            try:
                self.lost.clear()
                self.client = BleakClient(self.addr, disconnected_callback=lambda _: self.lost.set())
                await self.client.connect()
                log.info("%s: connected to %s", self.name, self.addr)
                self.linked_at = datetime.datetime.now()
                with contextlib.suppress(Exception):  # a flash cut off by a lost link would keep strobing
                    await self.write(bytes([statusring.SCENES_DELETE, 0, 0, statusring.FLASH_SLOT]))
                await self.lost.wait()
                log.warning("%s: disconnected", self.name)
            except Exception as e:  # BLE errors come in many types; just retry
                log.warning("%s: connect failed: %s", self.name, e)
            await asyncio.sleep(5)

    async def resync(self):
        """Once a night drop the link so the clock reconnects and reads the time again: it reads it only
        on a fresh connection, and the link can stay up for weeks, through a daylight-saving change."""
        while True:
            await asyncio.sleep(60)
            now = datetime.datetime.now()
            due = datetime.datetime.combine(now.date(), RESYNC_AT)
            if not RESYNC_AT <= now.time() < RESYNC_BY or not self.connected or (self.linked_at or now) >= due:
                continue
            if statusring.busy(self, time.time()) or statusring.counting_down(self, time.time()):
                continue  # a countdown might not survive the drop: wait for it to end
            log.info("%s: dropping the link so the clock reads the time again", self.name)
            try:
                await asyncio.wait_for(self.client.disconnect(), 30)
            except Exception as e:
                log.warning("%s: dropping the link failed: %s", self.name, str(e) or type(e).__name__)
                self.linked_at = now  # not again tonight

    @staticmethod
    def notice(text, color="Lime", anim="Pulse", sound="Radar", mode="Repeat", priority="high", strict=False):
        """The bytes of a Notify command; raises ValueError (or KeyError for priority) on bad names."""
        return bytes([NOTIFY, PRIORITIES[priority], 0, 0]) + build_notice(text, anim, sound, color, mode, strict)

    async def flash(self):
        """The red light flash on all four rings, then its scene deleted, which is tried twice since a
        leftover keeps strobing (a reconnect deletes it too). Best effort: failures are only logged."""
        if not self.connected:
            return
        try:
            await self.write(statusring.flash_bytes())
            self.last_alert = time.time()  # the status ring waits, as after an alert
            await asyncio.sleep(FLASH_S)
        except Exception as e:
            log.warning("%s: flash failed: %s", self.name, e)
        finally:
            for attempt in (1, 2):
                try:
                    await self.write(bytes([statusring.SCENES_DELETE, 0, 0, statusring.FLASH_SLOT]))
                    break
                except Exception as e:
                    log.warning("%s: removing the flash failed (try %d): %s", self.name, attempt, e)

    async def show(self, data, flash=False):
        """Send a notice; with flash, the light flash first (for something due now)."""
        if not flash:
            return await self.write(data)
        async with self.flashing:
            await self.flash()
            await self.write(data)

    async def notify(self, flash=False, **fields):
        await self.show(self.notice(**fields), flash)  # bad names fail before anything is sent

    async def play(self, sound):
        """Play one of the clock's sounds by name (NoneSound stops it) and stop it after SOUND_S."""
        n = enum_value(pb.Sound, sound)  # ValueError on a bad name, before anything is sent
        async with self.sounding:
            if self.sound_stop:
                self.sound_stop.cancel()
                self.sound_stop = None
            await self.write(bytes([SOUND, 0, 0, n]))
            if n:
                self.sound_stop = asyncio.create_task(self.stop_sound())

    async def stop_sound(self):
        await asyncio.sleep(SOUND_S)
        async with self.sounding:  # a play that got the lock first has cancelled this task
            self.sound_stop = None
            try:
                await self.write(bytes([SOUND, 0, 0, 0]))
            except Exception as e:
                log.warning("%s: stopping the sound failed: %s", self.name, e)

    async def read_settings(self):
        if not self.connected:
            raise ConnectionError("clock not connected")
        settings = pb.Settings()
        async with self.lock:
            settings.ParseFromString(await self.client.read_gatt_char(CHAR))
        return settings

    async def update_settings(self, brightness=None, night_mode=None, dnd=None, dnd_from=None, dnd_till=None):
        """Change only the given settings. brightness 0-255 is the low byte of displayBrightness (0 = auto)."""
        s = await self.read_settings()
        if brightness is not None:
            s.displayBrightness = (s.displayBrightness & ~0xFF) | max(0, min(255, int(brightness)))
        if night_mode is not None:
            s.nightModeEnabled = bool(night_mode)
        if dnd is not None:
            s.dnd.recurring = bool(dnd)
        if dnd_from is not None:
            s.dnd.fromHour = int(dnd_from) % 24
        if dnd_till is not None:
            s.dnd.tillHour = int(dnd_till) % 24
        async with self.lock:
            await self.client.write_gatt_char(CHAR, SETTINGS_CMD + s.SerializeToString(), response=True)
        return await self.read_settings()

    async def alert(self, source, keep=False, flash=False, **fields):
        """Show an alert (with flash, the light flash first); with keep, also add it to the unread list
        (even if the clock is offline). The same text again from the same source counts up the item
        already there, and shows the count. Bad names fail before anything is stored."""
        data = self.notice(**fields)
        if keep:
            item = self.unread.add({k: v for k, v in fields.items() if k != "strict"}, source, merge=True)
            if item.get("count", 1) > 1:
                data = self.notice(**{**fields, "text": unread.shown(item)})
        await self.show(data, flash)


LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def same_site(request):
    """A browser page from another site (or a name rebound to 127.0.0.1) mustn't pass as this machine:
    the Host must be the bridge's own, and an Origin, which browsers send on cross-site posts,
    must match it. curl and scripts send no Origin."""
    host = request.host.rsplit(":", 1)[0] if not request.host.endswith("]") else request.host
    origin = request.headers.get("Origin")
    return host in LOCAL_HOSTS and (origin is None or origin == f"http://{request.host}")


def password_check():
    """Requests from this machine pass (see same_site); anything from the network needs HTTP Basic
    auth with the panel password (any username), and a browser's Origin, if sent, must be the panel's own
    page (a saved password mustn't let another site's page post). With no panel_password file, network requests are
    refused. A reverse proxy on this machine would make every request look local: don't put one in front."""
    try:
        password = open(PASSWORD_FILE).read().strip().encode()
    except OSError:
        password = None
        log.info("no panel_password file: the panel and API answer this machine only")

    @web.middleware
    async def check(request, handler):
        if request.remote in LOCAL:
            if not same_site(request):
                return web.Response(status=403, text="not from this machine's own pages")
            return await handler(request)
        origin = request.headers.get("Origin")
        if origin is not None and origin != f"http://{request.host}":  # another site's page using saved credentials
            return web.Response(status=403, text="not from the panel's own page")
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                given = base64.b64decode(auth[6:]).split(b":", 1)[1]
            except Exception:
                given = b""
            if password and hmac.compare_digest(given, password):
                return await handler(request)
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Glance Clock"'})
    return check


async def read_body(request):
    """The request's JSON object, or a 400."""
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="body must be JSON")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="body must be a JSON object")
    return body


def make_app(clocks, shop_url=None):
    default = next(iter(clocks.values()))

    def pick(request, body=None):
        """The clock named by ?clock= or the body's "clock"; the first clock if none."""
        name = (body or {}).get("clock") or request.query.get("clock")
        if name and name not in clocks:
            raise web.HTTPBadRequest(text=f"no clock named {name!r}; have {', '.join(clocks)}")
        return clocks[name] if name else default

    async def notify(request):
        body = await read_body(request)
        clock = pick(request, body)
        if not body.get("text"):
            return web.json_response({"error": "text required"}, status=400)
        fields = {k: body[k] for k in ("text", "color", "anim", "sound", "mode", "priority") if k in body}
        try:
            await clock.alert(str(body.get("source", "api")), keep=bool(body.get("keep")), flash=bool(body.get("flash")),
                              strict=True, **fields)
        except ValueError as e:  # unknown icon, color, animation or sound name
            return web.json_response({"error": str(e)}, status=400)
        except (KeyError, TypeError) as e:
            return web.json_response({"error": f"bad field: {e}"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        log.info("%s: notify: %s", clock.name, fields)
        return web.json_response({"ok": True})

    async def play_sound(request):
        """{"sound": a name from /options}: play it on the clock for a few seconds; NoneSound stops it."""
        body = await read_body(request)
        clock = pick(request, body)
        try:
            await clock.play(str(body.get("sound", "")))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response({"ok": True, "seconds": SOUND_S})

    def clock_json(clock):
        focus = getattr(clock, "focus", None)  # nudges.py: (start of the active stretch, break length), drawn by the status ring
        return {"name": clock.name, "addr": clock.addr, "connected": clock.connected,
                "unread": len(clock.unread.items), "focus": {"since": focus[0], "break_s": focus[1]} if focus else None}

    async def status(request):
        # the top level describes the chosen (default) clock, as before; "clocks" lists them all
        return web.json_response({**clock_json(pick(request)), "clocks": [clock_json(c) for c in clocks.values()]})

    async def list_unread(request):
        return web.json_response(pick(request).unread.items)

    async def clear_unread(request):
        body = await read_body(request)
        clock = pick(request, body)
        cleared = clock.unread.clear(body.get("id"))
        await clock.refresh_ring()
        return web.json_response({"cleared": cleared})

    def settings_json(s):
        return {"brightness": s.displayBrightness & 0xFF, "night_mode": s.nightModeEnabled,
                "dnd": s.dnd.recurring, "dnd_from": s.dnd.fromHour, "dnd_till": s.dnd.tillHour}

    async def get_settings(request):
        try:
            return web.json_response(settings_json(await pick(request).read_settings()))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)

    async def set_settings(request):
        body = await read_body(request)
        keys = ("brightness", "night_mode", "dnd", "dnd_from", "dnd_till")
        clock = pick(request, body)
        try:
            s = await clock.update_settings(**{k: body[k] for k in keys if k in body})
        except (TypeError, ValueError) as e:
            return web.json_response({"error": f"bad value: {e}"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        log.info("%s: settings: %s", clock.name, body)
        return web.json_response(settings_json(s))

    async def add_stages(request):
        """{"name", "stages": [[label, minutes], ...] or [{"label", "minutes"}], "repeat"}"""
        body = await read_body(request)
        try:
            stages = [(s["label"], s["minutes"]) if isinstance(s, dict) else (s[0], s[1]) for s in body.get("stages", [])]
            stages = [(str(l), float(m)) for l, m in stages] * max(1, min(int(body.get("repeat") or 1), 12))
            item = await pickups.add_stages(pick(request, body), str(body.get("name", "")), stages)
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as e:
            return web.json_response({"error": f"bad stages: {e}"}, status=400)
        return web.json_response(item)

    async def add_pickup(request):
        body = await read_body(request)
        kind = body.get("kind", "pickup")
        if not body.get("when") or kind not in ("pickup", "timer", "stop") or (kind == "pickup" and not body.get("name")):
            return web.json_response({"error": "when (and a name for pickups) required"}, status=400)
        try:
            item = await pickups.add(pick(request, body), str(body.get("name", "")).strip(), body["when"], kind)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(item)

    api = brain.client()
    notebook = notes.Notes()

    def from_haiku(name, args):
        """A Haiku tool call -> the same (path, body) shape clockvoice.parse returns."""
        minutes = args.get("minutes")  # a fraction for a timer in seconds
        when = (f"{int(minutes)}m" if minutes == int(minutes) else f"{round(minutes * 60)}s") if minutes else args.get("at")
        target = {"clock": args["clock"]} if args.get("clock") else {}
        if name in ("set_timer", "add_pickup") and when:
            kind = "timer" if name == "set_timer" else "pickup"
            return "/pickup", {"kind": kind, "name": args.get("label") or args.get("name") or "", "when": when, **target}
        if name == "show_message":
            return "/notify", {"text": args["text"].upper()[:120], "color": "White", "sound": "NoneSound",
                               "priority": "critical", **target}
        if name == "set_hard_stop" and when:
            return "/pickup", {"kind": "stop", "name": args.get("label") or "", "when": when, **target}
        if name == "add_note":
            return "/note", {"text": args["text"], **target}
        if name == "cancel":
            return "/cancel", {"what": args.get("what", ""), **target}
        if name == "start_stages":
            stages = [(s.get("label", ""), s.get("minutes", 0)) for s in args.get("stages", [])]
            rounds = max(1, min(int(args.get("repeat") or 1), 12))
            stages = stages * rounds
            if rounds > 1 and stages and re.search(r"BREAK|REST|PAUSE", str(stages[-1][0]).upper()):
                stages = stages[:-1]  # repeated rounds end on the last work stage, not a break
            return "/stages", {"name": args.get("name", ""), "stages": stages, **target}
        if name == "change":
            return "/change", {k: args.get(k) for k in ("what", "new_label", "minutes", "at", "add_minutes", "move_to")} | target
        if name in ("clock_in", "clock_out"):
            return "/shopclock", {"in": name == "clock_in", "done": args.get("done", ""), **target}
        if name == "switch_job":
            return "/shopapi", {"path": "/api/clock/job", "body": {"text": args["text"]}, **target}
        if name == "wait_on_customer":
            body = {"text": args["text"], **({"days": args["days"]} if args.get("days") else {})}
            return "/shopapi", {"path": "/api/followups", "body": body, **target}
        if name == "customer_replied":
            return "/shopapi", {"path": "/api/followups/resolve", "body": {"text": args["text"]}, **target}
        if name == "clear_unread":
            return "/unread/clear", target
        return None

    def set_now():
        """The timers, pickups and stops set on every clock, for Haiku: the item's text (what `what`
        matches), and when it was set and for how long, so "the hour 37 timer" can be found."""
        lines, now = [], time.time()
        for clock in clocks.values():
            for i in clock.unread.items:
                if i.get("due") and i.get("kind") in pickups.KINDS and i["due"] > now - 3600:
                    set_time = i.get("time", i["due"])
                    minutes = max(0, round((i["due"] - set_time) / 60))
                    lines.append(f"- {i['text']} (on {clock.name}; due {datetime.datetime.fromtimestamp(i['due']):%H:%M}; "
                                 f"set {datetime.datetime.fromtimestamp(set_time):%H:%M} for {minutes // 60} h {minutes % 60} min)")
        return "\n".join(lines)

    def locate(args):
        """(clock, item) that a change or cancel means, on whichever clock it is; the named clock first."""
        what = args.get("what") or ""
        named = clocks.get(args.get("clock") or "")
        if named:
            found = pickups.best(pickups.matches(named, what))
            if found:
                return found
        return pickups.best([m for c in clocks.values() for m in pickups.matches(c, what)])

    async def cancel(args):
        """Clear the timer, pickup or stop meant (see locate). Returns the reply."""
        found = locate(args)
        if not found:
            return "There's nothing like that set."
        clock, item = found
        clock.unread.clear(item["id"])
        await clock.refresh_ring()
        where = f" on the {clock.name} clock" if clock is not default else ""
        return f"Cancelled {item['text'].rsplit(' ', 1)[0].title()}{where}."

    async def change(args):
        """Rename, reschedule, snooze or move an existing timer, pickup or stop. Returns the reply."""
        found = locate(args)
        if not found:
            return "There's nothing like that set."
        clock, item = found
        now, due = time.time(), None
        if (args.get("add_minutes") or 0) > 0:  # "five more minutes": added, never "five minutes from now"
            base = now if item["due"] <= now else item["due"]  # going off: from now; still ahead: later
            due = base + int(args["add_minutes"]) * 60
        elif (args.get("minutes") or 0) > 0:
            due = now + int(args["minutes"]) * 60
        elif args.get("at"):
            if not re.fullmatch(r"\d{1,2}:\d{2}", args["at"].strip()):
                raise ValueError(f"I didn't catch the time {args['at']!r}")
            due = pickups.parse_when(args["at"])
        name = args["new_label"].upper()[:60] if args.get("new_label") else None
        target = clocks.get(args.get("move_to") or "")
        moving = target is not None and target is not clock
        if item["kind"] == "stages" and (due is not None or moving):
            return "A staged timer can only be renamed or cancelled. Cancel it and start it again."
        if due is None and name is None and not moving:
            return "What should I change about it?"
        if moving:
            item = await pickups.move(clock, target, item, name=name, due=due)
            return f"Moved to the {target.name} clock. {clockvoice.spoken_due(item)}"
        item = await pickups.change(clock, item, name=name, due=due)
        where = f"On the {clock.name} clock: " if clock is not default else ""
        return f"Changed. {where}{clockvoice.spoken_due(item)}"

    async def shop_clock(clock_in, done=""):
        """Clock in or out through the shop app's desk-button API (GET /api/clock, POST /api/clock/toggle).
        A clock-out with what was done goes to POST /api/clock/out {"text"}: the shop app logs it on the
        instrument's work log when the session was all on one, else keeps the words for its split page.
        It may wait on Haiku rewording the words, hence the longer wait."""
        if not clock_in and done.strip():
            try:
                status, answer = await shopapp.call(shop_url, "POST", "/api/clock/out", {"text": done.strip()},
                                                    timeout=40)
            except shopapp.ERRORS:
                return "I couldn't reach the shop app. Check the clock there before trying again."
            if 200 <= status < 300 and answer.get("say"):
                return str(answer["say"])
            if status != 404:  # an older shop app without it toggles below, without the words
                return "The shop app couldn't clock out."
        status, state = await shopapp.call(shop_url, "GET", "/api/clock")
        if status == 404:
            return "The shop app doesn't have the clock in and out feature yet."
        if status != 200 or "clocked_in" not in state:
            return "The shop app gave an answer I don't understand."
        if state["clocked_in"] == clock_in:
            since = f", since {state['since']}" if clock_in and state.get("since") else ""
            return f"You're already clocked {'in' if clock_in else 'out'}{since}."
        status, state = await shopapp.call(shop_url, "POST", "/api/clock/toggle")
        if not 200 <= status < 300:
            return "The shop app couldn't change the clock."
        if clock_in:
            return f"Clocked in at {state['since']}." if state.get("since") else "Clocked in."
        return "Clocked out. Split the time in the shop app." if state.get("to_assign") else "Clocked out."

    async def voice(request):
        """A spoken command: instant patterns first, Claude Haiku for anything looser. Replies
        with a sentence to say back."""
        body = await read_body(request)
        text = str(body.get("text", "")).strip()
        parsed, via = clockvoice.parse(text, list(clocks)), "pattern"
        if parsed:
            path, args, reply = parsed
        else:
            via = "haiku"
            understood = await brain.understand(api, text, list(clocks), set_now())
            if understood is None:
                return web.json_response({"reply": "Sorry, I didn't get that clock command.", "via": "none"})
            if understood[0] == "reply":
                return web.json_response({"reply": understood[1], "via": via})
            log.info("haiku: %s %s", understood[1], understood[2])
            action = from_haiku(understood[1], understood[2])
            if not action:
                return web.json_response({"reply": "Sorry, I didn't catch when.", "via": via})
            (path, args), reply = action, None
        request_clock = args.get("clock") or ""  # change and cancel search every clock, this one first
        clock = pick(request, args)
        args.pop("clock", None)
        try:
            if path == "/shopclock":
                if not shop_url:
                    reply = "There's no shop app set up in the config."
                else:
                    reply = await shop_clock(args["in"], args.get("done", ""))
                    if reply.startswith("Clocked"):
                        await clock.alert("voice", text="{clock} " + ("CLOCKED IN" if args["in"] else "CLOCKED OUT"),
                                          color="White", sound="NoneSound", priority="critical")
            elif path == "/shopapi":
                if not shop_url:
                    reply = "There's no shop app set up in the config."
                else:
                    status, answer = await shopapp.call(shop_url, "POST", args["path"], args["body"])
                    ok = 200 <= status < 300 and answer.get("ok", True) is not False
                    if status == 404:
                        reply = "The shop app doesn't have that feature yet."
                    else:
                        reply = str(answer.get("say") or ("Done." if ok else "The shop app said no."))
                    if ok and args["path"] == "/api/followups/resolve":
                        due_now = await shopapp.due(shop_url)  # take the resolved nag off the clock now
                        if due_now is not None:
                            shopapp.prune(default, due_now)
            elif path == "/note":
                notebook.add(args["text"])
                await clock.alert("voice", text="{sun} NOTED", color="Aqua", anim="Pulse", sound="NoneSound",
                                  priority="critical")
                reply = "Noted."
            elif path == "/stages":
                item = await pickups.add_stages(clock, args.get("name", ""), args["stages"])
                reply = clockvoice.spoken_due(item)
            elif path == "/change":
                reply = await change({**args, "clock": request_clock})
            elif path == "/cancel":
                reply = await cancel({**args, "clock": request_clock})
            elif path == "/pickup":
                item = await pickups.add(clock, args.get("name", ""), args["when"], args.get("kind", "pickup"))
                reply = clockvoice.spoken_due(item)
            elif path == "/notify":
                await clock.alert("voice", **args)
                reply = reply or "On the clock."
            else:
                clock.unread.clear()
                await clock.refresh_ring()
                reply = reply or "Cleared."
        except ValueError as e:
            return web.json_response({"reply": f"Sorry, {e}.", "via": via})
        except shopapp.ERRORS:
            return web.json_response({"reply": "The shop app isn't answering.", "via": via})
        except Exception:
            log.exception("voice command failed: %r", text)
            return web.json_response({"reply": "The clock isn't answering.", "via": via})
        if clock is not default and path not in ("/change", "/cancel"):  # those say where the item was
            reply = f"{clock.name.capitalize()} clock: {reply}"
        log.info("voice (%s): %r -> %s", via, text, reply)
        return web.json_response({"reply": reply, "via": via})

    async def list_notes(request):
        return web.json_response(notebook.items)

    async def change_note(request):
        body = await read_body(request)
        if body.get("text") and not body.get("id"):
            return web.json_response(notebook.add(str(body["text"])))
        found = notebook.update(body.get("id"), done=body.get("done"), delete=bool(body.get("delete")))
        return web.json_response({"ok": found}, status=200 if found else 404)

    async def get_nudges(request):
        return web.json_response(pick(request).nudges.values)

    async def set_nudges(request):
        body = await read_body(request)
        clock = pick(request, body)
        changes = {k: v for k, v in body.items() if k != "clock"}
        try:
            if "snooze_minutes" in changes:  # the panel's snooze buttons; 0 = unsnooze, -1 = rest of today
                minutes = int(changes.pop("snooze_minutes"))
                end_of_day = datetime.datetime.now().replace(hour=23, minute=59, second=59).timestamp()
                changes["snooze_until"] = 0 if minutes == 0 else end_of_day if minutes < 0 else time.time() + minutes * 60
            values = clock.nudges.update(changes)
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        log.info("%s: nudges: %s", clock.name, changes)
        return web.json_response(values)

    async def get_screens(request):
        return web.json_response(pick(request).screens.json())

    async def set_screens(request):
        """{"status", "forecast", "schedule", "animation", "screensaver": true or false}; each screen's watcher puts
        it on or takes it off the clock within half a minute (later if an alert is showing)."""
        body = await read_body(request)
        clock = pick(request, body)
        try:
            changes = clock.screens.update({k: v for k, v in body.items() if k != "clock"})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except OSError as e:
            return web.json_response({"error": f"couldn't save: {e}"}, status=500)
        log.info("%s: screens: %s", clock.name, changes)
        return web.json_response(clock.screens.json())

    async def get_calendars(request):
        return web.json_response(pick(request).calendars.json())

    async def set_calendars(request):
        """{"add": {"name", "url"}}: the secret iCal address is read once to check it's a calendar, then kept
        on this machine only; or {"remove": id}. Returns the clock's calendars by name and host, never an address."""
        body = await read_body(request)
        clock = pick(request, body)
        cals = clock.calendars
        try:
            if isinstance(body.get("add"), dict):
                name, url = cals.check(body["add"].get("name"), body["add"].get("url"))
                try:
                    events = await calwatch.check(url)
                except Exception as e:
                    return web.json_response({"error": f"couldn't read a calendar there: {calwatch.why(e)}"}, status=400)
                entry = cals.add(name, url)
                cals.status[entry["id"]] = {"events": len(events), "error": None, "fetched": time.time()}
                log.info("%s: calendar %r added, %d events in the next 24 h", clock.name, name, len(events))
            elif isinstance(body.get("remove"), str):
                name = cals.name(body["remove"])
                cals.remove(body["remove"])
                log.info("%s: calendar %r removed", clock.name, name)
            else:
                return web.json_response({"error": 'send {"add": {"name", "url"}} or {"remove": id}'}, status=400)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except KeyError:
            return web.json_response({"error": "no such calendar on this clock"}, status=404)
        except OSError as e:
            return web.json_response({"error": f"couldn't save: {e.strerror or type(e).__name__}"}, status=500)
        return web.json_response(cals.json())

    async def get_message(request):
        return web.json_response(pick(request).message.json())

    async def set_message(request):
        """{"top", "bottom", "top_color", "bottom_color", "minutes"}: words on the rings now, taking the whole
        face until the minutes are up (0: until cleared); or {"clear": true} to take them off."""
        body = await read_body(request)
        clock = pick(request, body)
        try:
            if body.get("clear") is True:
                await clock.message.clear(clock)
            else:
                await clock.message.show(clock, **{k: body[k] for k in ("top", "bottom", "top_color", "bottom_color", "minutes")
                                                   if k in body})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except ringwords.NotShown as e:
            return web.json_response({"error": str(e)}, status=503)
        except OSError as e:
            return web.json_response({"error": f"couldn't save: {e.strerror or type(e).__name__}"}, status=500)
        return web.json_response(clock.message.json())

    async def options(request):
        from glance import ICONS
        return web.json_response({"colors": list(pb.Color.keys()), "animations": list(pb.Animation.keys()),
                                  "sounds": list(pb.Sound.keys()), "priorities": list(PRIORITIES),
                                  "icons": list(ICONS), "ring_font": ringwords.FONT})

    async def panel(request):
        return web.FileResponse(PANEL)

    app = web.Application(middlewares=[password_check()])
    app.add_routes([web.post("/notify", notify), web.post("/sound", play_sound), web.get("/status", status),
                    web.get("/unread", list_unread), web.post("/unread/clear", clear_unread),
                    web.get("/settings", get_settings), web.post("/settings", set_settings),
                    web.get("/options", options), web.post("/pickup", add_pickup), web.post("/stages", add_stages), web.post("/voice", voice), web.get("/nudges", get_nudges), web.get("/notes", list_notes), web.post("/notes", change_note), web.post("/nudges", set_nudges), web.get("/screens", get_screens), web.post("/screens", set_screens), web.get("/calendars", get_calendars), web.post("/calendars", set_calendars), web.get("/message", get_message), web.post("/message", set_message), web.get("/", panel)])
    return app


async def supervised(label, start):
    """Run a watcher forever: if it crashes, log it and start it again, so one bad email, event or
    setting can't silently switch a feature off. A watcher that returns is done."""
    while True:
        try:
            return await start()
        except Exception:
            log.exception("%s crashed; restarting in 30 s", label)
            await asyncio.sleep(30)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()
    time_server = await start_time_server()  # keep a reference so it stays registered
    clocks, tasks = {}, []
    for name, c in cfg["clocks"].items():
        clock = clocks[name] = Clock(name, c["address"])
        clock.nudges = nudges.Settings(name, c.get("nudges"))
        clock.screens = screens.Screens(name, c)
        clock.calendars = calendars.Calendars(name, c)
        clock.message = ringwords.Message(name)
        watchers = {"connection": clock.keep_connected, "resync": clock.resync, "replay": lambda clock=clock: unread.replay(clock),
                    "pickups": lambda clock=clock: pickups.watch(clock), "nudges": lambda clock=clock: nudges.watch(clock),
                    "ring": lambda clock=clock: statusring.watch(clock), "schedule": lambda clock=clock: schedulering.watch(clock),
                    "animation": lambda clock=clock: animscreen.watch(clock),
                    "screensaver": lambda clock=clock: screensaver.watch(clock), "words": lambda clock=clock: ringwords.watch(clock)}
        if "mail" in c:
            watchers["mail"] = lambda clock=clock, c=c: mailwatch.watch(clock, c["mail"])
        if "texts" in c:
            watchers["texts"] = lambda clock=clock, c=c: textwatch.watch(clock, c["texts"])
        watchers["calendar"] = lambda clock=clock: calwatch.watch(clock)  # calendars can be added on the panel
        # without a [forecast] section it only makes sure no forecast is left on the clock
        watchers["forecast"] = lambda clock=clock, c=c: forecast.watch(clock, c.get("forecast") or {})
        if not tasks and cfg.get("shop_app", {}).get("url"):  # follow-up nags go to the first clock
            watchers["followups"] = lambda clock=clock: shopapp.watch_followups(clock, cfg["shop_app"])
        tasks += [asyncio.create_task(supervised(f"{name} {label}", start)) for label, start in watchers.items()]
    if cfg.get("shop_app", {}).get("url"):  # pickups both ways with the shop app, across all clocks
        tasks.append(asyncio.create_task(supervised("pickup sync", lambda: pickupsync.watch(clocks, cfg["shop_app"]["url"]))))
    runner = web.AppRunner(make_app(clocks, cfg.get("shop_app", {}).get("url")))
    await runner.setup()
    port = cfg.get("port", 8740)
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("clocks %s; listening on port %d (network requests need the panel password)", ", ".join(clocks), port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
