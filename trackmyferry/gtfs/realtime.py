"""GTFS-realtime: parse NYC Ferry's TripUpdates / Alerts protobuf feeds, and encode TripUpdates.

The encoder exists so the simulator can publish a byte-for-byte GTFS-realtime feed; that way the
same parsing code is exercised in simulation, tests and production.
"""
from __future__ import annotations

import time
from collections.abc import Iterable

from ..messages import ServiceAlert, StopTimeUpdate, TripUpdate

try:  # pip install gtfs-realtime-bindings
    from google.transit import gtfs_realtime_pb2
except ImportError:  # pragma: no cover - surfaced at call time
    gtfs_realtime_pb2 = None


def _require():
    if gtfs_realtime_pb2 is None:
        raise RuntimeError("gtfs-realtime-bindings is not installed: pip install gtfs-realtime-bindings")
    return gtfs_realtime_pb2


def parse_trip_updates(data: bytes) -> tuple[float, list[TripUpdate]]:
    """Returns (feed timestamp, trip updates)."""
    pb = _require()
    feed = pb.FeedMessage()
    feed.ParseFromString(data)
    feed_ts = float(feed.header.timestamp or time.time())
    out: list[TripUpdate] = []
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        trip = tu.trip
        stus: list[StopTimeUpdate] = []
        for stu in tu.stop_time_update:
            arr = stu.arrival if stu.HasField("arrival") else None
            dep = stu.departure if stu.HasField("departure") else None
            stus.append(StopTimeUpdate(
                stop_id=stu.stop_id,
                stop_sequence=stu.stop_sequence if stu.HasField("stop_sequence") else None,
                arrival_ts=float(arr.time) if arr is not None and arr.HasField("time") else None,
                departure_ts=float(dep.time) if dep is not None and dep.HasField("time") else None,
                arrival_delay_s=arr.delay if arr is not None and arr.HasField("delay") else None,
                departure_delay_s=dep.delay if dep is not None and dep.HasField("delay") else None,
                skipped=stu.schedule_relationship == pb.TripUpdate.StopTimeUpdate.SKIPPED,
            ))
        out.append(TripUpdate(
            ts=float(tu.timestamp or feed_ts),
            trip_id=trip.trip_id,
            route_id=trip.route_id or None,
            direction_id=trip.direction_id if trip.HasField("direction_id") else None,
            start_date=trip.start_date or None,
            vehicle_id=tu.vehicle.id or None if tu.HasField("vehicle") else None,
            vehicle_label=tu.vehicle.label or None if tu.HasField("vehicle") else None,
            stop_updates=stus,
        ))
    return feed_ts, out


def parse_alerts(data: bytes) -> list[ServiceAlert]:
    pb = _require()
    feed = pb.FeedMessage()
    feed.ParseFromString(data)
    feed_ts = float(feed.header.timestamp or time.time())

    def text(ts) -> str | None:
        for tr in ts.translation:
            if not tr.language or tr.language.startswith("en"):
                return tr.text
        return ts.translation[0].text if ts.translation else None

    out = []
    for ent in feed.entity:
        if not ent.HasField("alert"):
            continue
        a = ent.alert
        period = a.active_period[0] if a.active_period else None
        out.append(ServiceAlert(
            ts=feed_ts,
            alert_id=ent.id,
            header=text(a.header_text),
            description=text(a.description_text),
            route_ids=sorted({ie.route_id for ie in a.informed_entity if ie.route_id}),
            stop_ids=sorted({ie.stop_id for ie in a.informed_entity if ie.stop_id}),
            active_from=float(period.start) if period is not None and period.start else None,
            active_to=float(period.end) if period is not None and period.end else None,
        ))
    return out


def encode_trip_updates(updates: Iterable[TripUpdate], feed_ts: float) -> bytes:
    pb = _require()
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = pb.FeedHeader.FULL_DATASET
    feed.header.timestamp = int(feed_ts)
    for u in updates:
        ent = feed.entity.add()
        ent.id = f"{u.trip_id}:{u.start_date or ''}"
        tu = ent.trip_update
        tu.trip.trip_id = u.trip_id
        if u.route_id:
            tu.trip.route_id = u.route_id
        if u.direction_id is not None:
            tu.trip.direction_id = u.direction_id
        if u.start_date:
            tu.trip.start_date = u.start_date
        if u.vehicle_id or u.vehicle_label:
            tu.vehicle.id = u.vehicle_id or ""
            tu.vehicle.label = u.vehicle_label or ""
        tu.timestamp = int(u.ts)
        for s in u.stop_updates:
            stu = tu.stop_time_update.add()
            stu.stop_id = s.stop_id
            if s.stop_sequence is not None:
                stu.stop_sequence = s.stop_sequence
            if s.skipped:
                stu.schedule_relationship = pb.TripUpdate.StopTimeUpdate.SKIPPED
                continue
            if s.arrival_ts is not None:
                stu.arrival.time = int(s.arrival_ts)
                if s.arrival_delay_s is not None:
                    stu.arrival.delay = int(s.arrival_delay_s)
            if s.departure_ts is not None:
                stu.departure.time = int(s.departure_ts)
                if s.departure_delay_s is not None:
                    stu.departure.delay = int(s.departure_delay_s)
    return feed.SerializeToString()
