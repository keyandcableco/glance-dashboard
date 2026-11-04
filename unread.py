"""Unread list: alerts worth keeping (orders, requests, texts) until they're cleared.

The clock's own saved scenes didn't display in testing, so the bridge replays unread items itself:
every REPLAY_MINUTES it shows each one in turn, silently.
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

    def add(self, fields, source):
        item = {"id": uuid.uuid4().hex[:8], "time": time.time(), "source": source,
                **{k: fields[k] for k in ("text", "color", "anim") if k in fields}}
        self.items.append(item)
        self._save()
        return item

    def clear(self, item_id=None):
        before = len(self.items)
        self.items = [i for i in self.items if item_id and i["id"] != item_id]
        self._save()
        return before - len(self.items)


async def replay(clock):
    while True:
        await asyncio.sleep(REPLAY_MINUTES * 60)
        for item in list(clock.unread.items):
            if item not in clock.unread.items:  # cleared mid-pass
                continue
            try:
                await clock.notify(text=item["text"], color=item.get("color", "Lime"),
                                   anim=item.get("anim", "Pulse"), sound="NoneSound", priority="critical")
            except ConnectionError:
                break  # clock gone: try the whole list next time
            except Exception as e:  # one bad item mustn't hold up the rest
                log.warning("replay skipped %r: %s", item.get("text"), e)
                continue
            await asyncio.sleep(GAP_SECONDS)
