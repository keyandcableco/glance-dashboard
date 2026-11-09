"""Claude's face: one more screen in the turn that shows how the Claude Code sessions on this computer are
doing, as an emoji face drawn on the rings, with eyebrows of steady pips on the outer ring, one for each
session that needs you and you haven't looked at, in its state's colour: errors and waiting ones over the
left eye, finished ones over the right.

  a turn died on an error        red: dizzy, frowning, crying or sobbing               a red pip each
  a session waits on you         wide eyes and an O, a frown, a tear, or eyes          an orange pip each
                                 looking round for you; they blink
  one finished since you looked  happy: a winking smile, a grin, laughing, heart or    a green pip each
                                 star eyes, tongue out, sunglasses, blushing
  sessions working               busy: eyes looking around over a flat mouth or a      no pip
                                 smile, sunglasses on, tongue out in concentration
  only ones you've seen waiting  patient: a slow blink, a side-eye, dozing             no pip
  none of these                  the screen leaves the turn (its slot deleted)

The first of these there is picks the mood, and within it a face is picked at random every VARY_S, never
the one shown last, so the screen looks different most times it comes round: one face a mood got boring.
Waiting sessions you've already looked at come last: a tracker may keep them for days, and ahead of the
others they'd hide the happy faces and the looking around.
Pips only for what needs you: one for every session, seen and working ones too, was too many to read.
In one row across 12 o'clock they were nearly eyebrows already; split over the eyes they read as them.
A brow holds MAX_BROW; one with none leaves the other raised. The heart and star eyes reach the outer
ring under the brows; the pips draw over them.

The states come from a session tracker, a Python file run inside the bridge, set per clock in
config.toml ([clocks.<name>.claude_face] tracker = ..., a relative path from this folder). It defines a
SessionRing class whose refresh(now), now in epoch seconds, sets .counts to how many sessions are in each
of the states (error, waiting, waitingSeen, done, running), say from files Claude Code hooks write per
session. A clock without that section has no Claude face screen. A tracker that can't be loaded or read
leaves the face off and is logged once.

It's a saved scene in watch-face mode, SLOT, kept drawn by statusring.watch like the other screens (never
during an alert or a countdown's alarm), and switched on and off on the panel as the "claude" screen. The
moves are timed objects, which play once each time the screen comes round. The drawings are the set tried
on the clock, where each drew as drawn and the blinks and winks kept their times; a few faces here put two
of them together (pupils with an O or a smile, sunglasses with a flat mouth).
"""
import importlib.util
import logging
import math
import os
import random
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
BROWS = (("error", "waiting"), ("done",))  # the pips over the left eye, then over the right
BROW = 41  # the outer ring's LED over the left eye's middle; the right brow is its mirror image
MAX_BROW = 4  # pips a brow
HERE = Path(__file__).parent
REFRESH_S = 10  # how often the tracker's files are looked at again
VARY_S = 60  # a new face from the mood's set this often: about once a turn of the screens
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
HAPPY = mirror(cells([1], [39, 40, 41, 42, 43]))  # the same arc on the eye's outer ring: ^ ^
CROSSES = mirror([(1, 41), (2, 40), (2, 41), (2, 42), (3, 41)])  # a + on the rings shows as an x at 10:30
# the rings turn a grid 45 degrees at 10:30, so shapes that must stay upright are picked cell by cell
HEARTS = mirror([(0, 41), (1, 40), (1, 41), (1, 42), (2, 40), (2, 41), (2, 42), (2, 43), (3, 40), (3, 41), (3, 42)])
STARS = mirror([(0, 42), (1, 40), (1, 41), (1, 42), (2, 40), (2, 41), (2, 42), (3, 41)])  # five-pointed, pale gold
SHADES = mirror(cells([1, 2, 3], [39, 40, 41, 42, 43]) + cells([3], [44, 45, 46, 47]))  # and the bridge between
SMILE = cells([1], range(18, 30)) + mirror([(2, 16), (2, 17)])  # along a ring the ends turn up
GRIN = cells([3, 2], range(18, 30)) + cells([1], range(20, 28)) + cells([0], range(22, 26))  # open, flat on top
O = [(0, 23), (0, 24), (1, 22), (1, 25), (2, 22), (2, 25), (3, 23), (3, 24)]  # hollow, four rings tall
FLAT = cells([3], range(21, 27))
FROWN = mirror([(0, 19), (0, 20), (1, 20), (1, 21), (2, 21), (2, 22), (3, 22), (3, 23)])  # each step shares an
# LED with the next: one LED a step drew as a zigzag
WAIL = cells([1, 2, 3], [22, 23, 24, 25])
LIPS = cells([2], range(18, 30))  # a straight mouth for the tongue to hang from
TONGUE = cells([0, 1], [22, 23, 24, 25])
TEAR = cells([2, 3], [37, 38])  # under the left eye
STREAMS = mirror(cells([3], range(31, 39)))  # down the inner ring from both eyes
CHEEKS = mirror(cells([2, 3], [31, 32]))


