"""Vessel -> trip matching.

AIS says *where a boat is*; GTFS(-realtime) says *which trips are running*. Nothing links the two
(NYC Ferry's feed has no vehicle positions), so we infer it. Routes share long stretches of river
(East River and Astoria both run Pier 11 <-> E 34th St), so geometry alone is ambiguous.

Each candidate (pattern, trip) gets a cost (lower is better) built from four pieces of evidence:

1. geometry  - cross-track distance of recent fixes to the pattern's shape;
2. direction - recent along-track progress must agree with the pattern's direction;
3. stops     - the landings the boat actually docked at must be on the pattern, in order, and it
               must not have sailed past a pattern stop without stopping;
4. schedule  - how late the boat would be on each active trip of the pattern at its current position
               (ferries rarely run early because they hold at landings).

If the GTFS-realtime feed names the vessel on a trip, that is used as strong evidence. The caller
(`Pipeline`) applies hysteresis and makes sure two boats never hold the same trip.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..geo import haversine_m
from ..gtfs.static import GTFS, Pattern, TripInstance
from .tracker import Track

MAX_CROSS_M = 220.0
NO_TRIP_COST = 7.0


@dataclass
class Candidate:
    pattern: Pattern
    trip: TripInstance | None
    along_m: float
    cross_m: float
    cost: float
    lateness_s: float | None

    @property
    def trip_key(self) -> str | None:
        return self.trip.key if self.trip else None


@dataclass
class Visit:
    stop_id: str
    arrival_ts: float
    departure_ts: float | None


class TripMatcher:
    def __init__(self, gtfs: GTFS, max_cross_m: float = MAX_CROSS_M):
        self.gtfs = gtfs
        self.max_cross_m = max_cross_m

    def candidates(
        self,
        track: Track,
        now: float,
        visits: list[Visit],
        docked: tuple[str, float] | None,
        active: list[TripInstance],
        hint_trip_key: str | None = None,
    ) -> list[Candidate]:
        last = track.last
        if last is None:
            return []
        recent = track.recent(420.0) or [last]
        if len(recent) > 10:  # evenly subsample; geometry evidence saturates quickly
            idx = np.linspace(0, len(recent) - 1, 10).round().astype(int)
            recent = [recent[i] for i in idx]
        rlat = np.array([f.lat for f in recent])
        rlon = np.array([f.lon for f in recent])
        moving = track.mean_sog(90.0) > 4.0
        traveled = 0.0
        if len(recent) >= 2:
            traveled = float(np.sum(haversine_m(rlat[:-1], rlon[:-1], rlat[1:], rlon[1:])))

        recent_visits = [v for v in visits if v.arrival_ts >= now - 2700]
        by_pattern: dict[str, list[TripInstance]] = {}
        for inst in active:
            by_pattern.setdefault(inst.pattern_id, []).append(inst)

        out: list[Candidate] = []
        for p in self.gtfs.patterns.values():
            pr = p.shape.project(last.lat, last.lon)
            if abs(pr.cross_m) > self.max_cross_m:
                continue
            along = pr.along_m
            docked_idx = p.stop_index(docked[0]) if docked else None
            if docked_idx is not None:
                along = p.stop_along[docked_idx]

            # 1. geometry. The projection window must reach back as far as the boat has travelled in the
            # recent window, or the oldest fixes snap to the wrong stretch of shape on long straight legs.
            window = max(4000.0, 1.25 * traveled + 300.0)
            alongs, crosses = p.shape.project_many(rlat, rlon, near_along=pr.along_m, window_m=window)
            cost = float(np.mean(np.square(np.minimum(crosses, 300.0)))) / (50.0**2)

            # 2. direction
            if moving and len(recent) >= 3 and traveled > 150:
                if alongs[-1] - alongs[0] < 0.35 * traveled:
                    continue

            # 3. stop consistency
            cost += self._stop_cost(p, along, recent_visits, docked, docked_idx, now)

            # 4. schedule
            trip, sched_cost, late = self._best_trip(p, along, docked_idx, now, by_pattern.get(p.pattern_id, []),
                                                     hint_trip_key)
            cost += sched_cost
            out.append(Candidate(p, trip, along, pr.cross_m, cost, late))
        out.sort(key=lambda c: c.cost)
        return out

    @staticmethod
    def _stop_cost(p: Pattern, along: float, visits: list[Visit], docked: tuple[str, float] | None,
                   docked_idx: int | None, now: float) -> float:
        cost = 0.0
        if docked is not None:
            if docked_idx is None:
                return 6.0  # docked at a landing this pattern never serves
            if docked_idx == len(p.stop_ids) - 1 and now - docked[1] > 90:
                cost += 3.0  # this trip has finished; the boat is waiting for its next one
            return cost
        if not visits:
            return 0.0
        last_visit = visits[-1]
        idx = p.stop_index(last_visit.stop_id)
        if idx is None:
            return 4.0 if now - last_visit.arrival_ts < 1800 else 1.0
        if p.stop_along[idx] > along + 100:
            return 3.0  # we docked at a stop that is still ahead of us on this pattern
        if idx == len(p.stop_ids) - 1:
            return 3.0  # last visit was this pattern's final stop, so we're on a new trip
        visited_after = {v.stop_id for v in visits if v.arrival_ts >= last_visit.arrival_ts}
        for j in range(idx + 1, len(p.stop_ids)):
            if p.stop_along[j] < along - 150 and p.stop_ids[j] not in visited_after:
                cost += 3.0  # sailed past a stop of this pattern without stopping
        return cost

    @staticmethod
    def _best_trip(p: Pattern, along: float, docked_idx: int | None, now: float, trips: list[TripInstance],
                   hint: str | None) -> tuple[TripInstance | None, float, float | None]:
        best: tuple[TripInstance | None, float, float | None] = (None, NO_TRIP_COST, None)
        last_idx = len(p.stop_ids) - 1
        for inst in trips:
            if docked_idx is not None and docked_idx < last_idx:
                late = now - float(inst.dep[docked_idx])
                if docked_idx == 0:
                    # waiting at the terminal: prefer the next departure
                    c = 0.3 + (max(0.0, -late) / 900.0) ** 2 + (max(0.0, late) / 420.0) ** 2
                else:
                    c = ((late - 30.0) / 360.0) ** 2 if late > -600 else 9.0
            elif docked_idx == last_idx:
                late = now - float(inst.arr[last_idx])
                c = ((late - 60.0) / 400.0) ** 2 if late > -600 else 9.0
            else:
                late = now - inst.scheduled_time_at_along(along, p)
                c = ((late - 60.0) / 360.0) ** 2 if late > -420 else 6.0 + ((late + 420) / 300.0) ** 2
            if late > 3000:
                continue
            if hint is not None and inst.key == hint:
                c -= 8.0
            if c < best[1]:
                best = (inst, c, late)
        return best


def softmax_confidence(costs: list[float]) -> float:
    if not costs:
        return 0.0
    c = np.array(costs[:5])
    w = np.exp(-(c - c.min()) / 2.0)
    return float(w[0] / w.sum())


def round_or_none(x: float | None, nd: int = 1) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, nd)
