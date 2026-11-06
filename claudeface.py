"""Claude's face: one more screen in the turn that shows how the Claude Code sessions on this computer are
doing, as an emoji face drawn on the rings, with a steady pip on the outer ring at 12 o'clock for each
session that needs you and you haven't looked at, in its state's colour.

  a turn died on an error        a dizzy face in red, x eyes and an open mouth        a red pip each
  a session waits on you         wide eyes and an O mouth; it blinks                  an orange pip each
  one finished since you looked  a smile; it winks                                    a green pip each
  sessions working               a flat mouth, eyes looking right, left, up; a blink  no pip
  only ones you've seen waiting  a flat mouth, a slow blink                           no pip
  none of these                  the screen leaves the turn (its slot deleted)

The first of these there is picks the face. Waiting sessions you've already looked at come last: a
tracker may keep them for days, and ahead of the others they'd hide the smile and the looking around.
Pips only for what needs you: one for every session, seen and working ones too, was too many to read.

The states come from a session tracker, a Python file run inside the bridge, set per clock in
config.toml ([clocks.<name>.claude_face] tracker = ..., a relative path from this folder). It defines a
SessionRing class whose refresh(now), now in epoch seconds, sets .counts to how many sessions are in each
of the states (error, waiting, waitingSeen, done, running), say from files Claude Code hooks write per
session. A clock without that section has no Claude face screen. A tracker that can't be loaded or read
leaves the face off and is logged once.

It's a saved scene in watch-face mode, SLOT, kept drawn by statusring.watch like the other screens (never
during an alert or a countdown's alarm), and switched on and off on the panel as the "claude" screen. The
moves are timed objects, which play once each time the screen comes round. Every face was tried on the
clock and drew as drawn, and the blink and wink kept their times.
"""
import importlib.util
import logging
import math
import os
import time
from pathlib import Path

import ringwords
import statusring
from glance import pb

log = logging.getLogger("glanced")

SLOT = 8
SKIN = "Darkorange"
PIP_COLORS = {"error": "Red", "waiting": "Darkorange", "done": "LimeGreen"}  # the states that get a pip
ORDER = ("error", "waiting", "done", "running", "waitingSeen")  # the first one there picks the face; the pips from the left
HERE = Path(__file__).parent
MAX_PIPS = 9
REFRESH_S = 10  # how often the tracker's files are looked at again
SECOND = 50  # frames


def cells(rings, leds):
    return [(r, p) for r in rings for p in leds]


def mirror(left):
    """The left half and its mirror image: LED i and 47 - i face each other across the 12-6 line."""
    return left + [(r, 47 - p) for r, p in left]


def centre(r, p):
    """A cell's centre (x right, y up) in the panel's units: rings of 48 cells from r 170 in to 97.6."""
    radius, a = 170 - (r + 0.5) * 18.1, math.radians((p + 0.5) * 7.5)
    return radius * math.sin(a), radius * math.cos(a)


