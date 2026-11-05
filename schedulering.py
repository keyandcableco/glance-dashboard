"""Schedule ring: the coming hours' calendar events, pickups and hard stops as colored blocks around
the dial, drawn by the clock's own appointments scene (the calendar view the Glance app had).

Each appointment is 8 bytes, decoded from Hypfer's captures (upstream/hypfer/sample_appointments.js;
his notes leave the format open; all 144 captured appointments re-encode exactly) and checked on a clock:
    uint32 LE  start minute | length in minutes << 11 | reminder minutes << 22   (11, 11, 10 bits;
                                                                                reminder 1023 = none)
    blue, green, red, height                                                    (3, 6, 9, 12 = 1-4 rings
                                                                                from the outside)
The start is minutes after the scene's timestamp, which is local midnight written as if UTC, like the
forecast's. Blocks that overlap step down in height, the way the Glance app drew them, so a hard stop
inside a meeting still shows.

Blocks run from the current half hour to HOURS_AHEAD out, so they never wrap round onto now, and stop
at midnight. It's a saved scene in SLOT, kept drawn like the status ring (statusring.watch): rewritten
when the blocks change, never while an alert or countdown is showing, and deleted while the screen is
switched off (screens.py).
"""
import calendar
import datetime
import struct

import statusring
from glance import encode_text, pb

SLOT = 7
APPOINTMENTS = 8
HOURS_AHEAD = 11
NO_REMINDER = 1023
MIN_MINUTES = 15  # one LED on a 12-hour ring of 48
MAX_BLOCKS = 12
MAX_BYTES = 500  # one Bluetooth write carries at most 512
NAME_CHARS = 16
COLORS = {"calendar": (0, 255, 255), "pickup": (184, 134, 11), "stop": (255, 0, 0)}  # as their alerts: Aqua, DarkGoldenRod, Red


def picture(clock, now):
    """(local midnight, ((start minute, minutes, height, kind, name), ...)) to draw now, or () for nothing.
    Minutes are wall-clock minutes, so blocks stay on the right hour on daylight-saving days."""
    midnight = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)

    def minute(epoch):
        return (datetime.datetime.fromtimestamp(epoch) - midnight).total_seconds() / 60

    first = int(minute(now)) // 30 * 30  # the current half hour
    last = min(first + HOURS_AHEAD * 60, 24 * 60)
    spans = [(start, end, "calendar", title) for _uid, start, title, end in clock.events]
    spans += [(i["due"], i["due"] + 30 * 60, i["kind"], i["text"]) for i in clock.unread.items
              if i.get("kind") in ("pickup", "stop") and i.get("due")]
    placed = []
    for start, end, kind, name in sorted(spans, key=lambda s: (s[0], -s[1])):  # longer first at the same start
        a, b = minute(start), minute(end)
        if b <= first or a >= last:
            continue  # over, or beyond what the ring shows
        a = int(max(a, first))
        length = max(MIN_MINUTES, int(min(b, last)) - a)
        under = sum(1 for p in placed if p[0] < a + length and a < p[0] + p[1])
        placed.append((a, length, max(3, 12 - 3 * under), kind, name))
    placed = sorted(placed[:MAX_BLOCKS], key=lambda p: -p[2])  # taller first, so shorter ones draw over them
    return (calendar.timegm(midnight.timetuple()), tuple(placed)) if placed else ()


def scene_bytes(want):
    timestamp, blocks = want
    while True:
        scene = pb.AppointmentsScene(timestamp=timestamp, notify=pb.AppointmentsScene.DISABLED,
                                     sound=pb.Sound.Value("NoneSound"))
        data = b""
        for start, minutes, height, kind, name in blocks:
            r, g, b = COLORS[kind]
            data += struct.pack("<I", start | min(minutes, 2047) << 11 | NO_REMINDER << 22) + bytes([b, g, r, height])
            scene.appointmentNames.add(text=encode_text(name[:NAME_CHARS]))
        scene.appointmentData = data
        out = bytes([APPOINTMENTS, 0, statusring.MODE_WATCHFACE, SLOT]) + scene.SerializeToString()
        if len(out) <= MAX_BYTES or len(blocks) == 1:
            return out
        blocks = blocks[:-1]  # too long for one write: leave one off


def watch(clock):
    return statusring.watch(clock, picture, scene_bytes, SLOT, "schedule ring", "schedule")
