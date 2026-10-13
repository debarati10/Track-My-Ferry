"""AIS ingest from aisstream.io (free WebSocket feed of ship positions; needs an API key).

`parse_aisstream_message` turns one aisstream JSON message into a `VesselPosition` or
`VesselStatic`. The simulator builds messages in the same JSON shape with `build_*`, so the parser
is exercised identically in simulation and production.

Docs: https://aisstream.io/documentation
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ..messages import VesselPosition, VesselStatic

log = logging.getLogger(__name__)

POSITION_TYPES = ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport")
STATIC_TYPES = ("ShipStaticData", "StaticDataReport")

SOG_NOT_AVAILABLE = 102.3
COG_NOT_AVAILABLE = 360.0
HEADING_NOT_AVAILABLE = 511


def parse_time_utc(value: str | None) -> float:
    """aisstream format: '2023-08-07 16:31:35.123456789 +0000 UTC' (nanosecond precision)."""
    if not value:
        return time.time()
    try:
        stamp = value.split(" +")[0].strip()
        if "." in stamp:
            base, frac = stamp.split(".")
            stamp = f"{base}.{frac[:6]}"
            dt = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f")
        else:
            dt = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=UTC).timestamp()
    except ValueError:
        return time.time()


def format_time_utc(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") + " +0000 UTC"


def _clean_name(name: Any) -> str | None:
    if not name:
        return None
    s = str(name).replace("@", " ").strip()
    return s or None


def parse_aisstream_message(msg: dict[str, Any]) -> VesselPosition | VesselStatic | None:
    mtype = msg.get("MessageType")
    meta = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get(mtype) or {}
    mmsi = int(meta.get("MMSI") or body.get("UserID") or 0)
    if not mmsi:
        return None
    ts = parse_time_utc(meta.get("time_utc"))
    if mtype in POSITION_TYPES:
        lat = body.get("Latitude", meta.get("latitude"))
        lon = body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return None
        sog = body.get("Sog")
        cog = body.get("Cog")
        hdg = body.get("TrueHeading")
        return VesselPosition(
            mmsi=mmsi,
            ts=ts,
            lat=float(lat),
            lon=float(lon),
            sog_kn=None if sog is None or sog >= SOG_NOT_AVAILABLE else float(sog),
            cog=None if cog is None or cog >= COG_NOT_AVAILABLE else float(cog),
            heading=None if hdg is None or hdg == HEADING_NOT_AVAILABLE else float(hdg),
            nav_status=body.get("NavigationalStatus"),
            name=_clean_name(meta.get("ShipName")),
            source="ais",
        )
    if mtype in STATIC_TYPES:
        dim = body.get("Dimension") or {}
        length = (dim.get("A") or 0) + (dim.get("B") or 0)
        name = body.get("Name")
        if mtype == "StaticDataReport":  # class B, part A carries the name
            name = (body.get("ReportA") or {}).get("Name") or name
        return VesselStatic(
            mmsi=mmsi,
            ts=ts,
            name=_clean_name(name) or _clean_name(meta.get("ShipName")),
            ship_type=body.get("Type"),
            call_sign=_clean_name(body.get("CallSign")),
            length_m=float(length) if length else None,
            destination=_clean_name(body.get("Destination")),
        )
    return None


def build_position_message(mmsi: int, name: str, ts: float, lat: float, lon: float, sog_kn: float,
                           cog: float, heading: float, nav_status: int = 0) -> dict[str, Any]:
    return {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": mmsi, "ShipName": name, "latitude": lat, "longitude": lon,
                     "time_utc": format_time_utc(ts)},
        "Message": {"PositionReport": {
            "UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": round(sog_kn, 1),
            "Cog": round(cog % 360, 1), "TrueHeading": int(round(heading)) % 360,
            "NavigationalStatus": nav_status, "Valid": True,
        }},
    }


def build_static_message(mmsi: int, name: str, ts: float, ship_type: int = 60, length_m: float = 26.0) -> dict[str, Any]:
    return {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": mmsi, "ShipName": name, "time_utc": format_time_utc(ts)},
        "Message": {"ShipStaticData": {
            "UserID": mmsi, "Name": name, "Type": ship_type, "CallSign": f"SIM{mmsi % 1000:03d}",
            "Dimension": {"A": length_m * 0.6, "B": length_m * 0.4, "C": 4, "D": 4},
            "Destination": "NYC FERRY", "Valid": True,
        }},
    }


def subscription_message(api_key: str, bbox: tuple[float, float, float, float],
                         mmsis: list[int] | None = None) -> dict[str, Any]:
    south, west, north, east = bbox
    sub: dict[str, Any] = {
        "APIKey": api_key,
        "BoundingBoxes": [[[south, west], [north, east]]],
        "FilterMessageTypes": list(POSITION_TYPES + STATIC_TYPES),
    }
    if mmsis:
        sub["FiltersShipMMSI"] = [str(m) for m in mmsis[:50]]
    return sub


async def stream_aisstream(api_key: str, bbox: tuple[float, float, float, float],
                           url: str = "wss://stream.aisstream.io/v0/stream",
                           mmsis: list[int] | None = None) -> AsyncIterator[VesselPosition | VesselStatic]:
    """Yields parsed messages forever, reconnecting with exponential backoff."""
    import websockets  # pip install websockets

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
                await ws.send(json.dumps(subscription_message(api_key, bbox, mmsis)))
                log.info("aisstream connected, bbox=%s", bbox)
                backoff = 1.0
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if "error" in data:
                        log.error("aisstream error: %s", data["error"])
                        break
                    parsed = parse_aisstream_message(data)
                    if parsed is not None:
                        yield parsed
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network errors, server restarts
            log.warning("aisstream disconnected (%s); reconnecting in %.0fs", exc, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)
