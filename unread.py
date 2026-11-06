"""Unread list: alerts worth keeping (orders, requests, texts) until they're cleared.

The bridge replays unread items itself as alerts: every REPLAY_MINUTES it shows each one in turn,
silently. (The status ring shows them at a glance in between.)
"""
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent
REPLAY_MINUTES = float(os.environ.get("GLANCE_REPLAY_MINUTES", "5"))
GAP_SECONDS = 15  # between items in one replay pass

log = logging.getLogger("glanced.unread")


class Unread:
    def __init__(self, name):
        self.path = HERE / f"unread-{name}.json"
        try:
            self.items = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=1))
        tmp.replace(self.path)

    def add(self, fields, source, merge=False):
        """A new item; with merge, the same text from the same source again counts up the one already
        there and moves it to the end, so twenty texts from one person are one item, not twenty."""
        kept = {k: fields[k] for k in ("text", "color", "anim") if k in fields}
        item = next((i for i in self.items if i.get("source") == source and i.get("text") == kept.get("text")), None) \
            if merge else None
        if item:
            self.items.remove(item)
            item.update(kept, time=time.time(), count=item.get("count", 1) + 1)
        else:
            item = {"id": uuid.uuid4().hex[:8], "time": time.time(), "source": source, **kept}
        self.items.append(item)
        self._save()
        return item

    def clear(self, item_id=None):
        """Clear one item, or everything but the daily check-ins, which wait to be confirmed (checkins.py)."""
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] != item_id] if item_id else \
            [i for i in self.items if i.get("kind") == "checkin"]
        self._save()
        return before - len(self.items)


def shown(item):
    """The item's text, with how many times it came when that's more than once: "{heart} SAM (3)"."""
    count = item.get("count", 1)
    return f"{item['text']} ({count})" if count > 1 else item["text"]


async def replay(clock):
    while True:
        await asyncio.sleep(REPLAY_MINUTES * 60)
        for item in list(clock.unread.items):
            if item not in clock.unread.items:  # cleared mid-pass
                continue
            try:
                await clock.notify(text=shown(item), color=item.get("color", "Lime"),
                                   anim=item.get("anim", "Pulse"), sound="NoneSound", priority="critical")
            except ConnectionError:
                break  # clock gone: try the whole list next time
            except Exception as e:  # one bad item mustn't hold up the rest
                log.warning("replay skipped %r: %s", item.get("text"), e)
                continue
            await asyncio.sleep(GAP_SECONDS)
