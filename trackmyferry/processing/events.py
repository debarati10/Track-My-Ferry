"""Detect arrivals at and departures from ferry landings using geofences + speed.

These detected events are the *labels* for the ETA model (the actual arrival time at each stop),
so their accuracy is validated against simulator ground truth in the tests.

Rules
-----
arrival   : inside a landing's radius and speed <= `arrive_kn`. The time is taken from the first
            such fix; if the boat was still creeping, add remaining distance / speed.
departure : after an arrival, speed >= `depart_kn` or outside the radius. The time is estimated by
            backing out the distance already travelled under constant acceleration, bounded by the
            last stationary fix.
Boats that pass through a geofence at speed produce no event.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..geo import KNOT_MS, haversine_m
from ..gtfs.static import Stop
from ..messages import StopEvent
from .tracker import Fix

TYPICAL_ACCEL = 0.35  # m/s^2


@dataclass
class _Visit:
    stop_id: str
    arrival_ts: float
    last_slow_ts: float


class StopEventDetector:
    def __init__(self, stops: dict[str, Stop], radius_m: float = 150.0, arrive_kn: float = 3.0,
                 depart_kn: float = 4.0):
        self.stop_ids = list(stops)
        self.lat = np.array([stops[s].lat for s in self.stop_ids])
        self.lon = np.array([stops[s].lon for s in self.stop_ids])
        self.radius_m = radius_m
        self.arrive_kn = arrive_kn
        self.depart_kn = depart_kn
        self.visits: dict[int, _Visit] = {}

    def nearest(self, lat: float, lon: float) -> tuple[str, float]:
        d = haversine_m(lat, lon, self.lat, self.lon)
        i = int(np.argmin(d))
        return self.stop_ids[i], float(d[i])

    def distance_to(self, stop_id: str, lat: float, lon: float) -> float:
        i = self.stop_ids.index(stop_id)
        return haversine_m(lat, lon, self.lat[i], self.lon[i])

    def docked_at(self, mmsi: int) -> tuple[str, float] | None:
        v = self.visits.get(mmsi)
        return (v.stop_id, v.arrival_ts) if v else None

    def update(self, mmsi: int, fix: Fix, prev: Fix | None) -> list[StopEvent]:
        visit = self.visits.get(mmsi)
        if visit is None:
            stop_id, d = self.nearest(fix.lat, fix.lon)
            if d <= self.radius_m and fix.sog_kn <= self.arrive_kn:
                creep = d / max(fix.sog_kn * KNOT_MS, 0.5) if fix.sog_kn > 0.3 and d > 25 else 0.0
                arrival = fix.ts + min(creep, 60.0)
                if prev is not None and prev.sog_kn <= self.arrive_kn and prev.ts > fix.ts - 30:
                    arrival = min(arrival, prev.ts)
                self.visits[mmsi] = _Visit(stop_id, arrival, fix.ts)
                return [StopEvent(mmsi=mmsi, stop_id=stop_id, arrival_ts=arrival)]
            return []

        d = self.distance_to(visit.stop_id, fix.lat, fix.lon)
        if fix.sog_kn >= self.depart_kn or d > self.radius_m:
            v = max(fix.sog_kn * KNOT_MS, 0.5)
            since = min(v / TYPICAL_ACCEL, math.sqrt(2 * max(d, 1.0) / TYPICAL_ACCEL))
            departure = max(visit.last_slow_ts, fix.ts - since)
            del self.visits[mmsi]
            return [StopEvent(mmsi=mmsi, stop_id=visit.stop_id, arrival_ts=visit.arrival_ts,
                              departure_ts=max(departure, visit.arrival_ts))]
        visit.last_slow_ts = fix.ts
        return []
