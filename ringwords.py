"""Words on the rings: two lines drawn in letters round the clock's four rings, one more screen in the
turn. They say what was typed on the panel while it lasts, else the next pickup, drop-off or hard stop due within
the hour, else what the clock's words source says.

The 4 rings of 48 LEDs make a display 4 lights tall and 48 wide that wraps round the dial. The font is
3×4 (capitals, digits, & ! ? - : *, and <3 for a heart), with a blank column between letters. The top
line is centred on 12 o'clock, the outer ring the top of its letters, reading clockwise; the bottom line
is centred on 6 o'clock and turned over (the inner ring on top, running anticlockwise), so it reads the
right way up. About 6 letters fit on each half; longer lines run down the sides and read sideways. The
lit LEDs are cut into rectangles, one ring segment each: two words are about 160 bytes. All of it was
seen on the shop clock (2026-11-05).

It's a saved scene in watch-face mode in SLOT, kept drawn by statusring.watch like the other screens
(never during an alert or a countdown's alarm), and switched on and off on the panel as the "words"
screen; with nothing to say it leaves the turn (its slot deleted). Typed words come first, then the
next pickup or hard stop when one is due within the hour (JUNO / 3:30), then the words source's. Typed
words and their end are kept in message-<name>.json. Until 2026-11-06 typed words took the whole face at
once (mode 0, priority 40); now they take their turn like the other screens, and a source keeps the
screen saying something useful.

The words source, set per clock in config.toml ([clocks.<name>.words] command = "..."), is a command run
every every_minutes: the first line it prints is the top line and the second the bottom, or one line
runs on from the top to the bottom. Accents come off, apostrophes and the . or , inside a number are
dropped, other characters the font lacks become spaces, and each line keeps the words that fit upright on
its half (a word too long for a half is cut). Printing nothing says nothing. A command that fails is tried
again within a minute and keeps its last words for STALE_S (two runs, if that's longer), then they go.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import time
import unicodedata
from pathlib import Path

import pickups
import statusring
from glance import FOLD, pb

HERE = Path(__file__).parent
SLOT = 1
RINGS, LEDS = 4, 48
LINE_CHARS = 16
HALF = LEDS // 2 - 1  # columns a line has upright on its half, with a dark LED between it and the other
MAX_MINUTES = 24 * 60
MAX_BYTES = 500  # one Bluetooth write carries at most 512
LINES = ((0, False), (LEDS // 2, True))  # (centre LED, turned over): the top line, the bottom line
COLORS = ("Aqua", "Darkorange")  # the words source's top and bottom line, unless its section names others
EVERY_MINUTES = 5
RUN_S = 30  # a words command that takes longer is stopped
STALE_S = 60 * 60
RETRY_S = 60  # after a failed run, say before the network is up
SOON_KINDS = ("pickup", "dropoff", "stop")
SOON_S, LATE_S = 60 * 60, 15 * 60  # these take the words from an hour before they're due to 15 minutes after

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




def draw(g):
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    scene.object.add(method=obj.Fill, lifeTime=statusring.LIFETIME, result=obj.OK).fill.segment.extend(
        statusring.segment(p, w, c, r, x) for p, w, c, r, x in rectangles(g))
    return bytes([statusring.SCENE, statusring.PRIORITY, statusring.MODE_WATCHFACE, SLOT]) + scene.SerializeToString()


def scene_bytes(want):
    """want: (top, bottom, top colour, bottom colour), as picture() gives it."""
    return draw(grid(*want))


def message(top, bottom, top_color, bottom_color, minutes):
    """Typed words as kept; ValueError if they can't be shown."""
    top, bottom = tidy(top), tidy(bottom)
    if not top and not bottom:
        raise ValueError("type something for the top or bottom line")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float, type(None))) or not 0 <= (minutes or 0) <= MAX_MINUTES:
        raise ValueError(f"minutes must be 0 (until cleared) to {MAX_MINUTES}")
    msg = {"top": top, "bottom": bottom, "top_color": colour(top_color), "bottom_color": colour(bottom_color),
           "until": time.time() + minutes * 60 if minutes else None}
    if len(draw(grid(top, bottom, msg["top_color"], msg["bottom_color"]))) > MAX_BYTES:
        raise ValueError("too much to draw at once: shorten it")
    return msg


