"""Pickups and drop-offs shared with the shop app (config [shop_app] url), so they land on its
calendar feed and Today page.

Every SYNC_SECONDS, across all clocks:
- a pickup or drop-off added or changed on a clock (voice, panel, `pickup`, `dropoff`) is sent to
  the shop app (POST /api/pickups {"name", "at", "kind", "source": "glance"}, with "id" once it has one);
- one cleared on a clock before its time is cancelled there (POST /api/pickups/cancel {"id"});
- a drop-off confirmed come in on a clock (arrive(): voice, the panel's Arrived) is confirmed there
  too (POST /api/pickups/arrived {"id"});
- one typed into the shop app is put on the first clock, where it alerts like any other;
- one cancelled in the shop app comes off the clock, if it's still ahead, and so does a drop-off
  confirmed there ("arrived_at").
Timers and hard stops stay on the clock only. If the shop app can't be reached, nothing is lost:
the next pass tries again. `pickups-sync.json` keeps the shop ids last seen on a clock (to tell a
clear), and cancels and arrivals not yet sent, across restarts.
"""
import asyncio
import datetime
import json
import logging
import time
from pathlib import Path

import pickups
import shopapp

SYNC_SECONDS = 60
STATE = Path(__file__).parent / "pickups-sync.json"
OLD_SECONDS = 3600  # a pickup already this far past when syncing starts isn't sent
SHARED = ("pickup", "dropoff")  # the kinds the shop app keeps

log = logging.getLogger("glanced.pickupsync")
_state = None  # shared by watch() and arrive(), loaded once
_arrived = set()  # clock item ids that came in before the shop app had answered with their id


def load_state():
    try:
        state = json.loads(STATE.read_text())
        return {"seen": {int(k): float(v) for k, v in state.get("seen", {}).items()},
                "cancel": [int(i) for i in state.get("cancel", [])],
                "arrive": [int(i) for i in state.get("arrive", [])]}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"seen": {}, "cancel": [], "arrive": []}


def state():
    global _state
    if _state is None:
        _state = load_state()
    return _state


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen": state["seen"], "cancel": state["cancel"], "arrive": state["arrive"]}))
    tmp.replace(STATE)


async def arrive(clock, item):
    """A drop-off has come in (said on a clock, or the panel's Arrived): off the clock with no more
    nagging, and confirmed in the shop app on the next pass. One the shop app hasn't got an id for
    yet only comes off the clock. Queued before it's cleared, so the pass never takes it for a clear."""
    st = state()
    sid = item.get("shop_id")
    if sid:
        st["seen"].pop(sid, None)
        if sid not in st["arrive"]:
            st["arrive"].append(sid)
        save_state(st)
    else:  # its first send may be waiting on the shop app right now: send_changes queues it then
        _arrived.add(item["id"])
    clock.unread.clear(item["id"])
    await clock.refresh_ring()
    log.info("%s: drop-off in: %s", clock.name, item["text"])


