"""Status ring: an always-on drawing on the LED rings, under the clock's own time and date.

Outer ring (0): one 3-pixel segment per unread item (the oldest MAX_ITEMS), from 12 o'clock, in the
item's own color. Inner ring (3): the current active stretch growing toward the break nudge in
ARC_STEPS steps (green, then orange past FOCUS_WARN of the way); only while break reminders are on.

It's a custom scene in watch-face mode (8), saved in SLOT and rewritten only when the picture
changes. The clock shows one thing at a time, so the ring never redraws while a countdown is on
the ring or within QUIET_AFTER_S of an alert, which would cut either off. Saved scenes keep cycling
until deleted, so it only ever uses SLOT, deletes it when there's nothing to show, and clears a
leftover one after every (re)connect. A screen switched off (screens.py) is deleted and stays off.
The schedule ring (schedulering.py) is kept drawn the same way, in its own slot.

Also here: the red light flash that alerts about something due now open with.
"""
import asyncio
import logging
import time

from glance import PRIORITIES, pb  # glance puts the generated protocol module on the path

SLOT = 4
MODE_WATCHFACE = 8
PRIORITY = 16  # below alerts (48 high, 80 critical), so an alert always wins
LIFETIME = 50 * 60 * 30  # frames (50 fps): 30 minutes; rewritten well before that
REFRESH_S = 15 * 60
QUIET_AFTER_S = 45
ITEM_PX, ITEM_GAP, MAX_ITEMS = 3, 1, 12  # 12 × 4 = 48 pixels
ARC_STEPS = 12
FOCUS_WARN = 0.8
SCENE, SCENES_DELETE = 0, 33
MODE_WHOLE = 0
FLASH_SLOT = 3
FLASH_FRAMES = 150  # 3 s
COLORS = {name.lower(): name for name in pb.Color.keys()}

log = logging.getLogger("glanced.ring")


def segment(start, pixels, color, ring, extra_rings=0):
    """One ring segment: start 0 = 12 o'clock, ring 0 = outermost; extra_rings more inward."""
    return pb.Color.Value(color) << 16 | extra_rings << 14 | (pixels - 1) << 8 | ring << 6 | start


def color_of(item):
    return COLORS.get(str(item.get("color", "Lime")).lower(), "Lime")  # the replay's default too


def picture(clock, now):
    """The segments to draw now: a tuple, so pictures compare."""
    segs = []
    for i, item in enumerate(clock.unread.items[:MAX_ITEMS]):
        segs.append(segment(i * (ITEM_PX + ITEM_GAP), ITEM_PX, color_of(item), 0))
    focus = getattr(clock, "focus", None)
    if focus:
        since, break_s = focus
        share = min(1.0, max(0.0, (now - since) / max(60, break_s)))
        steps = max(1, int(share * ARC_STEPS))
        segs.append(segment(0, steps * 48 // ARC_STEPS, "Darkorange" if share >= FOCUS_WARN else "DarkGreen", 3))
    return tuple(segs)


def scene_bytes(segs):
    scene = pb.CustomScene()
    obj = scene.object.add(method=pb.CustomScene.Object.Fill, lifeTime=LIFETIME,
                           result=pb.CustomScene.Object.OK)
    obj.fill.segment.extend(segs)
    return bytes([SCENE, PRIORITY, MODE_WATCHFACE, SLOT]) + scene.SerializeToString()


def flash_bytes():
    """The light flash: all four rings red with a white strobe over them, FLASH_FRAMES long. It's a
    saved scene like any other, so whoever sends it deletes FLASH_SLOT once it has played."""
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    everything = segment(0, 48, "Red", 0, 3)
    scene.object.add(method=obj.Fill, lifeTime=FLASH_FRAMES, result=obj.Expired).fill.segment.append(everything)
    flash = scene.object.add(method=obj.AreaAnimation, lifeTime=FLASH_FRAMES, result=obj.Expired).areaAnimation
    flash.type = obj.AreaAnimationData.LightFlash
    flash.area.append(everything)
    flash.flashLight.color = pb.Color.Value("White")
    flash.flashLight.speed = 3
    return bytes([SCENE, PRIORITIES["critical"], MODE_WHOLE, FLASH_SLOT]) + scene.SerializeToString()


def busy(clock, now):
    """A countdown on the ring, or an alert that may still be showing."""
    return bool(clock.ring and clock.ring[0] > now) or now - clock.last_alert < QUIET_AFTER_S


async def watch(clock, picture=picture, scene_bytes=scene_bytes, slot=SLOT, what="status ring", screen="status"):
    """Keep picture(clock, now) drawn in slot (empty, or the screen switched off: the slot deleted)."""
    shown, shown_at, client, counting = None, 0.0, None, False
    while True:
        await asyncio.sleep(20)
        if not clock.connected:
            continue
        now = time.time()
        if clock.client is not client:  # new link: the clock may hold a leftover or have lost ours
            client, shown = clock.client, None
        if busy(clock, now):
            counting = True
            continue
        if counting:  # a countdown or alert just ended: put the ring back
            counting, shown = False, None
        want = picture(clock, now) if clock.screens.on(screen) else ()
        if want == shown and now - shown_at < REFRESH_S:
            continue
        try:
            if want:
                await clock.write(scene_bytes(want))
            else:
                await clock.write(bytes([SCENES_DELETE, 0, 0, slot]))
            shown, shown_at = want, now
        except Exception as e:
            log.warning("%s: %s update failed: %s", clock.name, what, e)