def plain(text):
    """Text in the font's characters: glance's folding (curly quotes, dashes), accents off, apostrophes
    and the . or , inside a number dropped (XPANDER'S is XPANDERS, 1,000 is 1000), anything else it can't
    draw a space (a letter from another script too: no "?" for it)."""
    text = "".join(FOLD.get(c, c) for c in unicodedata.normalize("NFC", str(text or "")))
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper()
    text = re.sub(r"(?<=\d)[.,](?=\d)", "", text.replace("'", ""))
    out, i = [], 0
    while i < len(text):
        key = "<3" if text.startswith("<3", i) else text[i]
        out.append(key if key in FONT else " ")
        i += len(key)
    return tidy("".join(out))


def width(text):
    return len(columns(glyphs(text)))


def fill(words, room=HALF):
    """(as many of the words as fit in room columns, the words left): a first word too wide alone is cut."""
    line = ""
    for n, word in enumerate(words):
        if width(f"{line} {word}".strip()) <= room:
            line = f"{line} {word}".strip()
            continue
        if not line:
            while word and width(word) > room:
                word = word[:-1].removesuffix("<")  # never half a heart
            return word, words[n + 1:]
        return line, words[n:]
    return line, []


def fit(lines):
    """(top, bottom) for a words source's output lines: the first on top and the second below, or a
    single line running on from the top to the bottom; each upright on its half and one write long."""
    lines = [plain(line) for line in list(lines)[:2]]
    if len(lines) < 2 or not lines[1]:
        top, rest = fill(lines[0].split() if lines else [])
        bottom = fill(rest)[0]
    else:
        top, bottom = fill(lines[0].split())[0], fill(lines[1].split())[0]
    while len(draw(grid(top, bottom, *COLORS))) > MAX_BYTES:  # only dense words; drop the longer line's last
        if width(top) >= width(bottom):
            top = top.rpartition(" ")[0]
        else:
            bottom = bottom.rpartition(" ")[0]
    return top, bottom


class Message:
    """The words on one clock. typed: what was typed on the panel ({"top", "bottom", "top_color",
    "bottom_color", "until"}, until None = until cleared), kept in message-<name>.json; said: what the
    words source last said ({"top", "bottom", "at"}); either can be None. Typed words go first."""

    def __init__(self, name):
        self.name, self.path = name, HERE / f"message-{name}.json"
        self.typed, self.said, self.source, self.error, self.colors = None, None, False, None, COLORS
        self.stale_s = STALE_S
        try:
            s = json.loads(self.path.read_text()).get("showing")
            if s:  # still drawable; words() drops it if its minutes are up
                top, bottom, tc, bc = tidy(s["top"]), tidy(s["bottom"]), colour(s["top_color"]), colour(s["bottom_color"])
                grid(top, bottom, tc, bc)
                until = None if s.get("until") is None else float(s["until"])
                self.typed = {"top": top, "bottom": bottom, "top_color": tc, "bottom_color": bc, "until": until}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError, KeyError) as e:
            log.warning("%s couldn't be read (%s); no typed words", self.path.name, type(e).__name__)

    def _save(self, typed):
        """Memory first, so a full disk can't keep words up; the file follows if it can."""
        self.typed = typed
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"showing": typed}, indent=1))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("couldn't save %s: %s", self.path.name, e.strerror or type(e).__name__)

    def show(self, top="", bottom="", top_color="Aqua", bottom_color="Darkorange", minutes=5):
        """Typed words, shown when the screen next comes round; ValueError if they can't be drawn."""
        self._save(message(top, bottom, top_color, bottom_color, minutes))
        t = self.typed
        log.info("%s: words on the rings: %r / %r until %s", self.name, t["top"], t["bottom"],
                 time.strftime("%H:%M", time.localtime(t["until"])) if t["until"] else "cleared")

    def clear(self):
        """Take the typed words off: the words source's go back up, if there are any."""
        self._save(None)
        log.info("%s: typed words on the rings cleared", self.name)

    def words(self, now, soon=None):
        """(top, bottom, top colour, bottom colour) to show now, or (): typed words, else soon (the next
        pickup or hard stop, from soon()), else the words source's."""
        t = self.typed
        if t and t["until"] and now >= t["until"]:
            self._save(None)
            log.info("%s: typed words on the rings ended", self.name)
        elif t:
            return t["top"], t["bottom"], t["top_color"], t["bottom_color"]
        if soon:
            return (*soon, *self.colors)
        s = self.said
        return (s["top"], s["bottom"], *self.colors) if s and now - s["at"] < self.stale_s else ()

    def json(self, soon=None):
        now = time.time()
        self.words(now)  # an ended one goes
        colors = {"top_color": self.colors[0], "bottom_color": self.colors[1]}
        fresh = self.said and now - self.said["at"] < self.stale_s
        return {"typed": self.typed, "soon": {"top": soon[0], "bottom": soon[1], **colors} if soon else None,
                "said": {**self.said, **colors} if fresh else None, "source": self.source, "error": self.error}


