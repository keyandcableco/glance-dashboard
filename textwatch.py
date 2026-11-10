"""Watch desktop notifications for texts from chosen people (Google Messages for Web in Chrome), alert a
clock, and clear the clock's item again once the text has been read.

Configured per clock in config.toml [clocks.<name>.texts]: names = ["..."].

Listens with dbus-monitor to the notification traffic on the session bus: Notify calls (their strings
are app_name, app_icon, summary = the sender for Messages, and body; replaces_id is the first uint32),
the ids the notification server returns for them, and their closing. When Chrome closes one of those
notifications itself (CloseNotification, or NotificationClosed reason 3) or it's dismissed (reason 2),
the person's item comes off the clock. One that only timed out (reason 1) doesn't count. Ids are kept
in memory, so a text that came before a bridge restart has to be cleared by hand.
"""
import asyncio
import logging
import re
import time

APPS = ("chrome", "chromium", "messages")
ALERT = {"color": "MediumVioletRed", "anim": "Wave", "sound": "Flowers", "priority": "critical"}
REPEAT_GAP = 20  # seconds; Chrome can post the same text twice
CLEAR_AFTER = 10  # seconds; a new text from the same person in between means Chrome was only replacing it
READ_REASONS = (2, 3)  # NotificationClosed: 1 expired, 2 dismissed, 3 closed by the app
RULES = ("interface='org.freedesktop.Notifications'",
         "type='method_return',sender='org.freedesktop.Notifications'")  # the ids Notify returns

log = logging.getLogger("glanced.texts")


def alert_for(app, summary, names):
    name = name_for(app, summary, names)
    return {"text": item_text(name), **ALERT} if name else None


def name_for(app, summary, names):
    if not any(a in app.lower() for a in APPS):
        return None
    return next((n for n in names if n.lower() in summary.lower()), None)


def item_text(name):
    return f"{{heart}} {name.upper()}"


async def messages(readline):
    """dbus-monitor output as (header fields, member, strings, uints) per message. A message has no end
    marker, so it's complete at the next header or after half a second without output."""
    head, strings, uints = None, [], []
    while True:
        try:
            raw = await (asyncio.wait_for(readline(), 0.5) if head is not None else readline())
        except asyncio.TimeoutError:
            raw = b"quiet"
        line = raw.decode(errors="replace").strip()
        if head is not None and (not raw or raw == b"quiet" or line.startswith(("method ", "signal ", "error "))):
            yield head, head.get("member", ""), strings, uints
            head = None
        if not raw:
            return
        if line.startswith(("method ", "signal ", "error ")):
            head = dict(re.findall(r"(\w+)=([^\s;]+)", line))
            head["kind"] = line.split(" time=")[0]
            strings, uints = [], []
        elif head is not None and line.startswith('string "'):
            strings.append(line[8:-1])
        elif head is not None and line.startswith("uint32 "):
            uints.append(int(line.split()[1]))


async def follow(clock, names, readline):
    last = 0.0
    waiting = {}  # (caller, serial) of a Notify for a watched name -> name, until its id comes back
    ids = {}  # notification id -> name
    clearing = {}  # name -> the task that clears it after CLEAR_AFTER

    async def clear_later(name):
        try:
            await asyncio.sleep(CLEAR_AFTER)
            for item in [i for i in clock.unread.items if i.get("source") == "text" and i["text"] == item_text(name)]:
                clock.unread.clear(item["id"])
                log.info("%s: text from %s read; cleared from the clock", clock.name, name)
            for i in [i for i, n in ids.items() if n == name]:
                del ids[i]  # older notifications for the same person are read too
        finally:
            clearing.pop(name, None)

    def read(notification_id):
        name = ids.pop(notification_id, None)
        if name and name not in clearing:
            clearing[name] = asyncio.create_task(clear_later(name))

    async for head, member, strings, uints in messages(readline):
        if member == "Notify" and len(strings) >= 3:
            app, summary = strings[0], strings[2]
            name = name_for(app, summary, names)
            if not name:
                continue
            waiting[(head.get("sender"), head.get("serial"))] = name
            if uints and uints[0]:
                ids[uints[0]] = name  # replacing an earlier notification keeps its id
            if name in clearing:
                clearing.pop(name).cancel()
            if time.monotonic() - last > REPEAT_GAP:
                last = time.monotonic()
                try:
                    await clock.alert("text", keep=True, **alert_for(app, summary, names))
                    log.info("text alert from %s via %s", summary, app)
                except Exception as e:
                    log.warning("text alert failed: %s", e)
        elif head["kind"] == "method return" and uints:
            name = waiting.pop((head.get("destination"), head.get("reply_serial")), None)
            if name:
                ids[uints[0]] = name
        elif member == "CloseNotification" and uints:
            read(uints[0])
        elif member == "NotificationClosed" and len(uints) >= 2 and uints[1] in READ_REASONS:
            read(uints[0])


async def watch(clock, cfg):
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "dbus-monitor", "--session", *RULES, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.warning("%s: dbus-monitor isn't installed; text alerts are off", clock.name)
            return
        log.info("%s: watching desktop notifications for %s", clock.name, ", ".join(cfg["names"]))
        await follow(clock, cfg["names"], proc.stdout.readline)
        await proc.wait()
        log.warning("dbus-monitor exited; restarting")
        await asyncio.sleep(5)
