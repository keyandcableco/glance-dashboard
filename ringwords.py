"""Words on the rings: a message typed on the panel, drawn in letters round the clock's four rings.

The 4 rings of 48 LEDs make a display 4 lights tall and 48 wide that wraps round the dial. The font is
3×4 (capitals, digits, & ! ? - : *, and <3 for a heart), with a blank column between letters. The top
line is centred on 12 o'clock, the outer ring the top of its letters, reading clockwise; the bottom line
is centred on 6 o'clock and turned over (the inner ring on top, running anticlockwise), so it reads the
right way up. About 6 letters fit on each half; longer lines run down the sides and read sideways. The
lit LEDs are cut into rectangles, one ring segment each: two words are about 160 bytes. All of it was
seen on the shop clock (2026-11-05).

Show now writes it as a saved scene in SLOT, mode 0 at PRIORITY: it takes the whole face at once, the
time and date too, and holds it until it's deleted. A mode-0 arrival takes the face only if its priority
beats what's showing (the 1.6.7 firmware), so the words beat the screens in turn (16), and a high,
highest or critical alert beats the words; a low or medium one waits until they're off. The bridge deletes it when its minutes are up or on
Clear. The message and its end are kept in message-<name>.json, so a restart still takes it off; after
an alert, which takes the face, it's written again.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import statusring
from glance import PRIORITIES, pb

HERE = Path(__file__).parent
SLOT = 1
PRIORITY = 40  # above the screens in turn (16), below a high alert (48)
RINGS, LEDS = 4, 48
LINE_CHARS = 16
MAX_MINUTES = 24 * 60
MAX_BYTES = 500  # one Bluetooth write carries at most 512
CHECK_S = 5
RETRY_S = 30  # after a failed write
AFTER_ALERT_S = 90  # an alert in several pieces can show longer than the screens' 45-s wait
LINES = ((0, False), (LEDS // 2, True))  # (centre LED, turned over): the top line, the bottom line
DELETE = bytes([statusring.SCENES_DELETE, 0, 0, SLOT])

FONT = {
    "A": [".#.", "#.#", "###", "#.#"], "B": ["##.", "###", "#.#", "###"], "C": [".##", "#..", "#..", ".##"],
    "D": ["##.", "#.#", "#.#", "##."], "E": ["###", "##.", "#..", "###"], "F": ["###", "#..", "##.", "#.."],
    "G": [".##", "#..", "#.#", ".##"], "H": ["#.#", "###", "#.#", "#.#"], "I": ["###", ".#.", ".#.", "###"],
    "J": ["..#", "..#", "#.#", ".#."], "K": ["#.#", "##.", "##.", "#.#"], "L": ["#..", "#..", "#..", "###"],
    "M": ["#...#", "##.##", "#.#.#", "#...#"], "N": ["#..#", "##.#", "#.##", "#..#"], "O": [".#.", "#.#", "#.#", ".#."],
    "P": ["##.", "#.#", "##.", "#.."], "Q": [".#.", "#.#", "#.#", ".##"], "R": ["##.", "#.#", "##.", "#.#"],
    "S": [".##", "#..", "..#", "##."], "T": ["###", ".#.", ".#.", ".#."], "U": ["#.#", "#.#", "#.#", "###"],
    "V": ["#.#", "#.#", "#.#", ".#."], "W": ["#...#", "#.#.#", "##.##", "#...#"], "X": ["#.#", ".#.", ".#.", "#.#"],
    "Y": ["#.#", "#.#", ".#.", ".#."], "Z": ["###", "..#", "#..", "###"],
    "0": ["###", "#.#", "#.#", "###"], "1": [".#.", "##.", ".#.", "###"], "2": ["##.", "..#", ".#.", "###"],
    "3": ["###", ".##", "..#", "###"], "4": ["#.#", "#.#", "###", "..#"], "5": ["###", "##.", "..#", "##."],
    "6": ["#..", "###", "#.#", "###"], "7": ["###", "..#", ".#.", ".#."], "8": ["###", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#"],
    " ": ["..", "..", "..", ".."], "&": [".#.", "#.#", ".#.", "#.#"], "!": ["#", "#", ".", "#"],
    "?": ["##.", "..#", ".#.", ".#."], "-": ["..", "##", "..", ".."], ":": [".", "#", ".", "#"],
    "*": ["..", "##", "##", ".."],
    "<3": [".#.#.", "#####", ".###.", "..#.."],
}

log = logging.getLogger("glanced.words")


class NotShown(Exception):
    """The clock didn't take the message (not connected, or the write failed)."""


def tidy(text):
    return " ".join(str(text or "").split()).upper()


def glyphs(text):
    """The font keys for a tidied line, reading "<3" as a heart; ValueError naming what can't be drawn."""
    keys, bad, i = [], [], 0
    while i < len(text):
        key = "<3" if text.startswith("<3", i) else text[i]
        (keys if key in FONT else bad).append(key)
        i += len(key)
    if bad:
        raise ValueError(f"can't draw {' '.join(dict.fromkeys(bad))}: use letters, digits, spaces, & ! ? - : * and <3")
    return keys


