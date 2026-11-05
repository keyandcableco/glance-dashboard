"""An optional shop app (config [shop_app] url) with clock in/out, current job and customer follow-ups.

The bridge calls its JSON API for voice commands, and nags on the first clock about follow-ups that
are due (GET /api/followups/due -> {"due": [{"id", "label", "waiting_on"}, ...]}): once at each of
followup_hours (default 10 and 15) per day. A nagged follow-up stays in the unread list until the
shop app no longer lists it as due (resolved), checked at each nag hour and after a voice resolve.
    [shop_app]
    url = "http://192.168.1.50:8001"
    followup_hours = [10, 15]
"""
import asyncio
import datetime
import logging

import aiohttp

NAG_HOURS = (10, 15)
SOURCE = "followup"
LOOK = {"color": "OrangeRed", "anim": "Wave", "sound": "Bells", "priority": "high"}
ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)

log = logging.getLogger("glanced.shopapp")
nagged = set()  # (date, follow-up id, hour); kept here so a watcher restart doesn't nag again


async def call(url, method, path, body=None, timeout=10):
    """(HTTP status, JSON object reply) from the shop app; {} if the reply isn't a JSON object.
    Raises aiohttp.ClientError or asyncio.TimeoutError if it can't be reached."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
        async with http.request(method, url.rstrip("/") + path, json=body) as r:
            try:
                data = await r.json(content_type=None)
            except ValueError:
                data = None
            return r.status, data if isinstance(data, dict) else {}


def nag_text(f):
    return f"{{phone}} FOLLOW UP {f.get('label', '?')}: {f.get('waiting_on', '')}"[:120]


async def due(url):
    """The follow-ups due now, or None if they can't be read."""
    try:
        status, answer = await call(url, "GET", "/api/followups/due")
    except ERRORS as e:
        log.warning("shop app unreachable for follow-ups: %s", e)
        return None
    if status != 200:
        if status != 404:  # 404: this shop app has no follow-ups
            log.warning("shop app follow-ups: HTTP %s", status)
        return None
    items = answer.get("due")
    return [f for f in items if isinstance(f, dict) and "id" in f] if isinstance(items, list) else []


def prune(clock, due_now):
    """Drop nagged follow-ups from the unread list once they're no longer due (resolved)."""
    still = {nag_text(f) for f in due_now}
    for item in list(clock.unread.items):
        if item.get("source") == SOURCE and item["text"] not in still:
            clock.unread.clear(item["id"])


async def watch_followups(clock, cfg):
    hours = cfg.get("followup_hours", NAG_HOURS)
    if not isinstance(hours, (list, tuple)) or not all(isinstance(h, int) and 0 <= h <= 23 for h in hours):
        log.warning("followup_hours should be a list of hours like [10, 15]; using %s", list(NAG_HOURS))
        hours = NAG_HOURS
    while True:
        await asyncio.sleep(60)
        now = datetime.datetime.now()
        if now.hour not in hours or now.minute > 10:
            continue
        due_now = await due(cfg["url"])
        if due_now is None:
            continue
        prune(clock, due_now)
        for f in due_now:
            key = (now.date(), f["id"], now.hour)
            if key in nagged:
                continue
            nagged.add(key)
            text = nag_text(f)
            try:
                keep = not any(i["text"] == text for i in clock.unread.items)  # once, until resolved or cleared
                await clock.alert(SOURCE, keep=keep, text=text, **LOOK)
                log.info("%s: follow-up nag: %s", clock.name, text)
            except Exception as e:
                log.warning("%s: follow-up nag failed: %s", clock.name, e)
