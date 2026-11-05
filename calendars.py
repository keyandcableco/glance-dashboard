"""A clock's calendars: Google Calendar (or any iCal) feeds whose timed events calwatch merges.

A calendar in config.toml [clocks.<name>.calendar] (url_file, optional name) comes first, unless it was
removed on the panel. Calendars added on the panel are kept with their addresses in calendars-<name>.json.
A calendar's secret iCal address is a password in all but name: that file is git-ignored and mode 0600,
and the address never leaves this machine. The API and the log show a calendar's name, and at most the
address's host.
"""
import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).parent
MAX = 12  # calendars per clock
NAME_LEN = 40

log = logging.getLogger("glanced.calendar")


def host(url):
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def clean_url(url):
    """The address as it will be fetched (webcal:// becomes https://); ValueError, never quoting it, if it isn't one."""
    url = str(url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url:
        raise ValueError("paste the calendar's secret address in iCal format")
    if not url.lower().startswith(("https://", "http://")) or any(c.isspace() for c in url) or not host(url):
        raise ValueError("that isn't a web address; paste the calendar's secret address in iCal format")
    return url


class Calendars:
    def __init__(self, name, cfg):
        self.path = HERE / f"calendars-{name}.json"
        conf = cfg.get("calendar")
        self.config = {"id": "config", "name": " ".join(str(conf.get("name") or "Calendar").split())[:NAME_LEN],
                       "url_file": conf["url_file"]} if conf else None
        self.added, self.config_removed = [], ""  # config_removed: the url_file of the config calendar removed on the panel
        self.status = {}  # id: {"events", "error", "fetched"}, kept up by calwatch
        self.changed = asyncio.Event()  # set on every add and remove, so calwatch reads them at once
        try:
            saved = json.loads(self.path.read_text())
            self.added = [{k: str(c[k]) for k in ("id", "name", "url")} for c in saved.get("added", [])
                          if isinstance(c, dict) and {"id", "name", "url"} <= c.keys()]
            self.config_removed = str(saved.get("config_removed") or "")
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError, TypeError) as e:
            # don't let the next save overwrite addresses that can't be read now
            broken = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
            try:
                self.path.replace(broken)
                log.warning("%s couldn't be read (%s); moved to %s", self.path.name, type(e).__name__, broken.name)
            except OSError:
                log.warning("%s couldn't be read (%s)", self.path.name, type(e).__name__)
            self.added, self.config_removed = [], ""
            return
        try:
            os.chmod(self.path, 0o600)  # in case it was copied in with looser permissions
        except OSError as e:
            log.warning("couldn't make %s private: %s", self.path.name, e.strerror)

    def entries(self):
        """[{"id", "name", and "url" or "url_file"}]; the config's first."""
        return ([self.config] if self.config and not self.config_hidden() else []) + self.added

    def config_hidden(self):
        """Removed on the panel; config.toml pointing at another url_file brings it back."""
        return bool(self.config) and self.config_removed == str(self.config["url_file"])

    @staticmethod
    def url(entry):
        """The address to fetch; OSError or ValueError if a config calendar's url_file can't be read."""
        return entry["url"] if "url" in entry else entry["url_file"].read_text().strip()

    def check(self, name, url):
        """(name, address) cleaned up, or ValueError: no name, not an address, a name or address already
        on this clock, or MAX calendars already."""
        name = " ".join(str(name or "").split())[:NAME_LEN]
        if not name:
            raise ValueError("give the calendar a name")
        url = clean_url(url)
        entries = self.entries()
        if len(entries) >= MAX:
            raise ValueError(f"a clock takes up to {MAX} calendars")
        for e in entries:
            if e["name"].casefold() == name.casefold():
                raise ValueError(f"there's already a calendar called {e['name']}")
            try:
                same = self.url(e) == url
            except (OSError, ValueError):
                same = False
            if same:
                raise ValueError(f"that calendar is already on this clock, as {e['name']}")
        return name, url

    def add(self, name, url):
        name, url = self.check(name, url)
        entry = {"id": secrets.token_hex(4), "name": name, "url": url}
        self._save(self.added + [entry], self.config_removed)
        return entry

    def remove(self, cid):
        """KeyError if there's no such calendar on this clock."""
        if cid == "config" and self.config and not self.config_hidden():
            self._save(self.added, str(self.config["url_file"]))
        elif any(c["id"] == cid for c in self.added):
            self._save([c for c in self.added if c["id"] != cid], self.config_removed)
        else:
            raise KeyError(cid)
        self.status.pop(cid, None)

    def name(self, cid):
        return next((e["name"] for e in self.entries() if e["id"] == cid), cid)

    def _save(self, added, config_removed):
        """Write first, then change memory: OSError leaves both as they were."""
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)  # an old .tmp keeps its mode through O_CREAT
            json.dump({"added": added, "config_removed": config_removed}, f, indent=1)
        tmp.replace(self.path)
        self.added, self.config_removed = added, config_removed
        self.changed.set()

    def json(self):
        """What the panel sees: names, hosts and how the last read went; never the addresses."""
        out = []
        for e in self.entries():
            try:
                where = host(self.url(e))
            except (OSError, ValueError):
                where = ""
            st = self.status.get(e["id"], {})
            out.append({"id": e["id"], "name": e["name"], "host": where, "config": "url_file" in e,
                        "events": st.get("events"), "error": st.get("error"), "fetched": st.get("fetched")})
        return out
