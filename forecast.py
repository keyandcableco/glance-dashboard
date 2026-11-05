"""Forecast graph: the next 24 hours of humidity, temperature or chance of rain drawn around the ring
by the clock's own forecast scene, with the current value on the text line ("💧 64%").

Configured per clock in config.toml [clocks.<name>.forecast]:
    latitude = 40.0
    longitude = -75.0
    show = "humidity"     # humidity | temperature | rain
    source = "nws"        # nws: US National Weather Service (free, any use, US only)
                          # open-meteo: worldwide, free for non-commercial use only
    every_minutes = 10    # on the clock for the first show_minutes (2 or more) of every every_minutes;
    show_minutes = 2      # every_minutes = 0 keeps it on all the time. Switched on on the panel, it's
                          # shown at once for show_minutes.
Each screen gets about 15 s a turn and the clock often picks the same one again, so with only two or
three screens an always-on forecast was up a third to half of the time; part-time keeps it a glance.
It's a saved scene (SLOT), written at the start of each window (and every REFRESH_S while always on),
deleted at its end, and like the status ring it waits while an alert is showing. Values
are fetched at most every REFRESH_S and shifted to the current hour in between. Switched off
(screens.py), or with no [forecast] section, it's deleted (again after every reconnect) and nothing
is fetched.
"""
import asyncio
import calendar
import datetime
import logging
import struct
import time

import aiohttp

import statusring
from glance import pb

SLOT = 6
FORECAST = 7
RING_AND_TEXT = 24
REFRESH_S = 30 * 60
EVERY_MINUTES, SHOW_MINUTES = 10, 2
USER_AGENT = "glance-dashboard (github.com/keyandcableco/glance-dashboard)"  # NWS asks apps to identify themselves
ICON, DEGREE = 0xC2, 0xB0  # template: 0xC2 + icon code; 0x08 is replaced by the current value

SHOWS = {  # what: (NWS field, Open-Meteo field, icon code, unit bytes, low color, high color)
    "humidity": ("relativeHumidity", "relative_humidity_2m", 144, b"%", 0xFF9000, 0x0050FF),
    "temperature": ("temperature", "temperature_2m", 143, bytes([ICON, DEGREE]), 0x2080FF, 0xFF3000),
    "rain": ("probabilityOfPrecipitation", "precipitation_probability", 149, b"%", 0x203040, 0x00A0FF),
}

log = logging.getLogger("glanced.forecast")


async def nws_hours(http, cfg, field, cache={}):
    """The next 24 hourly values from the National Weather Service (US)."""
    key = (cfg["latitude"], cfg["longitude"])
    headers = {"User-Agent": cfg.get("user_agent", USER_AGENT)}
    if key not in cache:  # the points lookup gives this spot's forecast grid; it rarely changes
        async with http.get(f"https://api.weather.gov/points/{key[0]:.4f},{key[1]:.4f}", headers=headers) as r:
            r.raise_for_status()
            cache[key] = (await r.json())["properties"]["forecastHourly"]
    async with http.get(cache[key], headers=headers) as r:
        r.raise_for_status()
        periods = (await r.json())["properties"]["periods"]
    values = []
    for p in periods[:24]:
        v = p.get(field)
        values.append(v.get("value") if isinstance(v, dict) else v)
    return [0 if v is None else v for v in values]


async def open_meteo_hours(http, cfg, field):
    """The next 24 hourly values from Open-Meteo (free for non-commercial use only)."""
    params = {"latitude": cfg["latitude"], "longitude": cfg["longitude"], "hourly": field,
              "timezone": "auto", "temperature_unit": "fahrenheit", "forecast_hours": 24}
    async with http.get("https://api.open-meteo.com/v1/forecast", params=params) as r:
        r.raise_for_status()
        values = (await r.json())["hourly"][field]
    return [0 if v is None else v for v in values[:24]]