def columns(keys):
    """The line as columns of 4 (top row first), a blank column between glyphs."""
    cols = []
    for n, key in enumerate(keys):
        if n:
            cols.append((False,) * RINGS)
        rows = FONT[key]
        cols += [tuple(rows[r][c] == "#" for r in range(RINGS)) for c in range(len(rows[0]))]
    return cols


def leds(width, centre, flipped):
    """The LED each column lands on (0 = 12 o'clock, clockwise); a turned-over line runs anticlockwise."""
    if flipped:
        return [(centre + width // 2 - i) % LEDS for i in range(width)]
    return [(centre - width // 2 + i) % LEDS for i in range(width)]


def colour(name):
    found = statusring.COLORS.get(str(name or "").lower())
    if not found or found == "Black":
        raise ValueError(f"no colour called {name!r}")
    return found


def grid(top, bottom, top_color, bottom_color):
    """grid[ring][led]: a colour name or None. ValueError if a line is too long, can't be drawn, or the
    two would run into each other."""
    g = [[None] * LEDS for _ in range(RINGS)]
    spans = []
    for text, color, (centre, flipped) in ((top, top_color, LINES[0]), (bottom, bottom_color, LINES[1])):
        if len(text) > LINE_CHARS:
            raise ValueError(f"a line takes up to {LINE_CHARS} characters")
        cols = columns(glyphs(text))
        if len(cols) > LEDS - 2:
            raise ValueError("too long to fit round the dial")
        at = leds(len(cols), centre, flipped)
        spans.append(set(at))
        for col, p in zip(cols, at):
            for r, lit in enumerate(col):
                if lit:
                    g[RINGS - 1 - r if flipped else r][p] = colour(color)
    if {(p + d) % LEDS for p in spans[0] for d in (-1, 0, 1)} & spans[1]:  # at least one dark LED between them
        raise ValueError("the top and bottom lines run into each other: shorten one")
    return g


def rectangles(g):
    """Cover the lit LEDs with same-coloured rectangles that don't wrap past 12 o'clock: (start, width,
    colour, ring, extra rings). Greedy: from each uncovered LED, as wide as possible, then as deep."""
    used = [[False] * LEDS for _ in range(RINGS)]
    out = []
    for r in range(RINGS):
        for p in range(LEDS):
            c = g[r][p]
            if c is None or used[r][p]:
                continue
            w = 1
            while p + w < LEDS and g[r][p + w] == c and not used[r][p + w]:
                w += 1
            h = 1
            while r + h < RINGS and all(g[r + h][q] == c and not used[r + h][q] for q in range(p, p + w)):
                h += 1
            for rr in range(r, r + h):
                for q in range(p, p + w):
                    used[rr][q] = True
            out.append((p, w, c, r, h - 1))
    return out


def scene_bytes(g):
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    scene.object.add(method=obj.Fill, lifeTime=statusring.LIFETIME, result=obj.OK).fill.segment.extend(
        statusring.segment(p, w, c, r, x) for p, w, c, r, x in rectangles(g))
    return bytes([statusring.SCENE, PRIORITY, statusring.MODE_WHOLE, SLOT]) + scene.SerializeToString()


def blank_bytes():
    """All four rings dark, written before the delete: a deleted scene's LEDs can stay lit until drawn over."""
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    scene.object.add(method=obj.Fill, lifeTime=50, result=obj.OK).fill.segment.append(statusring.segment(0, LEDS, "Black", 0, 3))
    return bytes([statusring.SCENE, PRIORITY, statusring.MODE_WHOLE, SLOT]) + scene.SerializeToString()


BLANK = blank_bytes()


def message(top, bottom, top_color, bottom_color, minutes):
    """The message as kept, with the bytes that draw it; ValueError if it can't be shown."""
    top, bottom = tidy(top), tidy(bottom)
    if not top and not bottom:
        raise ValueError("type something for the top or bottom line")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float, type(None))) or not 0 <= (minutes or 0) <= MAX_MINUTES:
        raise ValueError(f"minutes must be 0 (until cleared) to {MAX_MINUTES}")
    msg = {"top": top, "bottom": bottom, "top_color": colour(top_color), "bottom_color": colour(bottom_color),
           "until": time.time() + minutes * 60 if minutes else None}
    data = scene_bytes(grid(top, bottom, msg["top_color"], msg["bottom_color"]))
    if len(data) > MAX_BYTES:
        raise ValueError("too much to draw at once: shorten it")
    return msg, data


class Message:
    """The message on one clock. showing: what it should show ({"top", "bottom", "top_color",
    "bottom_color", "until"}, until None = until cleared), or None. on_clock: it may still be on the clock
    (written, and not deleted since), so the watcher takes it off once nothing should show."""

    def __init__(self, name):
        self.path = HERE / f"message-{name}.json"
        self.showing, self.on_clock = None, False
        self.lock = asyncio.Lock()  # one write or delete at a time, so the file and the clock agree
        self.client, self.written, self.stale = None, 0.0, False  # the link and time of the last write
        self.retry_at = 0.0  # after a failed write, the watcher waits RETRY_S
        try:
            saved = json.loads(self.path.read_text())
            self.on_clock = bool(saved.get("on_clock"))
            s = saved.get("showing")
            if s:  # still drawable; the watcher takes it off if its minutes are up
                top, bottom, tc, bc = tidy(s["top"]), tidy(s["bottom"]), colour(s["top_color"]), colour(s["bottom_color"])
                grid(top, bottom, tc, bc)
                until = None if s.get("until") is None else float(s["until"])
                self.showing = {"top": top, "bottom": bottom, "top_color": tc, "bottom_color": bc, "until": until}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError, KeyError) as e:
            log.warning("%s couldn't be read (%s); taking any message off the clock", self.path.name, type(e).__name__)
            self.showing, self.on_clock = None, True

    def json(self):
        return {"showing": self.showing, "on_clock": self.on_clock}

    def _save(self, showing, on_clock):
        """Write first, then change memory: OSError leaves both as they were."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"showing": showing, "on_clock": on_clock}, indent=1))
        tmp.replace(self.path)
        self.showing, self.on_clock = showing, on_clock

    def _keep(self, showing, on_clock):
        """Memory first, so a full disk can't keep words on the clock; the file follows if it can."""
        self.showing, self.on_clock = showing, on_clock
        try:
            self._save(showing, on_clock)
        except OSError as e:
            log.warning("couldn't save %s: %s", self.path.name, e.strerror or type(e).__name__)

    async def show(self, clock, top="", bottom="", top_color="Aqua", bottom_color="Aqua", minutes=5):
        """Put it on the clock now. ValueError if it can't be drawn, NotShown if the clock didn't take it
        (what showed before stays), OSError if it can't be saved (then nothing is sent)."""
        msg, data = message(top, bottom, top_color, bottom_color, minutes)
        async with self.lock:
            if not clock.connected:
                raise NotShown("the clock isn't connected")
            before = self.showing
            self._save(msg, True)  # on_clock before the write: one that fails may still have landed
            try:
                await clock.write(data)
            except Exception as e:
                # the clock most likely kept what it had: keep that, and have the watcher write it (or take it off) again
                self._keep(before, True)
                self.stale, self.retry_at = True, time.time() + CHECK_S
                raise NotShown(f"the clock didn't take it ({e}); nothing changed") from e
            self.client, self.written, self.stale = clock.client, time.time(), False
        log.info("%s: words on the rings: %r / %r until %s", clock.name, msg["top"], msg["bottom"],
                 time.strftime("%H:%M", time.localtime(msg["until"])) if msg["until"] else "cleared")

    async def clear(self, clock):
        """Take it off: now if the clock takes it, else as soon as the watcher can."""
        async with self.lock:
            self._keep(None, self.on_clock)
            await self._delete(clock)
        log.info("%s: words on the rings cleared", clock.name)

    async def _delete(self, clock, sweep=False):
        """Dark rings, then the slot deleted. With sweep and nothing of ours there, only the delete:
        anything another program (a bench script) left in the slot."""
        if not clock.connected or not (self.on_clock or sweep):
            return
        try:
            if self.on_clock:
                await clock.write(BLANK)
            await clock.write(DELETE)
            self._keep(None, False)
            self.client = clock.client
        except Exception as e:
            self.retry_at = time.time() + RETRY_S
            log.warning("%s: taking the words off failed: %s", clock.name, e)


async def watch(clock):
    """Take the message off when its minutes are up, and write it again on a new link, after an alert
    (which takes the face), and every statusring.REFRESH_S while it shows. With nothing showing, a new
    link also deletes whatever is left in SLOT: a mode-0 leftover would hide the time."""
    m = clock.message
    while True:
        await asyncio.sleep(CHECK_S)
        if not clock.connected:
            continue
        now = time.time()
        async with m.lock:
            if m.showing and m.showing["until"] and now >= m.showing["until"]:
                m._keep(None, m.on_clock)
                log.info("%s: words on the rings ended", clock.name)
            if now < m.retry_at:
                continue
            new_link = m.client is not clock.client
            if not m.showing:
                if m.on_clock or new_link:
                    await m._delete(clock, sweep=new_link)
                continue
            if statusring.near_alarm(clock, now) or now - clock.last_alert < AFTER_ALERT_S:
                m.stale = True
                continue
            if not new_link and not m.stale and now - m.written < statusring.REFRESH_S:
                continue
            try:
                await clock.write(scene_bytes(grid(m.showing["top"], m.showing["bottom"], m.showing["top_color"],
                                                   m.showing["bottom_color"])))
                m.client, m.written, m.stale = clock.client, now, False
            except Exception as e:
                m.retry_at = now + RETRY_S
                log.warning("%s: writing the words again failed: %s", clock.name, e)
