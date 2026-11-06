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
import unicodedata
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
    "speaker": 151, "wind": 152, "cloud": 153, "deg": "°",
}

# The clock reads its text as UTF-8, with the icons as single bytes 0x80-0x99 among it. These are the
# characters past ASCII seen drawn on the clock (2026-11-05): every Latin-1 letter À-ÿ, and €; °, ¢ and £
# are single bytes there.
SHOWN = {"°": b"\xb0", "€": b"\xe2\x82\xac", "¢": b"\xa2", "£": b"\xa3",
         **{chr(c): chr(c).encode() for c in range(0xC0, 0x100) if c not in (0xD7, 0xF7)}}
# Other letters lose their accents (ł shows as l); these are changed by hand.
FOLD = {"‘": "'", "’": "'", "‚": "'", "′": "'", "“": '"', "”": '"', "„": '"', "″": '"', "‐": "-", "‑": "-", "‒": "-",
        "–": "-", "—": "-", "―": "-", "−": "-", "…": "...", "×": "x", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "µ": "u", "μ": "u",
        "Ω": "ohm", "℃": "°C", "℉": "°F", "º": "°", "•": "*", "·": "-", "«": '"', "»": '"', "‹": "'",
        "›": "'", "→": "->", "←": "<-", "±": "+/-", "≤": "<=", "≥": ">=", "≠": "!=", "÷": "/"}


def clock_char(c):
    """One character as the clock can show it: itself, plain letters, "?" for a letter it can't show,
    or "" for an emoji, symbol or control character, which are left out."""
    if " " <= c <= "~" or c in SHOWN:
        return c
    if c in FOLD:
        return FOLD[c]
    kind = unicodedata.category(c)
    if kind in ("So", "Sk") or kind[0] in "CMZ":  # 0x0A hides the whole text; other control bytes show "?"
        return ""
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", c).replace("\u2044", "/") if " " <= ch <= "~")
    return f" {plain} " if "/" in plain else plain or "?"  # 1½ is 1 1/2


def clock_text(text):
    """Text as the clock will show it: one space between words, nothing it can't draw."""
    # mail that says Latin-1 but means Windows-1252 leaves C1 characters: ’ arrives as \x92
    text = "".join(bytes([ord(c)]).decode("cp1252", errors="ignore") if "\x80" <= c <= "\x9f" else c for c in str(text))
    text = unicodedata.normalize("NFC", " ".join(text.split()))  # an accent sent as its own mark joins its letter
    return " ".join("".join(map(clock_char, text)).split())


def encode_text(text, strict=False):
    """Text with {icon} names as the clock's bytes. An unknown {name} stays as plain text, so a
    calendar title or email subject with braces can't break anything; strict raises instead."""
    out = []
    for n, part in enumerate(re.split(r"(\{\w+\})", clock_text(text))):
        name = part[1:-1].lower()
        if n % 2 and name in ICONS:
            icon = ICONS[name]
            out.append(SHOWN[icon] if isinstance(icon, str) else bytes([icon]))
            continue
        if n % 2 and strict:
            raise ValueError(f"unknown icon {{{name}}}; try --list")
        out += [SHOWN.get(c) or c.encode("ascii") for c in part]
    return b"".join(out)


def enum_value(enum, name):
    for key, val in enum.items():
        if key.lower() == name.lower():
            return val
    raise ValueError(f"unknown value {name!r}; try --list")


PIECE_BYTES = 30  # the clock shows about 32 characters of one text piece; longer text is cut off


def cut(word, size):
    """Where to cut a long word so no character's bytes are split: encode_text's only multi-byte
    characters are UTF-8 with a lead byte C2-EF, and every other byte is a character of its own."""
    at = 0
    while at < len(word):
        step = 3 if 0xE0 <= word[at] <= 0xEF else 2 if 0xC2 <= word[at] <= 0xDF else 1
        if at + step > size:
            break
        at += step
    return at or size


def pieces(data, size=PIECE_BYTES):
    """Encoded text split at spaces into pieces of at most `size` bytes (a long word is cut)."""
    out, piece = [], b""
    for word in data.split(b" "):
        while len(word) > size:
            if piece:
                out.append(piece)
                piece = b""
            at = cut(word, size)
            out.append(word[:at])
            word = word[at:]
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