def scene_bytes(values, show, now):
    nws_field, om_field, icon, unit, low, high = SHOWS[show]
    hour = now.replace(minute=0, second=0, microsecond=0)
    ints = [int(round(v)) for v in values][:24]
    ints += [ints[-1]] * (24 - len(ints))  # always 24 values
    scene = pb.ForecastScene(
        timestamp=calendar.timegm(hour.timetuple()),  # local wall time written as if UTC, as the clock expects
        minColor=low, maxColor=high, min=min(ints), max=max(max(ints), min(ints) + 1),
        values=struct.pack("<24h", *ints),
        template=bytes([ICON, icon, 0x08]) + unit)
    return bytes([FORECAST, statusring.PRIORITY, RING_AND_TEXT, SLOT]) + scene.SerializeToString()


def in_window(now, every_s, show_s):
    """Whether the forecast belongs on the clock now: always with every_s 0, else for the first show_s
    of each every_s (on the epoch, so at :00, :10, :20... with the defaults)."""
    return not every_s or now % every_s < show_s


def hour_start(epoch):
    return datetime.datetime.fromtimestamp(epoch).replace(minute=0, second=0, microsecond=0).timestamp()


def from_hour(values, fetched, now):
    """The hourly values fetched at `fetched` (they start at that hour), from the current hour on."""
    return values[round((hour_start(now) - hour_start(fetched)) / 3600):]


async def watch(clock, cfg):
    show = cfg.get("show", "humidity")
    if show not in SHOWS:
        log.warning("%s: forecast show should be one of %s; using humidity", clock.name, ", ".join(SHOWS))
        show = "humidity"
    source = cfg.get("source", "nws")
    fetch = open_meteo_hours if source == "open-meteo" else nws_hours
    field = SHOWS[show][1] if source == "open-meteo" else SHOWS[show][0]
    try:
        every_s = float(cfg.get("every_minutes", EVERY_MINUTES)) * 60
        show_s = float(cfg.get("show_minutes", SHOW_MINUTES)) * 60
        if not (0 <= every_s < 10 ** 7 and 0 < show_s < 10 ** 7):
            raise ValueError
    except (TypeError, ValueError):
        log.warning("%s: forecast every_minutes and show_minutes should be numbers; using %d of every %d",
                    clock.name, SHOW_MINUTES, EVERY_MINUTES)
        every_s, show_s = EVERY_MINUTES * 60.0, SHOW_MINUTES * 60.0
    show_s = max(show_s, 120.0)  # checked every 30 s, and a fetch can take that long: 2 minutes at least
    values, fetched, next_fetch = [], 0.0, 0.0
    written, on_clock, client = 0.0, None, None  # on_clock None: not known (a new link)
    was_on, extra_until = clock.screens.on("forecast"), 0.0  # switched on on the panel: shown at once, for show_s
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            if clock.client is not client:  # a new link: the clock may still hold a forecast, or have lost ours
                client, on_clock = clock.client, None
            is_on = clock.screens.on("forecast")
            if is_on and not was_on:
                extra_until = now + show_s
            was_on = is_on
            if not clock.connected or statusring.busy(clock, now):
                continue
            if not (is_on and (in_window(now, every_s, show_s) or now < extra_until)):
                if on_clock is not False:
                    try:
                        await clock.write(bytes([statusring.SCENES_DELETE, 0, 0, SLOT]))
                        on_clock = False
                    except Exception as e:
                        log.warning("%s: removing the forecast failed: %s", clock.name, e)
                continue
            if on_clock and now - written < REFRESH_S:
                continue
            if now >= next_fetch:
                try:
                    got = await fetch(http, cfg, field)
                    if not got:
                        raise ValueError("no hourly values")
                    values, fetched, next_fetch = got, now, now + REFRESH_S
                    log.info("%s: forecast (%s): now %s, range %s-%s", clock.name, show, got[0], min(got), max(got))
                except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
                    log.warning("%s: forecast failed: %s", clock.name, e)
                    next_fetch = now + 5 * 60  # try again in 5 minutes; meanwhile the last values, if any
            ahead = from_hour(values, fetched, now)
            if not ahead:
                continue
            try:
                await clock.write(scene_bytes(ahead, show, datetime.datetime.fromtimestamp(now)))  # the now `ahead` was cut at
                on_clock, written = True, now
            except Exception as e:
                log.warning("%s: forecast update failed: %s", clock.name, e)
