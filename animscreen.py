"""Animation screen: one of the clock's own ring animations as a screen in the turn, over its time and
date. Fire while a burn-in runs (a timer or staged timer with "burn" in its name or in any stage's
label: the whole run), else the day's holiday animation, else nothing.

It's a saved custom scene in watch-face mode (8), SLOT, kept drawn by statusring.watch like the status
ring, and switched on and off on the panel as the "animation" screen. The fire goes on within about a
minute of the burn-in's start (the screens wait out the countdown's own start alert) and stays for the
whole run, the countdown untouched (seen 2026-11-05). The animations (Gif objects) fill all four rings in
the colour they're given; all were seen on the shop clock (2026-11-04), Fire in this mode under the time.
"""
import datetime

import statusring
from glance import pb

SLOT = 5
SPEED = 3  # 0-10
FIRE = ("Fire", "OrangeRed")
HOLIDAYS = {  # (month, day): (animation, colour)
    (1, 1): ("Thunderstorm", "DarkGoldenRod"),  # its white-hot flashes stand in for fireworks
    (2, 14): ("Flower", "MediumVioletRed"),
    (3, 17): ("Flower2", "LimeGreen"),
    (7, 4): ("Thunderstorm", "Red"),
    (10, 31): ("Fire", "Darkorange"),
    (12, 24): ("Flower", "Red"),
    (12, 25): ("Flower", "Red"),
    (12, 31): ("Thunderstorm", "DarkGoldenRod"),
}
THANKSGIVING = ("Sun", "DarkGoldenRod")  # the fourth Thursday of November


def holiday(day):
    """(animation, colour) for this date, or None."""
    if day.month == 11 and day.weekday() == 3 and 22 <= day.day <= 28:
        return THANKSGIVING
    return HOLIDAYS.get((day.month, day.day))


def burns(item):
    """A timer or staged timer whose name, or any of its stages, says burn (burn-in, burning in)."""
    labels = [str(item.get("name", ""))] + [str(stage[0]) for stage in item.get("stages") or ()]
    return item.get("kind") in ("timer", "stages") and any("BURN" in label.upper() for label in labels)


def burning(clock, now):
    return any(burns(item) and (item.get("due") or 0) > now for item in clock.unread.items)


def picture(clock, now):
    if burning(clock, now):
        return FIRE
    return holiday(datetime.date.fromtimestamp(now)) or ()


def scene_bytes(want):
    animation, color = want
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    gif = scene.object.add(method=obj.Gif, lifeTime=statusring.LIFETIME).gif
    gif.type = pb.Animation.Value(animation)
    gif.segment = statusring.segment(0, 48, color, 0, 3)  # all four rings; the colour bits set its colour
    gif.speed = SPEED
    return bytes([statusring.SCENE, statusring.PRIORITY, statusring.MODE_WATCHFACE, SLOT]) + scene.SerializeToString()


def watch(clock):
    return statusring.watch(clock, picture, scene_bytes, SLOT, "animation screen", "animation")