def soon(clock, now):
    """(name, time) for the next pickup or hard stop on the list due within SOON_S, or up to LATE_S
    late (one still to come first), or None: JUNO / 3:30, or PICKUP / 3:30 without a name. A name cut to
    fit loses a dangling - & or : ("Sam - Juno" is SAM)."""
    near = [i for i in clock.unread.items if i.get("kind") in SOON_KINDS and isinstance(i.get("due"), (int, float))
            and -LATE_S <= i["due"] - now <= SOON_S]
    if not near:
        return None
    item = min(near, key=lambda i: (i["due"] < now, i["due"]))
    words = fill(plain(item.get("name")).split())[0].split()
    while words and not words[-1].strip("-&:"):
        words.pop()
    return " ".join(words) or pickups.KINDS[item["kind"]]["label"], pickups.clock_time(item["due"])


def picture(clock, now):
    return clock.message.words(now, soon(clock, now))


def watch(clock):
    return statusring.watch(clock, picture, scene_bytes, SLOT, "words", "words")


async def run(argv):
    """The command's output; RuntimeError if it fails or takes over RUN_S. It runs in a process group of
    its own, stopped as a whole: a child left running would hold the output open."""
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), RUN_S)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise RuntimeError(f"took over {RUN_S} s") from None
    if proc.returncode:
        last = (err.decode(errors="replace").strip().splitlines() or [""])[-1][:160]
        raise RuntimeError(f"exited {proc.returncode}{': ' + last if last else ''}")
    return out.decode(errors="replace")


async def follow(clock, cfg):
    """Run the words source's command every every_minutes and keep what it says. A bad section is
    logged and leaves the source off."""
    m = clock.message
    try:
        argv = shlex.split(cfg["command"])
        argv[0] = os.path.expanduser(argv[0])
        every = max(1.0, float(cfg.get("every_minutes", EVERY_MINUTES))) * 60
        m.colors = colour(cfg.get("top_color", COLORS[0])), colour(cfg.get("bottom_color", COLORS[1]))
        m.stale_s = max(STALE_S, 2 * every)  # a slow source's words last until its next run
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        log.warning("%s: the [words] section needs command = \"...\" (%s: %s); no words source", clock.name, type(e).__name__, e)
        return
    m.source = True
    while True:
        try:
            top, bottom = fit((await run(argv)).splitlines())
            m.said = {"top": top, "bottom": bottom, "at": time.time()} if top or bottom else None
            if m.error:
                log.info("%s: words source works again", clock.name)
            m.error = None
        except Exception as e:  # OSError (no such command), RuntimeError (it failed), UnicodeError...
            why = str(e) or type(e).__name__
            if why != m.error:
                log.warning("%s: words source: %s", clock.name, why)
            m.error = why
        await asyncio.sleep(min(every, RETRY_S) if m.error else every)