def pupil(eye, dx, dy):
    """The cell of a 3 by 3 eye nearest a point dx, dy (panel units) from its middle: where it looks."""
    mx = sum(centre(*c)[0] for c in eye) / len(eye)
    my = sum(centre(*c)[1] for c in eye) / len(eye)
    return min(eye, key=lambda c: math.dist(centre(*c), (mx + dx, my + dy)))


def looking(dx, dy):
    """Both eyes, each with a dark pupil looking the same way."""
    return [(EYES, SKIN), ([pupil(LEFT_EYE, dx, dy), pupil(RIGHT_EYE, dx, dy)], "Black")]


def blink(t, opened=None, held=0.2, skin=SKIN):
    """Moves: the eyes shut at t seconds and open again, as `opened`, `held` seconds later."""
    return [(t, [(EYES, "Black"), (SHUT, skin)]), (t + held, [(SHUT, "Black")] + (opened or [(EYES, skin)]))]


def wink(t):
    return [(t, [(LEFT_EYE, "Black"), (LEFT_SHUT, SKIN)]), (t + 0.6, [(LEFT_SHUT, "Black"), (LEFT_EYE, SKIN)])]


def glancing(*spots):
    """Moves: the eyes looking at each (seconds, dx, dy) in turn."""
    return [(t, looking(dx, dy)) for t, dx, dy in spots]


LOOK_ROUND = glancing((2.5, 18, 0), (4.5, -18, 0), (6.5, 0, 18), (8.5, 0, 0)) + blink(10, looking(0, 0))
DARTING = glancing((1.5, -18, 0), (3, 18, 0), (4.5, -18, 0), (6, 18, 0), (7.5, 0, 0)) + blink(9, looking(0, 0))
RED = "Red"

