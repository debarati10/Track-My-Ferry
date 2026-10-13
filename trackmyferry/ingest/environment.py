"""Environment feeds: NOAA tidal current predictions and Open-Meteo weather (both free, no key).

* NOAA CO-OPS current predictions at Hell Gate (NYH1924) — signed so + means flood (north-bound in
  the East River), the convention the features use.
* Open-Meteo current conditions: wind speed/direction and precipitation over the harbour.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import requests

from ..messages import EnvObservation

log = logging.getLogger(__name__)

NOAA_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def fetch_current_predictions(station: str, hours: int = 24, bin_: int | None = None,
                              session: requests.Session | None = None) -> list[tuple[float, float]]:
    """Returns [(epoch, signed speed in knots)] at 6-minute resolution starting now."""
    s = session or requests.Session()
    start = datetime.now(UTC)
    params = {
        "product": "currents_predictions", "station": station, "application": "trackmyferry",
        "begin_date": start.strftime("%Y%m%d %H:%M"), "end_date": (start + timedelta(hours=hours)).strftime("%Y%m%d %H:%M"),
        "time_zone": "gmt", "units": "english", "interval": "6", "format": "json",
    }
    if bin_ is not None:
        params["bin"] = str(bin_)
    resp = s.get(NOAA_API, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    preds = (data.get("current_predictions") or {}).get("cp", [])
    out = []
    for p in preds:
        ts = datetime.strptime(p["Time"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()
        speed = float(p.get("Velocity_Major") or p.get("Speed") or 0.0)  # signed: + flood, - ebb
        out.append((ts, speed))
    return out


def fetch_weather(lat: float, lon: float, session: requests.Session | None = None) -> dict[str, float]:
    s = session or requests.Session()
    resp = s.get(OPEN_METEO, params={
        "latitude": lat, "longitude": lon, "current": "wind_speed_10m,wind_direction_10m,precipitation",
        "wind_speed_unit": "kn", "timezone": "UTC",
    }, timeout=20)
    resp.raise_for_status()
    cur = resp.json().get("current", {})
    return {"wind_kn": float(cur.get("wind_speed_10m") or 0.0), "wind_dir": float(cur.get("wind_direction_10m") or 0.0),
            "precip_mm": float(cur.get("precipitation") or 0.0)}


async def poll_environment(current_station: str, lat: float, lon: float, interval_s: float = 600.0,
                           current_bin: int | None = None) -> AsyncIterator[EnvObservation]:
    session = requests.Session()
    series: list[tuple[float, float]] = []
    fetched_at = 0.0
    while True:
        now = time.time()
        try:
            if not series or now - fetched_at > 6 * 3600:
                series = await asyncio.to_thread(fetch_current_predictions, current_station, 24, current_bin, session)
                fetched_at = now
            if series:
                ts, speed = min(series, key=lambda x: abs(x[0] - now))
                yield EnvObservation(now, "current", {"speed_kn": speed, "axis_deg": 32.0}, source="noaa")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("NOAA current fetch failed: %s", exc)
        try:
            w = await asyncio.to_thread(fetch_weather, lat, lon, session)
            yield EnvObservation(now, "wind", {"speed_kn": w["wind_kn"], "dir_deg": w["wind_dir"]}, source="open-meteo")
            yield EnvObservation(now, "weather", {"precip_mm": w["precip_mm"]}, source="open-meteo")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("weather fetch failed: %s", exc)
        await asyncio.sleep(interval_s)
