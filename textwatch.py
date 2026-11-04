"""Watch desktop notifications for texts from chosen people (Google Messages for Web in Chrome) and alert a clock.

Configured per clock in config.toml [clocks.<name>.texts]: names = ["..."].

Listens with dbus-monitor to Notify calls on the session bus. Their first four strings are
app_name, app_icon, summary (the sender, for Messages) and body.
"""
import asyncio
import logging
import time

APPS = ("chrome", "chromium", "messages")
ALERT = {"color": "MediumVioletRed", "anim": "Wave", "sound": "Flowers", "priority": "critical"}
REPEAT_GAP = 20  # seconds; Chrome can post the same text twice

log = logging.getLogger("glanced.texts")


def alert_for(app, summary, names):
    if not any(a in app.lower() for a in APPS):
        return None
    name = next((n for n in names if n.lower() in summary.lower()), None)
    return {"text": f"{{heart}} {name.upper()}", **ALERT} if name else None


async def watch(clock, cfg):
    last = 0.0
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "dbus-monitor", "--session", "interface='org.freedesktop.Notifications',member='Notify'",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.warning("%s: dbus-monitor isn't installed; text alerts are off", clock.name)
            return
        log.info("%s: watching desktop notifications for %s", clock.name, ", ".join(cfg["names"]))
        strings, in_notify = [], False
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith("method call") and "member=Notify" in line:
                strings, in_notify = [], True
            elif in_notify and line.startswith('string "'):
                strings.append(line[8:-1])
                if len(strings) == 4:
                    in_notify = False
                    app, _icon, summary, _body = strings
                    alert = alert_for(app, summary, cfg["names"])
                    if alert and time.monotonic() - last > REPEAT_GAP:
                        last = time.monotonic()
                        try:
                            await clock.alert("text", keep=True, **alert)
                            log.info("text alert from %s via %s", summary, app)
                        except Exception as e:
                            log.warning("text alert failed: %s", e)
        await proc.wait()
        log.warning("dbus-monitor exited; restarting")
        await asyncio.sleep(5)
