"""Notes to self: things said in passing ("note: order more 2.2k resistors"), kept for later.

One list for all clocks, in notes.json; the panel shows it as a checklist. A note never interrupts:
the clock just flashes NOTED.
"""
import json
import time
import uuid
from pathlib import Path

PATH = Path(__file__).parent / "notes.json"


class Notes:
    def __init__(self, path=PATH):
        self.path = path
        try:
            self.items = json.loads(path.read_text())
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=1))
        tmp.replace(self.path)

    def add(self, text):
        item = {"id": uuid.uuid4().hex[:8], "time": time.time(), "text": text.strip(), "done": False}
        self.items.append(item)
        self._save()
        return item

    def update(self, item_id, done=None, delete=False):
        for item in self.items:
            if item["id"] == item_id:
                if delete:
                    self.items.remove(item)
                elif done is not None:
                    item["done"] = bool(done)
                self._save()
                return True
        return False
