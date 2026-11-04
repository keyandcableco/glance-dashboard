#!/usr/bin/env python3
"""Change one Settings field on the clock: set_setting.py nightModeEnabled false | displayBrightness 0x1EDCC8

Works while glanced is running, but when this exits the bridge's link drops for a few seconds and reconnects.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "glance"))
import glance_pb2 as pb  # noqa: E402
from bleak import BleakClient  # noqa: E402

from glance import CHAR, DEFAULT_ADDR  # noqa: E402

SETTINGS = bytes([5, 0, 0, 0])


def parse(field, raw):
    kind = pb.Settings.DESCRIPTOR.fields_by_name[field].type
    if kind == pb.Settings.DESCRIPTOR.fields_by_name["nightModeEnabled"].type:  # bool
        return raw.lower() in ("1", "true", "on", "yes")
    return int(raw, 0)


async def main(field, raw):
    async with BleakClient(DEFAULT_ADDR) as client:
        s = pb.Settings()
        s.ParseFromString(await client.read_gatt_char(CHAR))
        print(f"{field}: {getattr(s, field)!r} -> ", end="")
        setattr(s, field, parse(field, raw))
        await client.write_gatt_char(CHAR, SETTINGS + s.SerializeToString(), response=True)
        s.ParseFromString(await client.read_gatt_char(CHAR))
        print(repr(getattr(s, field)))


asyncio.run(main(sys.argv[1], sys.argv[2]))
