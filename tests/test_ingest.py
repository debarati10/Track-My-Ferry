import asyncio

import pytest

from trackmyferry.bus import MemoryBus, publish_message
from trackmyferry.ingest.ais import (
    build_position_message,
    build_static_message,
    parse_aisstream_message,
    parse_time_utc,
    subscription_message,
)
from trackmyferry.messages import Topics, VesselPosition, VesselStatic, decode


def test_parse_time_with_nanoseconds():
    ts = parse_time_utc("2026-10-13 14:05:06.123456789 +0000 UTC")
    assert ts == pytest.approx(1791900306.123456, abs=1e-5)


def test_parse_real_shaped_position_message():
    raw = {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 367123456, "ShipName": "OPPORTUNITY@@@@", "latitude": 40.70, "longitude": -74.0,
                     "time_utc": "2026-10-13 14:05:06.5 +0000 UTC"},
        "Message": {"PositionReport": {"UserID": 367123456, "Latitude": 40.7031, "Longitude": -74.0058,
                                       "Sog": 102.3, "Cog": 360, "TrueHeading": 511, "NavigationalStatus": 0}},
    }
    pos = parse_aisstream_message(raw)
    assert isinstance(pos, VesselPosition)
    assert pos.name == "OPPORTUNITY"
    assert pos.sog_kn is None and pos.cog is None and pos.heading is None  # "not available" sentinels
    assert (pos.lat, pos.lon) == (40.7031, -74.0058)


def test_builders_round_trip():
    pos = parse_aisstream_message(build_position_message(366900001, "SIM ER 02", 1_790_000_000.25, 40.7, -74.0, 21.4, 33.0, 34.0))
    assert pos.mmsi == 366900001 and pos.sog_kn == 21.4 and pos.ts == pytest.approx(1_790_000_000.25)
    st = parse_aisstream_message(build_static_message(366900001, "SIM ER 02", 1_790_000_000))
    assert isinstance(st, VesselStatic) and st.ship_type == 60 and st.length_m == pytest.approx(26.0)


def test_subscription_message():
    sub = subscription_message("KEY", (40.5, -74.1, 40.8, -73.8), mmsis=[1, 2])
    assert sub["BoundingBoxes"] == [[[40.5, -74.1], [40.8, -73.8]]]
    assert sub["FiltersShipMMSI"] == ["1", "2"]


def test_memory_bus_fan_out_and_decode():
    async def main():
        bus = MemoryBus()
        got_a, got_b = [], []

        async def consume(sink, n):
            async for topic, payload in bus.subscribe([Topics.AIS_POSITIONS], group="x"):
                payload.pop("_ingest_ts")
                sink.append(decode(topic, payload))
                if len(sink) == n:
                    return

        tasks = [asyncio.create_task(consume(got_a, 2)), asyncio.create_task(consume(got_b, 2))]
        await asyncio.sleep(0)
        for i in range(2):
            await publish_message(bus, VesselPosition(mmsi=i, ts=float(i), lat=40.7, lon=-74.0))
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        return got_a, got_b

    a, b = asyncio.run(main())
    assert [m.mmsi for m in a] == [0, 1] == [m.mmsi for m in b]
