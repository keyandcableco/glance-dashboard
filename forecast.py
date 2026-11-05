"""Forecast graph: the next 24 hours of humidity, temperature or chance of rain drawn around the ring
by the clock's own forecast scene, with the current value on the text line ("💧 64%").

Configured per clock in config.toml [clocks.<name>.forecast]:
    latitude = 40.0
    longitude = -75.0
    show = "humidity"     # humidity | temperature | rain
    source = "nws"        # nws: US National Weather Service (free, any use, US only)
                          # open-meteo: worldwide, free for non-commercial use only
It's a saved scene (SLOT), refreshed every REFRESH_S, and like the status ring it waits while an
alert or countdown is showing. Switched off (screens.py), or with no [forecast] section, it's deleted
(again after every reconnect) and nothing is fetched.
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


async def watch(clock, cfg):
    show = cfg.get("show", "humidity")
    if show not in SHOWS:
        log.warning("%s: forecast show should be one of %s; using humidity", clock.name, ", ".join(SHOWS))
        show = "humidity"
    source = cfg.get("source", "nws")
    fetch = open_meteo_hours if source == "open-meteo" else nws_hours
    field = SHOWS[show][1] if source == "open-meteo" else SHOWS[show][0]
    sent_at, removed, client = 0.0, False, None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            if not clock.screens.on("forecast"):
                sent_at = 0.0  # drawn again soon after it's switched back on
                if clock.client is not client:  # a new link: the clock may still hold one
                    client, removed = clock.client, False
                if not removed and clock.connected and not statusring.busy(clock, now):
                    try:
                        await clock.write(bytes([statusring.SCENES_DELETE, 0, 0, SLOT]))
                        removed = True
                    except Exception as e:
                        log.warning("%s: removing the forecast failed: %s", clock.name, e)
                continue
            removed = False
            if now - sent_at < REFRESH_S or not clock.connected or statusring.busy(clock, now):
                continue
            try:
                values = await fetch(http, cfg, field)
                if not values:
                    raise ValueError("no hourly values")
                await clock.write(scene_bytes(values, show, datetime.datetime.now()))
                sent_at = now
                log.info("%s: forecast (%s): now %s, range %s-%s", clock.name, show, values[0], min(values), max(values))
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
                log.warning("%s: forecast failed: %s", clock.name, e)
                sent_at = now - REFRESH_S + 5 * 60  # try again in 5 minutes