LEFT_EYE = cells([1, 2, 3], [40, 41, 42])  # 3 by 3 at 10:30, the right one at 1:30
RIGHT_EYE = [(r, 47 - p) for r, p in LEFT_EYE]
EYES = LEFT_EYE + RIGHT_EYE
SHUT = mirror(cells([2], [39, 40, 41, 42, 43]))  # an arc along a ring bows outward, like a closed eye
LEFT_SHUT = SHUT[:len(SHUT) // 2]
SMILE = cells([1], range(18, 30)) + mirror([(2, 16), (2, 17)])  # along a ring the ends turn up
O = [(0, 23), (0, 24), (1, 22), (1, 25), (2, 22), (2, 25), (3, 23), (3, 24)]  # hollow, four rings tall
FLAT = cells([3], range(21, 27))
CROSSES = mirror([(1, 41), (2, 40), (2, 41), (2, 42), (3, 41)])  # a + on the rings shows as an x at 10:30


def pupil(eye, dx, dy):
    """The cell of a 3 by 3 eye nearest a point dx, dy (panel units) from its middle: where it looks."""
    mx = sum(centre(*c)[0] for c in eye) / len(eye)
    my = sum(centre(*c)[1] for c in eye) / len(eye)
    return min(eye, key=lambda c: math.dist(centre(*c), (mx + dx, my + dy)))


def looking(dx, dy):
    """Both eyes, each with a dark pupil looking the same way."""
    return [(EYES, SKIN), ([pupil(LEFT_EYE, dx, dy), pupil(RIGHT_EYE, dx, dy)], "Black")]


FACES = {  # the face drawn first, then (seconds, what changes) played once per turn
    "error": ([(CROSSES, "Red"), (O, "Red")], []),
    "waiting": ([(EYES, SKIN), (O, SKIN)],
                [(3, [(EYES, "Black"), (SHUT, SKIN)]), (3.2, [(SHUT, "Black"), (EYES, SKIN)]),
                 (9, [(EYES, "Black"), (SHUT, SKIN)]), (9.2, [(SHUT, "Black"), (EYES, SKIN)])]),
    "waitingSeen": ([(EYES, SKIN), (FLAT, SKIN)],
                    [(6, [(EYES, "Black"), (SHUT, SKIN)]), (6.3, [(SHUT, "Black"), (EYES, SKIN)])]),
    "done": ([(EYES, SKIN), (SMILE, SKIN)],
             [(3, [(LEFT_EYE, "Black"), (LEFT_SHUT, SKIN)]), (3.6, [(LEFT_SHUT, "Black"), (LEFT_EYE, SKIN)])]),
    "running": ([(FLAT, SKIN)] + looking(0, 0),
                [(2.5, looking(18, 0)), (4.5, looking(-18, 0)), (6.5, looking(0, 18)), (8.5, looking(0, 0)),
                 (10, [(EYES, "Black"), (SHUT, SKIN)]), (10.2, [(SHUT, "Black")] + looking(0, 0))]),
}


def segments(layers):
    """Fill segments for (cells, colour) layers, later ones over earlier ones."""
    g = [[None] * 48 for _ in range(4)]
    for where, color in layers:
        for r, p in where:
            g[r][p] = color
    return [statusring.segment(p, w, c, r, x) for p, w, c, r, x in ringwords.rectangles(g)]


def pips(states):
    """[(LED, state)] on the outer ring, centred on LED 0, two LEDs apart, worst first from the left."""
    n = len(states)
    return [((2 * i - n + 1) % 48, s) for i, s in enumerate(states)]


def scene_bytes(want):
    state, states = want
    face, moves = FACES[state]
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    scene.object.add(method=obj.Fill, lifeTime=statusring.LIFETIME, result=obj.OK).fill.segment.extend(
        segments(face + [([(0, p)], PIP_COLORS[s]) for p, s in pips(states)]))
    for seconds, layers in moves:
        scene.object.add(method=obj.Fill, startTime=round(seconds * SECOND), lifeTime=statusring.LIFETIME,
                         result=obj.OK).fill.segment.extend(segments(layers))
    return bytes([statusring.SCENE, statusring.PRIORITY, statusring.MODE_WATCHFACE, SLOT]) + scene.SerializeToString()


class Sessions:
    """The tracker's counts, looked at no more than every REFRESH_S. A tracker that can't be loaded or
    read leaves the face off (logged once) rather than stopping the screens."""

    def __init__(self, path):
        self.path, self.ring, self.counts, self.looked, self.failed = HERE / os.path.expanduser(path), None, {}, 0.0, False

    def states(self, now):
        if now - self.looked >= REFRESH_S:
            self.looked = now
            try:
                if self.ring is None:
                    spec = importlib.util.spec_from_file_location("claude_sessionring", self.path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self.ring = module.SessionRing()
                self.ring.refresh(now)
                self.counts, self.failed = {s: int(n) for s, n in self.ring.counts.items()}, False
            except Exception as e:
                if not self.failed:
                    log.warning("claude face: session tracker %s: %s: %s", self.path, type(e).__name__, e)
                self.counts, self.failed = {}, True
        return [s for s in ORDER for _ in range(self.counts.get(s, 0))]


TRACKERS = {}  # one per tracker file, shared by the clocks that use it


def tracker(clock_cfg):
    """The tracker file named in a clock's [claude_face] section, or None without a usable one."""
    section = clock_cfg.get("claude_face")
    path = section.get("tracker") if isinstance(section, dict) else None
    return path if isinstance(path, str) and path.strip() else None


def picture(clock, now):
    path = getattr(clock, "claude_tracker", None)
    if not path:
        return ()
    states = TRACKERS.setdefault(path, Sessions(path)).states(now)
    return (states[0], tuple(s for s in states if s in PIP_COLORS)[:MAX_PIPS]) if states else ()


def watch(clock):
    return statusring.watch(clock, picture, scene_bytes, SLOT, "claude face", "claude")


if __name__ == "__main__":  # print each face's size, and the states the tracker sees now
    import sys
    for state in ORDER:
        for n in (0, MAX_PIPS):
            data = scene_bytes((state, (state if state in PIP_COLORS else "waiting",) * n))
            print(f"{state:11} {n} pips {len(data):3} B")
    if len(sys.argv) > 1:
        states = Sessions(sys.argv[1]).states(time.time())
        print(states, "pips:", [s for s in states if s in PIP_COLORS][:MAX_PIPS])