FACES = {  # each mood's faces: (what, the face drawn first, (seconds, what changes) played once per turn)
    "error": [
        ("dizzy", [(CROSSES, RED), (O, RED)], []),
        ("frowning", [(EYES, RED), (FROWN, RED)], blink(3, skin=RED) + blink(9, skin=RED)),
        ("crying", [(EYES, RED), (FROWN, RED), (TEAR, "SkyBlue")], []),
        ("sobbing", [(SHUT, RED), (WAIL, RED), (STREAMS, "SkyBlue")], []),
    ],
    "waiting": [
        ("wide eyes, O", [(EYES, SKIN), (O, SKIN)], blink(3) + blink(9)),
        ("frowning", [(EYES, SKIN), (FROWN, SKIN)], blink(3) + blink(9)),
        ("a tear", [(EYES, SKIN), (FROWN, SKIN), (TEAR, "SkyBlue")], blink(5)),
        ("looking round for you", [(O, SKIN)] + looking(0, 0), DARTING),
    ],
    "done": [
        ("smile, wink", [(EYES, SKIN), (SMILE, SKIN)], wink(3)),
        ("grin", [(EYES, SKIN), (GRIN, SKIN)], blink(3) + blink(9)),
        ("laughing", [(HAPPY, SKIN), (GRIN, SKIN)], []),
        ("heart eyes", [(HEARTS, RED), (SMILE, SKIN)], []),
        ("star eyes", [(STARS, "Google26"), (GRIN, SKIN)], []),
        ("wink, tongue out", [(RIGHT_EYE + LEFT_SHUT, SKIN), (LIPS, SKIN), (TONGUE, RED)], []),
        ("sunglasses", [(SHADES, "Blue"), (SMILE, SKIN)], []),
        ("blushing", [(HAPPY, SKIN), (SMILE, SKIN), (CHEEKS, "MediumVioletRed")], []),
    ],
    "running": [
        ("looking around", [(FLAT, SKIN)] + looking(0, 0), LOOK_ROUND),
        ("smiling, looking around", [(SMILE, SKIN)] + looking(0, 0), LOOK_ROUND),
        ("sunglasses on", [(SHADES, "Blue"), (FLAT, SKIN)], []),
        ("tongue out, concentrating", [(LIPS, SKIN), (TONGUE, RED)] + looking(0, -18),
         glancing((2.5, -18, 0), (5, 0, -18), (7.5, 18, 0), (10, 0, -18))),
    ],
    "waitingSeen": [
        ("slow blink", [(EYES, SKIN), (FLAT, SKIN)], blink(6, held=0.3)),
        ("side-eye", [(FLAT, SKIN)] + looking(-18, 0), blink(6, looking(-18, 0), held=0.3)),
        ("dozing", [(SHUT, SKIN), (FLAT, SKIN)],
         [(5, [(SHUT, "Black"), (EYES, SKIN)]), (6.5, [(EYES, "Black"), (SHUT, SKIN)])]),
    ],
}

CHOSEN = {}  # (clock name, mood): (the VARY_S period it was picked in, its index in FACES)


def variant(name, state, now):
    """Which of the mood's faces to show: a new one at random each VARY_S, never the one shown last."""
    period, was = int(now // VARY_S), CHOSEN.get((name, state))
    if was is None or was[0] != period:
        choices = [i for i in range(len(FACES[state])) if was is None or i != was[1]] or [0]
        CHOSEN[(name, state)] = (period, random.choice(choices))
    return CHOSEN[(name, state)][1]


def segments(layers):
    """Fill segments for (cells, colour) layers, later ones over earlier ones."""
    g = [[None] * 48 for _ in range(4)]
    for where, color in layers:
        for r, p in where:
            g[r][p] = color
    return [statusring.segment(p, w, c, r, x) for p, w, c, r, x in ringwords.rectangles(g)]


def brows(states):
    """The states that get a pip, one tuple a brow (BROWS), in ORDER, at most MAX_BROW each."""
    return tuple(tuple(s for s in states if s in side)[:MAX_BROW] for side in BROWS)


def pips(left, right):
    """[(LED, state)] on the outer ring: each brow's pips two LEDs apart and centred over its eye, worst
    first from the left. A brow with none is left out; the other stays over its eye."""
    return [((middle + 2 * i - len(states) + 1) % 48, s)
            for middle, states in ((BROW, left), (47 - BROW, right)) for i, s in enumerate(states)]


def scene_bytes(want):
    state, which, (left, right) = want
    _, face, moves = FACES[state][which]
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    scene.object.add(method=obj.Fill, lifeTime=statusring.LIFETIME, result=obj.OK).fill.segment.extend(
        segments(face + [([(0, p)], PIP_COLORS[s]) for p, s in pips(left, right)]))
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
    return (states[0], variant(clock.name, states[0], now), brows(states)) if states else ()


def watch(clock):
    return statusring.watch(clock, picture, scene_bytes, SLOT, "claude face", "claude")


if __name__ == "__main__":  # print each face's size, and the states the tracker sees now
    import sys
    for state in ORDER:
        for which, (what, _, _) in enumerate(FACES[state]):
            sizes = [len(scene_bytes((state, which, (("error",) * n, ("done",) * n)))) for n in (0, MAX_BROW)]
            print(f"{state:11} {what:26} {sizes[0]:3} B, {sizes[1]:3} B with {2 * MAX_BROW} pips")
    if len(sys.argv) > 1:
        states = Sessions(sys.argv[1]).states(time.time())
        print(states, "brows:", brows(states))
