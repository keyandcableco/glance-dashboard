"""Screensaver: one more screen in the turn that plays something different each time it comes round,
over the time and date. Off by default; switched per clock on the panel.

The clock shows each screen about 15 s and picks the next itself, so the bridge just rewrites the
saved scene every CHANGE_S: whenever the screen comes round it plays whatever was written last, from
the start. In turn:
- the clock's own ring animations (Gif objects), filling all four rings in a colour that changes too;
- its weather particles (Weather objects), falling in their own white-blue;
- drawn ones, where every LED can have its own colour: a rainbow with the clock's Wave over it,
  confetti (random colours, pastels too) with a Pulse over it, a sweep that fills each ring in its own
  colour and direction, and a rainbow painting itself round the dial.
All were seen on the shop clock (2026-11-04/05). The clock moves only its own animations smoothly; a
drawn LED stays lit until drawn over, and timed objects play once per turn and don't loop, so drawn
motion is limited to things that build up. The Animation enum's Weather* names are for alerts only (a
Gif with one is refused, 0x84). A true-RGB colour wheel drawn with appointment blocks (the only way to
any RGB colour) looked best of all, but after it was written the schedule ring's blocks stopped showing,
so it's left out. While a countdown runs the screen steps aside, so the countdown's own screen comes
round more often.

Kept drawn by statusring.watch like the other screens, in its own SLOT.
"""
import random

import statusring
from glance import pb

SLOT = 2
CHANGE_S = 20  # the screens' watcher looks every 20 s
SPEED = 3  # 0-10
ANIMATIONS = [("gif", "Fire"), ("drawn", "rainbow"), ("gif", "Flower"), ("weather", "Rain"), ("drawn", "confetti"),
              ("gif", "Fan"), ("gif", "Wheel"), ("drawn", "sweep"), ("gif", "Sun"), ("weather", "Snow"),
              ("gif", "Thunderstorm"), ("drawn", "paint"), ("gif", "Flower2"), ("weather", "Fog"), ("gif", "Cloud")]
COLORS = ["Red", "OrangeRed", "Darkorange", "DarkGoldenRod", "LawnGreen", "Lime", "MediumSpringGreen", "Aqua",
          "SkyBlue", "Blue", "BlueViolet", "MediumVioletRed"]  # in hue order: the rainbow's bands too
DIM = {"Black", "Maroon", "Brown", "Indigo", "DarkGreen", "Olive", "DarkMagenta"}
CONFETTI = [c for c in pb.Color.keys() if c not in DIM]  # the Google1-29 tints are mostly pastels
PATCH = 4  # confetti patch width in LEDs: 48 patches, ~250 bytes
PAINT_FRAMES = 50  # a rainbow band painted every second (the clock runs 50 frames a second)


def pick(turn):
    """(kind, animation, colour, turn) for the turn-th CHANGE_S: never the same animation twice running,
    the colour stepping 5 along so neighbours differ; the drawn ones vary with the turn."""
    kind, name = ANIMATIONS[turn % len(ANIMATIONS)]
    return kind, name, COLORS[turn * 5 % len(COLORS)], turn


def picture(clock, now):
    return () if statusring.counting_down(clock, now) else pick(int(now // CHANGE_S))


def bands(shift):
    """The rainbow: 12 bands of 4 LEDs across all four rings, starting from colour `shift` at 12 o'clock."""
    return [statusring.segment(i * 4, 4, COLORS[(i + shift) % len(COLORS)], 0, 3) for i in range(len(COLORS))]


def draw(scene, name, turn):
    obj = pb.CustomScene.Object
    everything = statusring.segment(0, 48, "White", 0, 3)  # an area: its colour isn't used
    if name in ("rainbow", "confetti"):
        if name == "rainbow":
            segs, effect = bands(turn), obj.AreaAnimationData.Wave
        else:
            rnd, segs, last = random.Random(turn), [], None
            for ring in range(4):
                for k in range(48 // PATCH):
                    last = rnd.choice([c for c in CONFETTI if c != last])
                    segs.append(statusring.segment(k * PATCH, PATCH, last, ring))
            effect = obj.AreaAnimationData.Pulse
        scene.object.add(method=obj.Fill, lifeTime=statusring.LIFETIME).fill.segment.extend(segs)
        area = scene.object.add(method=obj.AreaAnimation, lifeTime=statusring.LIFETIME).areaAnimation
        area.type = effect
        area.area.append(everything)
    elif name == "sweep":
        for ring in range(4):  # the bar fills its ring in the front colour from where it starts, then stays
            bar = scene.object.add(method=obj.MovingBar, lifeTime=statusring.LIFETIME).movingBar
            bar.area = statusring.segment(0, 48, "Blue", ring)
            bar.frontColor = pb.Color.Value(COLORS[(turn * 5 + ring * 3) % len(COLORS)])
            bar.backColor = pb.Color.Value("Black")
            bar.speed = (4 + ring) * (1 if ring % 2 == 0 else -1)
    else:  # paint: one band a second, clockwise from 12; each stays lit once drawn
        for i, seg in enumerate(bands(turn)):
            scene.object.add(method=obj.Fill, startTime=i * PAINT_FRAMES, lifeTime=statusring.LIFETIME).fill.segment.append(seg)


def scene_bytes(want):
    kind, name, color, turn = want
    scene, obj = pb.CustomScene(), pb.CustomScene.Object
    if kind == "weather":
        weather = scene.object.add(method=obj.Weather, lifeTime=statusring.LIFETIME).weather
        weather.condition = obj.WeatherData.Condition.Value(name)
        weather.position = obj.WeatherData.Position.Value("FullScreen")
        weather.intensity = 5
    elif kind == "drawn":
        draw(scene, name, turn)
    else:
        gif = scene.object.add(method=obj.Gif, lifeTime=statusring.LIFETIME).gif
        gif.type = pb.Animation.Value(name)
        gif.segment = statusring.segment(0, 48, color, 0, 3)  # all four rings; the colour bits set its colour
        gif.speed = SPEED
    return bytes([statusring.SCENE, statusring.PRIORITY, statusring.MODE_WATCHFACE, SLOT]) + scene.SerializeToString()


def watch(clock):
    return statusring.watch(clock, picture, scene_bytes, SLOT, "screensaver", "screensaver")