def local_iso(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def on_clocks(clocks):
    """{shop id: (clock, item)} for every pickup on a clock that the shop app knows."""
    return {i["shop_id"]: (clock, i) for clock in clocks.values() for i in clock.unread.items
            if i.get("kind") in SHARED and i.get("shop_id")}


async def take_off(clock, item, state, why="cancelled"):
    """Cancelled (or, a drop-off, confirmed come in) in the shop app: off the clock, and not cancelled back."""
    state["seen"].pop(item.get("shop_id"), None)
    if item in clock.unread.items:
        clock.unread.clear(item["id"])
        await clock.refresh_ring()
        log.info("%s: %s %s in the shop app: %s", clock.name, item["kind"], why, item["text"])


def note_clears(state, clocks, now):
    """Queue a cancel for each pickup or drop-off seen on a clock that's gone from it before its time
    (not one that came in: arrive() queued that)."""
    current = on_clocks(clocks)
    for sid, due in state["seen"].items():
        if sid not in current and due > now and sid not in state["cancel"] and sid not in state["arrive"]:
            state["cancel"].append(sid)


async def send_cancels(url, state):
    """Cancel in the shop app what was cleared on a clock before its time."""
    for sid in list(state["cancel"]):
        status, _ = await shopapp.call(url, "POST", "/api/pickups/cancel", {"id": sid})
        if status in (200, 404):  # 404: already cancelled there
            state["cancel"].remove(sid)
            log.info("pickup %s cleared on the clock: cancelled in the shop app", sid)
        else:
            log.warning("shop app pickup cancel %s: HTTP %s", sid, status)


async def send_arrivals(url, state):
    """Confirm in the shop app the drop-offs that came in on a clock."""
    for sid in list(state["arrive"]):
        status, _ = await shopapp.call(url, "POST", "/api/pickups/arrived", {"id": sid})
        if status in (200, 404):  # 404: cancelled there, or a shop app without drop-offs
            state["arrive"].remove(sid)
            log.info("drop-off %s in on the clock: confirmed in the shop app", sid)
        else:
            log.warning("shop app drop-off arrived %s: HTTP %s", sid, status)


async def send_changes(clocks, url, state, now):
    """Send each pickup that's new or changed since it was last sent. "ref" (the clock item's id)
    makes a resend after a lost reply update the same one instead of adding a second."""
    for clock in clocks.values():
        for item in list(clock.unread.items):
            if item.get("kind") not in SHARED or not item.get("due"):
                continue
            want = [item.get("name", ""), round(item["due"])]
            if item.get("shop_sent") == want or (not item.get("shop_id") and item["due"] < now - OLD_SECONDS):
                continue
            body = {"name": item.get("name") or ("Drop-off" if item["kind"] == "dropoff" else "Pickup"),
                    "at": local_iso(item["due"]), "kind": item["kind"], "source": "glance", "ref": item["id"]}
            if item.get("shop_id"):
                body["id"] = item["shop_id"]
            status, answer = await shopapp.call(url, "POST", "/api/pickups", body)
            if status == 200 and isinstance(answer.get("pickup"), dict):
                item["shop_id"], item["shop_sent"] = answer["pickup"]["id"], want
                if item["id"] in _arrived:  # came in while this send was out: confirm it, don't cancel it
                    _arrived.discard(item["id"])
                    if item["shop_id"] not in state["arrive"]:
                        state["arrive"].append(item["shop_id"])
                    continue
                state["seen"][item["shop_id"]] = item["due"]  # so a clear before this pass ends is caught
                clock.unread._save()
                log.info("%s: %s sent to the shop app: %s", clock.name, item["kind"], item["text"])
            elif status == 404 and not item.get("shop_id"):
                return  # this shop app has no pickups
            elif status == 404:  # cancelled there meanwhile
                if item["due"] > now:
                    await take_off(clock, item, state)
                else:
                    item["shop_sent"] = want
                    clock.unread._save()
            else:
                log.warning("shop app pickup save: HTTP %s %s", status, answer.get("say", ""))


async def sync_once(clocks, url, state):
    now = time.time()
    try:
        await exchange(clocks, url, state, now)
    finally:  # even when the shop app stopped answering partway, a clear during this pass is caught
        note_clears(state, clocks, now)
        state["seen"] = {sid: item["due"] for sid, (_clock, item) in on_clocks(clocks).items()}
        save_state(state)


async def exchange(clocks, url, state, now):
    note_clears(state, clocks, now)
    await send_cancels(url, state)
    await send_arrivals(url, state)
    await send_changes(clocks, url, state, now)
    status, answer = await shopapp.call(url, "GET", "/api/pickups")
    if status == 200 and isinstance(answer.get("pickups"), list):
        listed = {p["id"]: p for p in answer["pickups"] if isinstance(p, dict) and "id" in p}
        cancelled = set(answer.get("cancelled") or [])
        for sid, (clock, item) in on_clocks(clocks).items():
            if sid in listed and listed[sid].get("arrived_at"):  # a drop-off confirmed in the shop app
                await take_off(clock, item, state, "come in")
                continue
            if sid in listed or item["due"] <= now:
                continue
            if sid in cancelled:
                await take_off(clock, item, state)
            else:  # unknown there (its data restored or replaced): send it again rather than lose it
                item.pop("shop_id", None)
                item.pop("shop_sent", None)
                state["seen"].pop(sid, None)
                clock.unread._save()
                log.warning("%s: pickup %s missing from the shop app; sending it again", clock.name, sid)
        first = next(iter(clocks.values()))
        current = on_clocks(clocks)
        for sid, p in listed.items():
            if (p.get("source") == "glance" or sid in current or sid in state["cancel"] or sid in state["arrive"]
                    or p.get("arrived_at")):
                continue
            try:
                due = datetime.datetime.fromisoformat(str(p.get("at"))).timestamp()
            except ValueError:
                continue
            if due <= now:
                continue
            kind = "dropoff" if p.get("kind") == "dropoff" else "pickup"
            name = str(p.get("name") or ("Drop-off" if kind == "dropoff" else "Pickup"))
            state["seen"][sid] = due  # before the add's awaits: a clear during them is caught
            item = await pickups.add(first, name, None, kind, due=due)
            item["shop_id"], item["shop_sent"] = sid, [name, round(due)]
            first.unread._save()
            log.info("%s: %s from the shop app: %s", first.name, kind, item["text"])
    elif status != 404:  # 404: this shop app has no pickups
        log.warning("shop app pickups: HTTP %s", status)


async def watch(clocks, url):
    st = state()
    while True:
        await asyncio.sleep(SYNC_SECONDS)
        try:
            await sync_once(clocks, url, st)
        except shopapp.ERRORS as e:
            log.info("shop app unreachable for pickups: %s", e)
