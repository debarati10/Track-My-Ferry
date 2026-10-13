"""Fleet simulator: synthetic but physically plausible NYC Ferry operations.

What it models
--------------
* Vessels run timetabled trips (chained into daily "blocks", so lateness carries over between
  trips), accelerate/brake into landings and hold at stops when early.
* A semi-diurnal tidal current along the East River (stronger near Hell Gate, weaker in the Upper
  Bay) speeds boats up or slows them down depending on direction. The *true* current has daily
  amplitude/phase errors; the published "NOAA" prediction does not — like real life.
* Wind slows boats a little; rain days cut demand.
* Passengers: time-of-day/day-of-week demand per landing, alightings, a hard capacity (people get
  left behind), and dwell time that grows with boardings. Random incidents (ramp issues, a slow
  boat) add delays.
* AIS: Class-A-like report intervals by speed, GPS noise, 3% dropped messages and short coverage
  gaps per boat per day. Messages are emitted in aisstream.io's JSON format and parsed by the real
  parser.
* GTFS-realtime: every 30 s an "official" TripUpdates feed (schedule + last observed delay, no
  recovery — a common simple approach) is encoded to protobuf and parsed back.

Ground truth (real arrival/departure times, loads, people left behind) is kept separately so the
arrival detector, ETA model and crowding model can be evaluated honestly.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from ..geo import HARBOR, KNOT_MS
from ..gtfs.realtime import encode_trip_updates, parse_trip_updates
from ..gtfs.static import GTFS, Pattern, TripInstance, local_date
from ..gtfs.static import TZ as HARBOR_TZ
from ..ingest.ais import build_position_message, build_static_message, parse_aisstream_message
from ..messages import EnvObservation, Message, StopTimeUpdate, TripUpdate
from .sample_network import ROUTES

MANHATTAN = {"PIER11", "E34", "E90"}
LEISURE = {"DUMBO", "NWBG", "PIER6", "RI", "BAYRIDGE", "PIER11", "GRPT"}
STOP_WEIGHT = {
    "PIER11": 1.0, "E34": 0.9, "DUMBO": 0.75, "NWBG": 0.85, "SWBG": 0.55, "GRPT": 0.65, "HPS": 0.6,
    "BNY": 0.35, "LIC": 0.55, "RI": 0.4, "AST": 0.65, "E90": 0.55, "PIER6": 0.5, "REDHOOK": 0.35,
    "BAT": 0.5, "BAYRIDGE": 0.75,
}
ROUTE_BASE = {"ER": 21.0, "AS": 15.0, "SB": 14.0}
RIVER_AXIS_DEG = 32.0  # flood current runs roughly NNE up the East River
HARBOR_AXIS_DEG = 20.0
M2_PERIOD_S = 12.42 * 3600
TIDE_EPOCH = 1_790_000_000.0
ACCEL = 0.35  # m/s^2
DECEL = 0.30


@dataclass
class SimConfig:
    seed: int = 7
    capacity: int = 150
    include_vehicle_in_rt: bool = False
    ais_drop_rate: float = 0.03
    gps_noise_m: float = 4.0
    gaps_per_vessel_day: float = 1.0
    incident_prob: float = 0.03
    slow_trip_prob: float = 0.05
    rt_interval_s: int = 30
    env_interval_s: int = 600
    static_interval_s: int = 360
    min_layover_s: int = 180
    plan_layover_s: int = 420


class Environment:
    """Ground-truth harbour conditions plus the forecasts a real system would see."""

    def __init__(self, seed: int):
        self.seed = seed
        self._days: dict[date, dict] = {}

    def day(self, ts: float) -> dict:
        d = local_date(ts)
        if d not in self._days:
            rng = np.random.default_rng([self.seed, d.toordinal()])
            self._days[d] = {
                "amp": rng.uniform(1.8, 3.1),
                "jitter": rng.normal(0, 900),
                "wind_mean": rng.uniform(5, 19),
                "wind_phase": rng.uniform(0, 2 * math.pi),
                "wind_dir": rng.uniform(0, 360),
                "rain": bool(rng.random() < 0.22),
                "demand": float(rng.lognormal(0, 0.12)),
            }
        return self._days[d]

    def current_kn(self, ts: float) -> float:
        p = self.day(ts)
        return p["amp"] * math.sin(2 * math.pi * (ts - TIDE_EPOCH - p["jitter"]) / M2_PERIOD_S)

    @staticmethod
    def predicted_current_kn(ts: float) -> float:
        return 2.2 * math.sin(2 * math.pi * (ts - TIDE_EPOCH) / M2_PERIOD_S)

    def wind_kn(self, ts: float) -> float:
        p = self.day(ts)
        return max(0.0, p["wind_mean"] * (1 + 0.35 * math.sin(2 * math.pi * ts / (5.3 * 3600) + p["wind_phase"])))

    @staticmethod
    def region(lat: float) -> tuple[float, float]:
        if lat >= 40.698:
            return (1.4 if lat > 40.765 else 1.0), RIVER_AXIS_DEG
        return 0.45, HARBOR_AXIS_DEG

    def current_effect_kn(self, lat: float, seg_bearing: float, ts: float) -> float:
        scale, axis = self.region(lat)
        return self.current_kn(ts) * scale * math.cos(math.radians(seg_bearing - axis))


def local_hour(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, HARBOR_TZ)
    return dt.hour + dt.minute / 60



def boarding_rate(route_id: str, stop_id: str, ts: float, weekend: bool, rain: bool, day_factor: float) -> float:
    h = local_hour(ts)
    w = STOP_WEIGHT.get(stop_id, 0.5)
    base = ROUTE_BASE.get(route_id, 14.0)
    if weekend:
        prof = 0.5 + 1.9 * math.exp(-(((h - 14.0) / 3.2) ** 2))
        if stop_id in LEISURE:
            prof *= 1.35
    else:
        am = math.exp(-(((h - 8.2) / 1.0) ** 2))
        pm = math.exp(-(((h - 18.0) / 1.2) ** 2))
        prof = 0.55 + 0.6 * am + 2.6 * pm if stop_id in MANHATTAN else 0.55 + 2.6 * am + 0.6 * pm
    if rain:
        prof *= 0.65
    return base * w * prof * day_factor


def alight_weight(stop_id: str, ts: float, weekend: bool) -> float:
    if weekend:
        return 1.0 + (0.8 if stop_id in LEISURE else 0.0)
    h = local_hour(ts)
    am = math.exp(-(((h - 8.2) / 1.2) ** 2))
    pm = math.exp(-(((h - 18.0) / 1.4) ** 2))
    return 1.0 + 3.0 * am if stop_id in MANHATTAN else 1.0 + 2.5 * pm


@dataclass
class VesselSim:
    mmsi: int
    name: str
    route_id: str
    cruise_kn: float
    block: list[TripInstance] = field(default_factory=list)
    trip_idx: int = -1
    state: str = "off"  # off | layover | dwell | underway
    trip: TripInstance | None = None
    pattern: Pattern | None = None
    k: int = 0
    along: float = 0.0
    v: float = 0.0  # speed through water, m/s
    vg: float = 0.0  # speed over ground, m/s
    lat: float = 0.0
    lon: float = 0.0
    cog: float = 0.0
    load: int = 0
    arr_ts: float | None = None
    depart_after: float = 0.0
    ready_ts: float = 0.0
    slow_factor: float = 1.0
    off_since: float = 0.0
    next_report: float = 0.0
    next_static: float = 0.0
    gaps: list[tuple[float, float]] = field(default_factory=list)
    actual_arr: dict[int, float] = field(default_factory=dict)
    actual_dep: dict[int, float] = field(default_factory=dict)
    pending: dict | None = None


class FleetSim:
    def __init__(self, gtfs: GTFS, config: SimConfig | None = None):
        self.gtfs = gtfs
        self.cfg = config or SimConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        self.env = Environment(self.cfg.seed)
        self.fleet: dict[str, list[VesselSim]] = {}
        self.truth: list[dict] = []
        self.ridership: dict[tuple, int] = {}
        self.clock: float = 0.0
        self.planned: date | None = None
        self.day_start = 0.0
        self.day_end = 0.0

    # ------------------------------------------------------------------ planning
    def plan_day(self, d: date) -> None:
        instances = self.gtfs.instances_for_date(d)
        by_route: dict[str, list[TripInstance]] = {}
        for inst in instances:
            by_route.setdefault(inst.route_id, []).append(inst)
        for route_id, trips in by_route.items():
            blocks: list[list[TripInstance]] = []
            for trip in sorted(trips, key=lambda i: i.start):
                best, best_slack = None, None
                for b in blocks:
                    last = b[-1]
                    slack = trip.start - (last.end + self.cfg.plan_layover_s)
                    if last.stop_ids[-1] == trip.stop_ids[0] and slack >= 0 and (best is None or slack < best_slack):
                        best, best_slack = b, slack
                if best is None:
                    blocks.append([trip])
                else:
                    best.append(trip)
            fleet = self.fleet.setdefault(route_id, [])
            route_idx = list(ROUTES).index(route_id) if route_id in ROUTES else len(self.fleet)
            while len(fleet) < len(blocks):
                i = len(fleet)
                fleet.append(VesselSim(
                    mmsi=366_900_000 + route_idx * 100 + i,
                    name=f"SIM {ROUTES.get(route_id, (route_id,))[0]} {i + 1:02d}",
                    route_id=route_id,
                    cruise_kn=float(self.rng.normal(21.3, 1.0)),
                ))
            day_rng = np.random.default_rng([self.cfg.seed, d.toordinal(), 99])
            for vs, block in zip(fleet, blocks + [[]] * (len(fleet) - len(blocks)), strict=False):
                vs.block, vs.trip_idx, vs.state, vs.trip, vs.pattern = block, -1, "off", None, None
                vs.gaps = []
                if block:
                    for _ in range(day_rng.poisson(self.cfg.gaps_per_vessel_day)):
                        g0 = day_rng.uniform(block[0].start, block[-1].end)
                        vs.gaps.append((g0, g0 + day_rng.uniform(120, 420)))
        starts = [i.start for i in instances]
        ends = [i.end for i in instances]
        self.day_start = min(starts) - 1800 if starts else 0.0
        self.day_end = max(ends) + 900 if ends else 0.0
        self.planned = d

    def all_vessels(self) -> list[VesselSim]:
        return [v for fleet in self.fleet.values() for v in fleet]

    # ------------------------------------------------------------------ vessel lifecycle
    def _start_trip(self, vs: VesselSim, idx: int, t: float, ready_ts: float) -> None:
        vs.trip_idx = idx
        vs.trip = vs.block[idx]
        vs.pattern = self.gtfs.patterns[vs.trip.pattern_id]
        vs.k, vs.along, vs.v, vs.vg = 0, vs.pattern.stop_along[0], 0.0, 0.0
        vs.state = "layover"
        vs.ready_ts = ready_ts
        vs.slow_factor = 0.82 if self.rng.random() < self.cfg.slow_trip_prob else 1.0
        vs.load = 0
        vs.actual_arr, vs.actual_dep = {}, {}
        vs.arr_ts = None
        vs.pending = None
        vs.lat, vs.lon = vs.pattern.shape.point_at(vs.along)

    def _weekend(self, ts: float) -> bool:
        return local_date(ts).weekday() >= 5

    def _board(self, vs: VesselSim, stop_id: str, ts: float) -> tuple[int, int]:
        day = self.env.day(ts)
        lam = boarding_rate(vs.route_id, stop_id, ts, self._weekend(ts), day["rain"], day["demand"])
        want = int(self.rng.poisson(lam))
        space = max(self.cfg.capacity - vs.load, 0)
        board = min(want, space)
        vs.load += board
        return board, want - board

    def _alight(self, vs: VesselSim, k: int, ts: float) -> int:
        stops = vs.pattern.stop_ids
        if k == len(stops) - 1:
            n = vs.load
        else:
            weekend = self._weekend(ts)
            w = [alight_weight(s, ts, weekend) for s in stops[k:]]
            p = w[0] / sum(w)
            n = int(self.rng.binomial(vs.load, p)) if vs.load > 0 else 0
        vs.load -= n
        return n

    def _depart(self, vs: VesselSim, t: float) -> None:
        k = vs.k
        stop_id = vs.trip.stop_ids[k]
        if k == 0:
            board, left = self._board(vs, stop_id, t)
            alight = 0
        else:
            board, left, alight = vs.pending["board"], vs.pending["left"], vs.pending["alight"]
        vs.actual_dep[k] = t
        self._record(vs, k, vs.arr_ts if k > 0 else None, t, board, alight, left)
        vs.state, vs.v = "underway", 0.0

    def _record(self, vs, k, arr, dep, board, alight, left) -> None:
        trip = vs.trip
        stop_id = trip.stop_ids[k]
        self.truth.append({
            "mmsi": vs.mmsi, "trip_id": trip.trip_id, "service_date": trip.service_date,
            "route_id": trip.route_id, "direction_id": trip.direction_id, "stop_id": stop_id,
            "stop_index": k, "arrival_ts": arr, "departure_ts": dep,
            "sched_arrival_ts": float(trip.arr[k]), "sched_departure_ts": float(trip.dep[k]),
            "board": board, "alight": alight, "load_departing": vs.load, "left_behind": left,
            "capacity": self.cfg.capacity,
        })
        if board and dep is not None:
            dt = datetime.fromtimestamp(dep, HARBOR_TZ)
            dir_name = ROUTES.get(trip.route_id, ("", "", "", ("0", "1")))[3][trip.direction_id]
            stop_name = self.gtfs.stops[stop_id].name
            key = (dt.strftime("%Y-%m-%d"), dt.hour, trip.route_id, dir_name, stop_name,
                   "Weekend" if dt.weekday() >= 5 else "Weekday")
            self.ridership[key] = self.ridership.get(key, 0) + board

    def _arrive(self, vs: VesselSim, t: float) -> None:
        vs.k += 1
        k = vs.k
        vs.along = vs.pattern.stop_along[k]
        vs.v = vs.vg = 0.0
        vs.arr_ts = t
        vs.actual_arr[k] = t
        last = len(vs.trip.stop_ids) - 1
        if k == last:
            alight = self._alight(vs, k, t)
            self._record(vs, k, t, None, 0, alight, 0)
            nxt = vs.trip_idx + 1
            if nxt < len(vs.block) and vs.block[nxt].stop_ids[0] == vs.trip.stop_ids[-1]:
                self._start_trip(vs, nxt, t, t + self.cfg.min_layover_s)
            else:
                vs.state, vs.off_since = "off", t
            return
        alight = self._alight(vs, k, t)
        board, left = self._board(vs, vs.trip.stop_ids[k], t)
        dwell = 30 + 1.6 * (board + alight) ** 0.9
        if self.rng.random() < self.cfg.incident_prob:
            dwell += self.rng.uniform(90, 420)
        vs.pending = {"board": board, "alight": alight, "left": left}
        vs.depart_after = max(float(vs.trip.dep[k]), t + dwell)
        vs.state = "dwell"

    def _move(self, vs: VesselSim, t: float, dt: float) -> None:
        p = vs.pattern
        target = p.stop_along[vs.k + 1]
        dist = target - vs.along
        seg_bearing = p.shape.bearing_at(vs.along)
        cur = self.env.current_effect_kn(vs.lat, seg_bearing, t) * KNOT_MS
        wind = self.env.wind_kn(t)
        v_cruise = vs.cruise_kn * KNOT_MS * (1 - 0.006 * max(0.0, wind - 12)) * vs.slow_factor
        vg = max(vs.v + cur, 0.4)
        if dist <= vg * vg / (2 * DECEL) + 8:
            vs.v = max(vs.v - DECEL * dt, 0.9)
        elif vs.v < v_cruise:
            vs.v = min(vs.v + ACCEL * dt, v_cruise)
        else:
            vs.v = max(vs.v - DECEL * dt, v_cruise)
        vs.vg = max(vs.v + cur, 0.4)
        vs.along += vs.vg * dt
        vs.cog = seg_bearing
        if vs.along >= target - 2:
            self._arrive(vs, t)  # may switch to the next trip's pattern
        vs.lat, vs.lon = vs.pattern.shape.point_at(vs.along)

    def _step_vessel(self, vs: VesselSim, t: float, dt: float, out: list[Message]) -> None:
        if vs.state == "off":
            nxt = vs.trip_idx + 1
            if nxt < len(vs.block) and t >= vs.block[nxt].start - 1500:
                self._start_trip(vs, nxt, t, t)
            elif t - vs.off_since > 600 or not vs.block:
                return
        elif vs.state == "layover":
            if t >= vs.trip.dep[0] and t >= vs.ready_ts:
                self._depart(vs, t)
        elif vs.state == "dwell":
            if t >= vs.depart_after:
                self._depart(vs, t)
        elif vs.state == "underway":
            self._move(vs, t, dt)
        self._report(vs, t, out)

    # ------------------------------------------------------------------ emitters
    def _report(self, vs: VesselSim, t: float, out: list[Message]) -> None:
        if t >= vs.next_static and vs.block:
            vs.next_static = t + self.cfg.static_interval_s
            out.append(parse_aisstream_message(build_static_message(vs.mmsi, vs.name, t)))
        if t < vs.next_report or vs.pattern is None:
            return
        sog_kn = vs.vg / KNOT_MS if vs.state == "underway" else 0.0
        interval = 10.0 if sog_kn < 14 else (6.0 if sog_kn < 23 else 2.0)
        vs.next_report = t + interval + self.rng.uniform(-0.5, 0.5)
        if self.rng.random() < self.cfg.ais_drop_rate or any(a <= t <= b for a, b in vs.gaps):
            return
        x, y = HARBOR.to_xy(vs.lat, vs.lon)
        nx, ny = self.rng.normal(0, self.cfg.gps_noise_m, 2)
        lat, lon = HARBOR.to_latlon(x + nx, y + ny)
        cog = (vs.cog + self.rng.normal(0, 2)) % 360
        raw = build_position_message(
            vs.mmsi, vs.name, t, round(float(lat), 6), round(float(lon), 6),
            max(0.0, sog_kn + self.rng.normal(0, 0.15)) if sog_kn > 0 else 0.0,
            cog, cog + self.rng.normal(0, 2), nav_status=0,
        )
        out.append(parse_aisstream_message(raw))

    def official_updates(self, t: float) -> list[TripUpdate]:
        """The operator's own predictions: schedule + last observed delay, no recovery."""
        ups: list[TripUpdate] = []
        for vs in self.all_vessels():
            if vs.trip is None or vs.state == "off":
                continue
            trip, k = vs.trip, vs.k
            if vs.state == "layover":
                delay, first = max(0.0, t + 30 - trip.dep[0]), 1
            elif vs.state == "dwell":
                delay, first = max(0.0, max(float(trip.dep[k]), t + 30) - trip.dep[k]), k + 1
            else:
                delay, first = max(0.0, vs.actual_dep.get(k, t) - trip.dep[k]), k + 1
            stus = []
            if vs.state == "layover":
                stus.append(StopTimeUpdate(trip.stop_ids[0], 1, None, float(trip.dep[0] + delay), None, int(delay)))
            for j in range(first, len(trip.stop_ids)):
                stus.append(StopTimeUpdate(
                    trip.stop_ids[j], j + 1, float(trip.arr[j] + delay), float(trip.dep[j] + delay),
                    int(delay), int(delay),
                ))
            ups.append(TripUpdate(
                ts=t, trip_id=trip.trip_id, route_id=trip.route_id, direction_id=trip.direction_id,
                start_date=trip.service_date,
                vehicle_id=str(vs.mmsi) if self.cfg.include_vehicle_in_rt else None,
                vehicle_label=vs.name if self.cfg.include_vehicle_in_rt else None,
                stop_updates=stus,
            ))
        return ups

    def env_observations(self, t: float) -> list[EnvObservation]:
        p = self.env.day(t)
        return [
            EnvObservation(t, "current", {"speed_kn": round(self.env.predicted_current_kn(t), 3),
                                          "axis_deg": RIVER_AXIS_DEG}, source="sim-noaa"),
            EnvObservation(t, "wind", {"speed_kn": round(self.env.wind_kn(t) + self.rng.normal(0, 1.0), 2),
                                       "dir_deg": p["wind_dir"]}, source="sim-weather"),
            EnvObservation(t, "weather", {"precip_mm": 1.2 if p["rain"] else 0.0}, source="sim-weather"),
        ]

    # ------------------------------------------------------------------ clock
    def warm_start(self, t: float) -> None:
        """Jump into the middle of a service day: put every boat where the timetable says it is."""
        t = float(int(t))
        self._ensure_plan(t)
        self.clock = t
        for vs in self.all_vessels():
            vs.next_report = t + float(self.rng.uniform(0, 6))
            idx = next((i for i, tr in enumerate(vs.block) if tr.end > t), None)
            if idx is None or t < vs.block[idx].start:
                vs.trip_idx = (len(vs.block) if idx is None else idx) - 1
                vs.state, vs.off_since = "off", t - 1e4
                continue
            self._start_trip(vs, idx, t, t)
            trip, p = vs.trip, vs.pattern
            if t < trip.dep[0]:
                continue  # waiting at the first landing
            along = trip.scheduled_along(t, p)
            k = max(i for i, a in enumerate(p.stop_along[:-1]) if a <= along + 1.0)
            vs.k, vs.load = k, int(self.rng.integers(10, 60))
            if trip.arr[k] <= t < trip.dep[k] and k > 0:
                vs.state, vs.along, vs.arr_ts = "dwell", p.stop_along[k], float(trip.arr[k])
                vs.pending = {"board": 0, "alight": 0, "left": 0}
                vs.depart_after = float(trip.dep[k])
            else:
                vs.state, vs.along = "underway", along
                vs.v = vs.cruise_kn * KNOT_MS
                vs.actual_dep[k] = float(trip.dep[k])
            vs.lat, vs.lon = p.shape.point_at(vs.along)

    def _ensure_plan(self, t: float) -> None:
        d = local_date(t - 3 * 3600)
        if d != self.planned:
            self.plan_day(d)

    def step(self, t: float, dt: float = 1.0) -> list[Message]:
        self._ensure_plan(t)
        out: list[Message] = []
        for vs in self.all_vessels():
            self._step_vessel(vs, t, dt, out)
        it = int(t)
        if it % self.cfg.rt_interval_s == 0:
            ups = self.official_updates(t)
            if ups:
                _, parsed = parse_trip_updates(encode_trip_updates(ups, t))
                out.extend(parsed)
        if it % self.cfg.env_interval_s == 0:
            out.extend(self.env_observations(t))
        return out

    def advance_to(self, target: float) -> list[Message]:
        out: list[Message] = []
        while self.clock < target:
            out.extend(self.step(self.clock))
            self.clock += 1.0
        return out

    def iter_days(self, start: date, days: int) -> Iterator[Message]:
        """Run whole service days back to back (skipping the idle night), in time order."""
        for n in range(days):
            d = start + timedelta(days=n)
            self.plan_day(d)
            t = float(int(self.day_start))
            while t <= self.day_end:
                yield from self.step(t)
                t += 1.0

    # ------------------------------------------------------------------ outputs
    def ridership_rows(self) -> list[dict]:
        """Hourly boardings in the same shape as NYC Open Data's NYC Ferry Ridership dataset."""
        return [
            {"date": f"{d}T00:00:00.000", "hour": str(h), "route": r, "direction": dn, "stop": s,
             "boardings": str(b), "typeday": td}
            for (d, h, r, dn, s, td), b in sorted(self.ridership.items())
        ]


async def run_realtime(sim: FleetSim, publish, speedup: float = 1.0, start_ts: float | None = None,
                       tick_s: float = 0.25) -> None:
    """Drive the simulator against the wall clock and hand each message to `publish(msg)`."""
    wall0 = time.time()
    sim_t0 = float(int(start_ts if start_ts is not None else wall0))
    sim.warm_start(sim_t0)
    while True:
        target = sim_t0 + (time.time() - wall0) * speedup
        for msg in sim.advance_to(target):
            await publish(msg)
        await asyncio.sleep(tick_s)


def default_demo_start(now: float | None = None) -> float:
    """Start the live demo at the current time of day, or at 08:30 if the ferries aren't running."""
    now = now or time.time()
    dt = datetime.fromtimestamp(now, HARBOR_TZ)
    if 7 <= dt.hour < 21:
        return now
    return datetime(dt.year, dt.month, dt.day, 8, 30, tzinfo=HARBOR_TZ).timestamp()
