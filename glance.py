#!/usr/bin/env python3
"""Send alerts to a paired Glance Clock over BLE (stock firmware, Hypfer's protocol).

  glance.py "ORDER {bell}" --color Red --anim Pulse --sound Bells
  glance.py --settings
  glance.py --list
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "glance"))
import glance_pb2 as pb  # noqa: E402
from bleak import BleakClient  # noqa: E402

import config  # noqa: E402

DEFAULT_ADDR = config.default_address()
CHAR = "5075fb2e-1e0e-11e7-93ae-92361f002671"

NOTIFY = 2
PRIORITIES = {"low": 1, "medium": 16, "high": 48, "highest": 64, "critical": 80}

# Built-in icon characters (Hypfer README), usable as {name} in text
ICONS = {
    "house": 128, "phone": 129, "clock": 130, "plug": 131, "smartphone": 132,
    "sun": 133, "smiley": 134, "block": 135, "bat0": 137, "bat1": 138, "bat2": 139,
    "bat3": 140, "bellmute": 141, "mute": 142, "thermo": 143, "drop": 144,
    "heart": 145, "baro": 146, "in": 147, "out": 148, "umbrella": 149, "bell": 150,
    "speaker": 151, "wind": 152, "cloud": 153, "deg": 176,
}


def encode_text(text, strict=False):
    """Text with {icon} names as the clock's bytes. An unknown {name} stays as plain text, so a
    calendar title or email subject with braces can't break anything; strict raises instead."""
    def icon(m):
        name = m.group(1).lower()
        if name in ICONS:
            return chr(ICONS[name])
        if strict:
            raise ValueError(f"unknown icon {{{name}}}; try --list")
        return m.group(0)
    return re.sub(r"\{(\w+)\}", icon, text).encode("latin-1", errors="replace")


def enum_value(enum, name):
    for key, val in enum.items():
        if key.lower() == name.lower():
            return val
    raise ValueError(f"unknown value {name!r}; try --list")


PIECE_BYTES = 30  # the clock shows about 32 characters of one text piece; longer text is cut off


def pieces(data, size=PIECE_BYTES):
    """Encoded text split at spaces into pieces of at most `size` bytes (a long word is cut)."""
    out, piece = [], b""
    for word in data.split(b" "):
        while len(word) > size:
            if piece:
                out.append(piece)
                piece = b""
            out.append(word[:size])
            word = word[size:]
        if piece and len(piece) + 1 + len(word) > size:
            out.append(piece)
            piece = b""
        piece = piece + b" " + word if piece else word
    return out + [piece] if piece else out


def build_notice(text, anim, sound, color, mode, strict=False):
    """A Notice; text longer than one piece goes as several pieces the clock shows in turn,
    each once (the default Repeat mode would show each twice)."""
    notice = pb.Notice(
        type=enum_value(pb.Animation, anim),
        sound=enum_value(pb.Sound, sound),
        color=enum_value(pb.Color, color),
    )
    parts = pieces(encode_text(text, strict)) or [b""]
    modificator = enum_value(pb.TextData.Modificator, mode if len(parts) == 1 else "None")
    for part in parts:
        notice.text.add(text=part, modificators=modificator)
    return notice.SerializeToString()


async def run(args):
    async with BleakClient(args.addr) as client:
        if args.settings:
            settings = pb.Settings()
            settings.ParseFromString(await client.read_gatt_char(CHAR))
            print(settings)
            return
        payload = build_notice(args.text, args.anim, args.sound, args.color, args.mode, strict=True)
        header = bytes([NOTIFY, PRIORITIES[args.priority], 0, 0])
        await client.write_gatt_char(CHAR, header + payload, response=True)


def print_lists():
    print("icons:     ", " ".join(ICONS))
    print("colors:    ", " ".join(pb.Color.keys()))
    print("animations:", " ".join(pb.Animation.keys()))
    print("sounds:    ", " ".join(pb.Sound.keys()))
    print("text modes:", " ".join(pb.TextData.Modificator.keys()))
    print("priorities:", " ".join(PRIORITIES))


def main():
    p = argparse.ArgumentParser(description="Send an alert to a Glance Clock.")
    p.add_argument("text", nargs="?", help="text to show; {bell}, {heart} etc. insert icons")
    p.add_argument("--color", default="Lime")
    p.add_argument("--anim", default="Pulse")
    p.add_argument("--sound", default="Radar", help="NoneSound for silent")
    p.add_argument("--mode", default="Repeat", help="text mode: None, Repeat, Rapid, Delay")
    p.add_argument("--priority", default="high", choices=PRIORITIES)
    p.add_argument("--addr", default=DEFAULT_ADDR)
    p.add_argument("--settings", action="store_true", help="print the clock's settings")
    p.add_argument("--list", action="store_true", help="list icons, colors, animations, sounds")
    args = p.parse_args()
    if args.list:
        return print_lists()
    if not args.settings and not args.text:
        p.error("give some text, or --settings / --list")
    try:
        asyncio.run(run(args))
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
