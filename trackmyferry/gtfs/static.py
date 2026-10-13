"""GTFS static schedule loader.

Turns a GTFS feed (zip file, folder or URL) into:

* stops and routes;
* **patterns**: each unique (route, direction, stop sequence) with its shape and the distance of
  every stop along that shape, which is what the matcher and ETA features work in;
* **trip instances**: trips on concrete service dates with absolute (epoch) times, computed with
  the GTFS "noon minus 12 hours" rule so daylight-saving days are handled correctly.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ..geo import Polyline

TZ = ZoneInfo("America/New_York")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_gtfs_time(value: str) -> int:
    h, m, s = value.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def format_gtfs_time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def service_day_origin(d: date, tz: ZoneInfo = TZ) -> float:
    """Epoch of 'noon minus 12h' on a service day, the zero point for GTFS stop times."""
    return datetime(d.year, d.month, d.day, 12, tzinfo=tz).timestamp() - 12 * 3600


def local_date(ts: float, tz: ZoneInfo = TZ) -> date:
    return datetime.fromtimestamp(ts, tz).date()


@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Route:
    route_id: str
    short_name: str
    long_name: str
    color: str = "75AADB"
    text_color: str = "000000"


@dataclass
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    direction_id: int
    shape_id: str | None
    headsign: str
    stop_ids: tuple[str, ...]
    arr: list[int]  # seconds from service-day origin
    dep: list[int]
    pattern_id: str = ""


@dataclass
class Pattern:
    pattern_id: str
    route_id: str
    direction_id: int
    stop_ids: tuple[str, ...]
    shape: Polyline
    stop_along: list[float]
    headsign: str = ""

    def stop_index(self, stop_id: str) -> int | None:
        try:
            return self.stop_ids.index(stop_id)
        except ValueError:
            return None

    def next_stop_index(self, along_m: float, tolerance_m: float = 60.0) -> int | None:
        """Index of the first stop strictly ahead of a position (None after the last stop)."""
        for i, a in enumerate(self.stop_along):
            if a > along_m + tolerance_m:
                return i
        return None


@dataclass
class TripInstance:
    """A trip on a specific service date, with absolute times."""

    trip_id: str
    service_date: str  # YYYYMMDD
    route_id: str
    direction_id: int
    pattern_id: str
    stop_ids: tuple[str, ...]
    arr: np.ndarray  # epoch seconds
    dep: np.ndarray

    @property
    def start(self) -> float:
        return float(self.dep[0])

    @property
    def end(self) -> float:
        return float(self.arr[-1])

    @property
    def key(self) -> str:
        return f"{self.trip_id}@{self.service_date}"

    def scheduled_along(self, t: float, pattern: Pattern) -> float:
        """Where the schedule says the boat should be (metres along the pattern) at time t."""
        sa = pattern.stop_along
        if t <= self.dep[0]:
            return sa[0]
        for i in range(len(self.stop_ids) - 1):
            if t <= self.arr[i + 1]:
                if t <= self.dep[i]:
                    return sa[i]
                span = max(self.arr[i + 1] - self.dep[i], 1.0)
                return sa[i] + (sa[i + 1] - sa[i]) * (t - self.dep[i]) / span
            if t <= self.dep[i + 1]:
                return sa[i + 1]
        return sa[-1]

    def scheduled_time_at_along(self, along_m: float, pattern: Pattern) -> float:
        """Inverse of `scheduled_along`: when the schedule expects the boat at this point."""
        sa = pattern.stop_along
        if along_m <= sa[0]:
            return float(self.dep[0])
        for i in range(len(sa) - 1):
            if along_m <= sa[i + 1]:
                span = max(sa[i + 1] - sa[i], 1.0)
                return float(self.dep[i] + (self.arr[i + 1] - self.dep[i]) * (along_m - sa[i]) / span)
        return float(self.arr[-1])


class GTFS:
    def __init__(
        self,
        stops: dict[str, Stop],
        routes: dict[str, Route],
        trips: dict[str, Trip],
        shapes: dict[str, list[tuple[float, float]]],
        calendar: dict[str, dict],
        calendar_dates: dict[str, dict[str, int]],
        agency_tz: ZoneInfo = TZ,
    ):
        self.stops = stops
        self.routes = routes
        self.trips = trips
        self.shapes = shapes
        self.calendar = calendar
        self.calendar_dates = calendar_dates
        self.tz = agency_tz
        self.patterns: dict[str, Pattern] = {}
        self._build_patterns()
        self._instance_cache: dict[date, list[TripInstance]] = {}

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, source: str | Path | bytes) -> GTFS:
        files = _read_feed(source)
        tz_name = (files.get("agency.txt") or [{}])[0].get("agency_timezone") or "America/New_York"

        stops = {
            r["stop_id"]: Stop(r["stop_id"], r.get("stop_name", r["stop_id"]), float(r["stop_lat"]), float(r["stop_lon"]))
            for r in files.get("stops.txt", [])
            if r.get("stop_lat") and r.get("stop_lon")
        }
        routes = {
            r["route_id"]: Route(
                r["route_id"],
                r.get("route_short_name") or r["route_id"],
                r.get("route_long_name") or "",
                (r.get("route_color") or "75AADB").upper(),
                (r.get("route_text_color") or "000000").upper(),
            )
            for r in files.get("routes.txt", [])
        }
        st_by_trip: dict[str, list[dict]] = defaultdict(list)
        for r in files.get("stop_times.txt", []):
            st_by_trip[r["trip_id"]].append(r)

        trips: dict[str, Trip] = {}
        for r in files.get("trips.txt", []):
            rows = sorted(st_by_trip.get(r["trip_id"], []), key=lambda x: int(x["stop_sequence"]))
            rows = [x for x in rows if x["stop_id"] in stops]
            if len(rows) < 2:
                continue
            arr, dep = [], []
            for x in rows:
                a = x.get("arrival_time") or x.get("departure_time")
                d = x.get("departure_time") or x.get("arrival_time")
                if not a or not d:  # untimed stop: interpolate later
                    arr.append(-1)
                    dep.append(-1)
                else:
                    arr.append(parse_gtfs_time(a))
                    dep.append(parse_gtfs_time(d))
            arr, dep = _fill_untimed(arr), _fill_untimed(dep)
            trips[r["trip_id"]] = Trip(
                trip_id=r["trip_id"],
                route_id=r["route_id"],
                service_id=r["service_id"],
                direction_id=int(r.get("direction_id") or 0),
                shape_id=r.get("shape_id") or None,
                headsign=r.get("trip_headsign") or "",
                stop_ids=tuple(x["stop_id"] for x in rows),
                arr=arr,
                dep=dep,
            )

        shape_rows: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        for r in files.get("shapes.txt", []):
            shape_rows[r["shape_id"]].append(
                (int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"]))
            )
        shapes = {sid: [(lat, lon) for _, lat, lon in sorted(pts)] for sid, pts in shape_rows.items()}

        calendar = {r["service_id"]: r for r in files.get("calendar.txt", [])}
        calendar_dates: dict[str, dict[str, int]] = defaultdict(dict)
        for r in files.get("calendar_dates.txt", []):
            calendar_dates[r["service_id"]][r["date"]] = int(r["exception_type"])

        return cls(stops, routes, trips, shapes, calendar, dict(calendar_dates), ZoneInfo(tz_name))

    # ------------------------------------------------------------------ patterns
    def _build_patterns(self) -> None:
        groups: dict[tuple[str, int, tuple[str, ...]], list[Trip]] = defaultdict(list)
        for trip in self.trips.values():
            groups[(trip.route_id, trip.direction_id, trip.stop_ids)].append(trip)
        counters: dict[tuple[str, int], int] = defaultdict(int)
        for (route_id, direction_id, stop_ids), trips in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], -len(kv[1]))):
            n = counters[(route_id, direction_id)]
            counters[(route_id, direction_id)] += 1
            pattern_id = f"{route_id}-{direction_id}-{n}"
            shape = self._shape_for(trips, stop_ids)
            stop_along = self._stop_distances(shape, stop_ids)
            self.patterns[pattern_id] = Pattern(
                pattern_id, route_id, direction_id, stop_ids, shape, stop_along, trips[0].headsign
            )
            for t in trips:
                t.pattern_id = pattern_id

    def _shape_for(self, trips: list[Trip], stop_ids: tuple[str, ...]) -> Polyline:
        for t in trips:
            if t.shape_id and t.shape_id in self.shapes and len(self.shapes[t.shape_id]) >= 2:
                return Polyline(self.shapes[t.shape_id])
        return Polyline([(self.stops[s].lat, self.stops[s].lon) for s in stop_ids])

    def _stop_distances(self, shape: Polyline, stop_ids: tuple[str, ...]) -> list[float]:
        out: list[float] = []
        prev = 0.0
        for sid in stop_ids:
            s = self.stops[sid]
            p = shape.project(s.lat, s.lon, min_along=max(prev - 30.0, 0.0))
            out.append(max(p.along_m, prev))
            prev = out[-1]
        return out

    def patterns_for_route(self, route_id: str) -> list[Pattern]:
        return [p for p in self.patterns.values() if p.route_id == route_id]

    # ------------------------------------------------------------------ calendar
    def service_active(self, service_id: str, d: date) -> bool:
        ds = d.strftime("%Y%m%d")
        exc = self.calendar_dates.get(service_id, {}).get(ds)
        if exc == 1:
            return True
        if exc == 2:
            return False
        cal = self.calendar.get(service_id)
        if not cal:
            return False
        if not (cal["start_date"] <= ds <= cal["end_date"]):
            return False
        return cal.get(WEEKDAYS[d.weekday()], "0") == "1"

    def instances_for_date(self, d: date) -> list[TripInstance]:
        if d in self._instance_cache:
            return self._instance_cache[d]
        origin = service_day_origin(d, self.tz)
        ds = d.strftime("%Y%m%d")
        out = [
            TripInstance(
                trip_id=t.trip_id,
                service_date=ds,
                route_id=t.route_id,
                direction_id=t.direction_id,
                pattern_id=t.pattern_id,
                stop_ids=t.stop_ids,
                arr=origin + np.asarray(t.arr, dtype=float),
                dep=origin + np.asarray(t.dep, dtype=float),
            )
            for t in self.trips.values()
            if self.service_active(t.service_id, d)
        ]
        out.sort(key=lambda i: i.start)
        if len(self._instance_cache) > 14:
            self._instance_cache.pop(next(iter(self._instance_cache)))
        self._instance_cache[d] = out
        return out

    def instances_between(self, t0: float, t1: float) -> list[TripInstance]:
        d0 = local_date(t0, self.tz) - timedelta(days=1)
        d1 = local_date(t1, self.tz)
        out: list[TripInstance] = []
        d = d0
        while d <= d1:
            out.extend(i for i in self.instances_for_date(d) if i.end >= t0 and i.start <= t1)
            d += timedelta(days=1)
        return out

    def instance(self, trip_id: str, service_date: str) -> TripInstance | None:
        d = datetime.strptime(service_date, "%Y%m%d").date()
        for inst in self.instances_for_date(d):
            if inst.trip_id == trip_id:
                return inst
        return None

    # ------------------------------------------------------------------ export
    def to_geojson(self) -> dict:
        features = []
        seen_shapes: set[tuple[str, int]] = set()
        for p in self.patterns.values():
            if (p.route_id, p.direction_id) in seen_shapes:
                continue
            seen_shapes.add((p.route_id, p.direction_id))
            r = self.routes.get(p.route_id)
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": p.shape.lonlat_coords()},
                "properties": {
                    "kind": "route", "route_id": p.route_id, "direction_id": p.direction_id,
                    "pattern_id": p.pattern_id, "name": r.long_name if r else p.route_id,
                    "short_name": r.short_name if r else p.route_id, "color": f"#{r.color if r else '75AADB'}",
                },
            })
        routes_by_stop: dict[str, set[str]] = defaultdict(set)
        for p in self.patterns.values():
            for s in p.stop_ids:
                routes_by_stop[s].add(p.route_id)
        for s in self.stops.values():
            if s.stop_id not in routes_by_stop:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s.lon, s.lat]},
                "properties": {"kind": "stop", "stop_id": s.stop_id, "name": s.name,
                               "routes": sorted(routes_by_stop[s.stop_id])},
            })
        return {"type": "FeatureCollection", "features": features}


def _fill_untimed(values: list[int]) -> list[int]:
    """Linearly interpolate stop times that the feed leaves blank."""
    arr = np.asarray(values, dtype=float)
    known = arr >= 0
    if known.all():
        return [int(v) for v in arr]
    idx = np.arange(len(arr))
    arr[~known] = np.interp(idx[~known], idx[known], arr[known])
    return [int(round(v)) for v in arr]


def _read_feed(source: str | Path | bytes) -> dict[str, list[dict]]:
    if isinstance(source, (bytes, bytearray)):
        return _read_zip(io.BytesIO(source))
    s = str(source)
    if s.startswith(("http://", "https://")):
        import requests

        resp = requests.get(s, timeout=60)
        resp.raise_for_status()
        return _read_zip(io.BytesIO(resp.content))
    path = Path(s)
    if path.is_dir():
        return {
            f.name: list(csv.DictReader(io.StringIO(f.read_text(encoding="utf-8-sig"))))
            for f in path.glob("*.txt")
        }
    return _read_zip(path)


def _read_zip(fp) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with zipfile.ZipFile(fp) as zf:
        for name in zf.namelist():
            if name.endswith(".txt"):
                text = zf.read(name).decode("utf-8-sig")
                out[Path(name).name] = list(csv.DictReader(io.StringIO(text)))
    return out


def iter_stop_pairs(pattern: Pattern) -> Iterable[tuple[int, str, str]]:
    for i in range(len(pattern.stop_ids) - 1):
        yield i, pattern.stop_ids[i], pattern.stop_ids[i + 1]
