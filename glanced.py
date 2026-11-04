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

import aiohttp
from aiohttp import web
from bleak import BleakClient
from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

import brain
import calwatch
import shopapp
import statusring
import clockvoice
import config
import mailwatch
import notes
import nudges
import pickups
import textwatch
import unread
from glance import CHAR, NOTIFY, PRIORITIES, build_notice, encode_text
import glance_pb2 as pb  # on sys.path via glance

PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
SETTINGS_CMD = bytes([5, 0, 0, 0])
TIMER, TIMER_STOP = 3, 10
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


async def start_time_server():
    server = BlessServer(name="glanced")
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
        self.last_alert = 0.0  # when an alert or countdown was last sent (the status ring waits after it)

    async def countdown(self, due, owner, text):
        """Ask for a ring countdown to `due` for unread item `owner`. The clock has one ring
        timer, so it goes to the soonest wanted countdown; refresh_ring hands it on."""
        for item in self.unread.items:
            if item["id"] == owner:
                item.update(ring_due=due, ring_text=text[:24] or "TIME")
        self.unread._save()
        await self.refresh_ring()

    async def refresh_ring(self):
        """Put the soonest wanted countdown on the ring; stop it if none is left. Called on
        every change and every 20 s, so a later countdown takes over when one ends or is cleared."""
        now = time.time()
        wanted = [i for i in self.unread.items if i.get("ring_due", 0) > now]
        best = min(wanted, key=lambda i: i["ring_due"], default=None)
        if best is None:
            if self.ring_unknown or (self.ring and self.ring[0] > now):  # cleared before it finished, or left from before a restart
                with contextlib.suppress(Exception):  # not connected yet: try again on the next check
                    await self.write(bytes([TIMER_STOP, 0, 0, 0]))
                    self.ring, self.ring_unknown = None, False
            return
        if self.ring and self.ring[1] == best["id"] and self.ring[0] > now:
            return
        timer = pb.Timer()
        timer.intervals.add(duration=max(1, int(best["ring_due"] - now)))
        timer.finalText.add(text=encode_text(best["ring_text"]))
        try:
            await self.write(bytes([TIMER, 0, 0, 0]) + timer.SerializeToString())
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
                await self.lost.wait()
                log.warning("%s: disconnected", self.name)
            except Exception as e:  # BLE errors come in many types; just retry
                log.warning("%s: connect failed: %s", self.name, e)
            await asyncio.sleep(5)

    @staticmethod
    def notice(text, color="Lime", anim="Pulse", sound="Radar", mode="Repeat", priority="high", strict=False):
        """The bytes of a Notify command; raises ValueError (or KeyError for priority) on bad names."""
        return bytes([NOTIFY, PRIORITIES[priority], 0, 0]) + build_notice(text, anim, sound, color, mode, strict)

    async def notify(self, **fields):
        await self.write(self.notice(**fields))

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

    async def alert(self, source, keep=False, **fields):
        """Show an alert; with keep, also add it to the unread list (even if the clock is offline).
        Bad names fail before anything is stored."""
        data = self.notice(**fields)
        if keep:
            self.unread.add({k: v for k, v in fields.items() if k != "strict"}, source)
        await self.write(data)


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
    auth with the panel password (any username). With no panel_password file, network requests are
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
            await clock.alert(str(body.get("source", "api")), keep=bool(body.get("keep")), strict=True, **fields)
        except ValueError as e:  # unknown icon, color, animation or sound name
            return web.json_response({"error": str(e)}, status=400)
        except (KeyError, TypeError) as e:
            return web.json_response({"error": f"bad field: {e}"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        log.info("%s: notify: %s", clock.name, fields)
        return web.json_response({"ok": True})

    def clock_json(clock):
        return {"name": clock.name, "addr": clock.addr, "connected": clock.connected,
                "unread": len(clock.unread.items)}

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

    async def add_pickup(request):
        body = await read_body(request)
        kind = body.get("kind", "pickup")
        if not body.get("when") or kind not in pickups.KINDS or (kind == "pickup" and not body.get("name")):
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
        when = f"{args['minutes']}m" if args.get("minutes") else args.get("at")
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
        if name == "change":
            return "/change", {k: args.get(k) for k in ("what", "new_label", "minutes", "at", "add_minutes", "move_to")} | target
        if name in ("clock_in", "clock_out"):
            return "/shopclock", {"in": name == "clock_in", **target}
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
        lines = []
        for clock in clocks.values():
            for i in clock.unread.items:
                if i.get("due") and i.get("kind") in pickups.KINDS:
                    minutes = round((i["due"] - i["time"]) / 60)
                    set_at = datetime.datetime.fromtimestamp(i["time"]).strftime("%H:%M")
                    lines.append(f"- {i['text']} (on {clock.name}; set {set_at} for {minutes // 60} h {minutes % 60} min)")
        return "\n".join(lines)

    def cancel(clock, what):
        """Clear the timer, pickup or stop whose label or name has these words; with none, the one
        going off, else the newest."""
        item = pickups.find(clock, what)
        if item:
            clock.unread.clear(item["id"])
        return item

    async def change(clock, args):
        """Rename, reschedule, snooze or move an existing timer, pickup or stop. Returns the reply."""
        item = pickups.find(clock, args.get("what") or "")
        if not item:
            return "There's nothing like that set."
        now = time.time()
        due = None
        if args.get("minutes"):
            due = now + int(args["minutes"]) * 60
        elif args.get("at"):
            due = pickups.parse_when(args["at"])
        elif args.get("add_minutes"):
            base = now if item["due"] <= now else item["due"]  # going off: from now; still ahead: later
            due = base + int(args["add_minutes"]) * 60
        name = args["new_label"].upper()[:60] if args.get("new_label") else None
        if due is None and name is None and not args.get("move_to"):
            return "What should I change about it?"
        if due is not None or name is not None:
            item = await pickups.change(clock, item, name=name, due=due)
        target = clocks.get(args.get("move_to") or "")
        if target and target is not clock:
            item = await pickups.move(clock, target, item)
            return f"Moved to the {target.name} clock: {clockvoice.spoken_due(item)}"
        return "Changed. " + clockvoice.spoken_due(item)

    async def shop_clock(clock_in):
        """Clock in or out through the shop app's desk-button API (GET /api/clock, POST /api/clock/toggle)."""
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
        clock = pick(request, args)
        args.pop("clock", None)
        try:
            if path == "/shopclock":
                if not shop_url:
                    reply = "There's no shop app set up in the config."
                else:
                    reply = await shop_clock(args["in"])
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
            elif path == "/change":
                reply = await change(clock, args)
            elif path == "/cancel":
                item = cancel(clock, args.get("what", ""))
                await clock.refresh_ring()
                reply = f"Cancelled {item['text'].rsplit(' ', 1)[0].title()}." if item else "There's nothing like that set."
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
        if clock is not default:
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

    async def options(request):
        from glance import ICONS
        return web.json_response({"colors": list(pb.Color.keys()), "animations": list(pb.Animation.keys()),
                                  "sounds": list(pb.Sound.keys()), "priorities": list(PRIORITIES),
                                  "icons": list(ICONS)})

    async def panel(request):
        return web.FileResponse(PANEL)

    app = web.Application(middlewares=[password_check()])
    app.add_routes([web.post("/notify", notify), web.get("/status", status),
                    web.get("/unread", list_unread), web.post("/unread/clear", clear_unread),
                    web.get("/settings", get_settings), web.post("/settings", set_settings),
                    web.get("/options", options), web.post("/pickup", add_pickup), web.post("/voice", voice), web.get("/nudges", get_nudges), web.get("/notes", list_notes), web.post("/notes", change_note), web.post("/nudges", set_nudges), web.get("/", panel)])
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
        watchers = {"connection": clock.keep_connected, "replay": lambda clock=clock: unread.replay(clock),
                    "pickups": lambda clock=clock: pickups.watch(clock), "nudges": lambda clock=clock: nudges.watch(clock)}
        ring = statusring.watch if c.get("status_ring", True) else statusring.clear
        watchers["ring"] = lambda clock=clock, ring=ring: ring(clock)
        if "mail" in c:
            watchers["mail"] = lambda clock=clock, c=c: mailwatch.watch(clock, c["mail"])
        if "texts" in c:
            watchers["texts"] = lambda clock=clock, c=c: textwatch.watch(clock, c["texts"])
        if "calendar" in c:
            watchers["calendar"] = lambda clock=clock, c=c: calwatch.watch(clock, c["calendar"])
        if not tasks and cfg.get("shop_app", {}).get("url"):  # follow-up nags go to the first clock
            watchers["followups"] = lambda clock=clock: shopapp.watch_followups(clock, cfg["shop_app"])
        tasks += [asyncio.create_task(supervised(f"{name} {label}", start)) for label, start in watchers.items()]
    runner = web.AppRunner(make_app(clocks, cfg.get("shop_app", {}).get("url")))
    await runner.setup()
    port = cfg.get("port", 8740)
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("clocks %s; listening on port %d (network requests need the panel password)", ", ".join(clocks), port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
