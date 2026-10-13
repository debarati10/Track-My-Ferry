"""Features for the arrival-time (ETA) model.

One row = (vessel on a trip, at time `now`) x (a downstream stop). The target is the number of
seconds until the boat actually arrives at that stop.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..geo import KNOT_MS
from ..gtfs.static import TZ, Pattern, TripInstance

FEATURES = [
    "stops_ahead", "dist_m", "sog_kn", "sog_mean_2m", "docked", "dwell_elapsed_s",
    "sched_remaining_s", "sched_dep_wait_s", "delay_s", "official_remaining_s",
    "hour_sin", "hour_cos", "dow", "weekend",
    "current_assist_kn", "wind_kn", "precip_mm",
    "intermediate_dwell_prior_s", "vessel_dwell_excess_s", "free_run_s", "naive_remaining_s",
    "route_code", "stop_code",
]
CATEGORICAL = ["route_code", "stop_code"]
REFERENCE_SPEED_KN = 20.0
LEG_OVERHEAD_S = 40.0  # accelerate out of / brake into a landing
RIVER_AXIS_DEG = 32.0
HARBOR_AXIS_DEG = 20.0


def harbor_region(lat: float) -> tuple[float, float]:
    """(relative current strength, axis bearing) for a latitude: East River vs Upper Bay."""
    if lat >= 40.698:
        return (1.4 if lat > 40.765 else 1.0), RIVER_AXIS_DEG
    return 0.45, HARBOR_AXIS_DEG


_ASSIST_CACHE: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def _assist_profile(p: Pattern, step_m: float = 100.0) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative integral of (current strength x alignment with the current axis) along a shape."""
    key = id(p.shape)
    if key not in _ASSIST_CACHE:
        grid = np.arange(0.0, p.shape.length + step_m, step_m)
        vals = np.empty(len(grid))
        for i, a in enumerate(grid):
            lat, _ = p.shape.point_at(a)
            scale, axis = harbor_region(lat)
            vals[i] = scale * math.cos(math.radians(p.shape.bearing_at(a) - axis))
        cum = np.r_[0.0, np.cumsum((vals[1:] + vals[:-1]) / 2 * np.diff(grid))]
        _ASSIST_CACHE[key] = (grid, cum)
    return _ASSIST_CACHE[key]


def current_assist_kn(p: Pattern, a0: float, a1: float, current_kn: float) -> float:
    """Average along-track help (+) or hindrance (-) from the tidal current over [a0, a1]."""
    grid, cum = _assist_profile(p)
    if a1 - a0 < 1.0:
        a1 = a0 + 1.0
    f0, f1 = np.interp([a0, a1], grid, cum)
    return float(current_kn * (f1 - f0) / (a1 - a0))


@dataclass
class DwellPriors:
    """Running mean dwell time per (stop, hour of day), learned online from detected stop events."""

    alpha: float = 0.05
    default_s: float = 60.0
    table: dict[str, float] = field(default_factory=dict)
    by_stop: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def _key(stop_id: str, hour: int) -> str:
        return f"{stop_id}|{hour}"

    def update(self, stop_id: str, ts: float, dwell_s: float) -> None:
        dwell_s = min(max(dwell_s, 10.0), 900.0)
        hour = datetime.fromtimestamp(ts, TZ).hour
        k = self._key(stop_id, hour)
        self.table[k] = dwell_s if k not in self.table else (1 - self.alpha) * self.table[k] + self.alpha * dwell_s
        s = self.by_stop.get(stop_id)
        self.by_stop[stop_id] = dwell_s if s is None else (1 - self.alpha) * s + self.alpha * dwell_s

    def get(self, stop_id: str, ts: float) -> float:
        hour = datetime.fromtimestamp(ts, TZ).hour
        return self.table.get(self._key(stop_id, hour), self.by_stop.get(stop_id, self.default_s))

    def to_dict(self) -> dict:
        return {"table": self.table, "by_stop": self.by_stop, "default_s": self.default_s}

    @classmethod
    def from_dict(cls, d: dict) -> DwellPriors:
        return cls(table=dict(d.get("table", {})), by_stop=dict(d.get("by_stop", {})),
                   default_s=float(d.get("default_s", 60.0)))


@dataclass
class Vocab:
    """Stable integer codes for categorical features (saved with the model)."""

    routes: dict[str, int] = field(default_factory=dict)
    stops: dict[str, int] = field(default_factory=dict)

    def route(self, route_id: str, grow: bool = False) -> float:
        if route_id not in self.routes and grow:
            self.routes[route_id] = len(self.routes)
        return float(self.routes[route_id]) if route_id in self.routes else math.nan

    def stop(self, stop_id: str, grow: bool = False) -> float:
        if stop_id not in self.stops and grow:
            self.stops[stop_id] = len(self.stops)
        return float(self.stops[stop_id]) if stop_id in self.stops else math.nan


@dataclass
class EnvState:
    current_kn: float = 0.0
    wind_kn: float = 10.0
    precip_mm: float = 0.0
    updated_ts: float = 0.0


def progress_indices(p: Pattern, along: float, docked_idx: int | None) -> tuple[int | None, int | None]:
    """(index of the stop the boat is docked at or None, index of the next stop to reach)."""
    if docked_idx is not None:
        return docked_idx, docked_idx + 1 if docked_idx + 1 < len(p.stop_ids) else None
    return None, p.next_stop_index(along, tolerance_m=30.0)


def build_rows(
    now: float,
    p: Pattern,
    inst: TripInstance,
    along: float,
    docked_idx: int | None,
    dwell_elapsed_s: float,
    sog_kn: float,
    sog_mean_2m: float,
    env: EnvState,
    priors: DwellPriors,
    vessel_dwell_excess_s: float,
    official: dict[str, float] | None,
    vocab: Vocab,
    max_ahead: int = 8,
    grow_vocab: bool = False,
) -> list[dict]:
    cur_idx, next_idx = progress_indices(p, along, docked_idx)
    if next_idx is None:
        return []
    dt = datetime.fromtimestamp(now, TZ)
    hour = dt.hour + dt.minute / 60.0
    if cur_idx is not None:
        delay = now - float(inst.dep[cur_idx])
        dep_wait = max(0.0, float(inst.dep[cur_idx]) - now)
    else:
        delay = now - inst.scheduled_time_at_along(along, p)
        dep_wait = 0.0
    route_code = vocab.route(p.route_id, grow_vocab)
    rows = []
    inter_prior = 0.0
    for j in range(next_idx, min(len(p.stop_ids), next_idx + max_ahead)):
        stop_id = p.stop_ids[j]
        dist = max(0.0, p.stop_along[j] - along)
        legs = j - cur_idx if cur_idx is not None else j - next_idx + 1
        free_run = dist / (REFERENCE_SPEED_KN * KNOT_MS) + LEG_OVERHEAD_S * legs
        off = official.get(stop_id) if official else None
        rows.append({
            "stop_id": stop_id,
            "stop_index": j,
            "scheduled_eta": float(inst.arr[j]),
            "official_eta": off,
            "stops_ahead": float(j - next_idx + 1),
            "dist_m": dist,
            "sog_kn": sog_kn,
            "sog_mean_2m": sog_mean_2m,
            "docked": 1.0 if cur_idx is not None else 0.0,
            "dwell_elapsed_s": dwell_elapsed_s if cur_idx is not None else 0.0,
            "sched_remaining_s": float(inst.arr[j]) - now,
            "sched_dep_wait_s": dep_wait,
            "delay_s": delay,
            "official_remaining_s": (off - now) if off is not None else math.nan,
            "hour_sin": math.sin(2 * math.pi * hour / 24),
            "hour_cos": math.cos(2 * math.pi * hour / 24),
            "dow": float(dt.weekday()),
            "weekend": 1.0 if dt.weekday() >= 5 else 0.0,
            "current_assist_kn": current_assist_kn(p, along, p.stop_along[j], env.current_kn),
            "wind_kn": env.wind_kn,
            "precip_mm": env.precip_mm,
            "intermediate_dwell_prior_s": inter_prior,
            "vessel_dwell_excess_s": vessel_dwell_excess_s,
            "free_run_s": free_run,
            "naive_remaining_s": dep_wait + free_run + inter_prior,
            "route_code": route_code,
            "stop_code": vocab.stop(stop_id, grow_vocab),
        })
        inter_prior += priors.get(stop_id, now)
    return rows


class VesselDwellHistory:
    """How much longer than usual this boat has been dwelling lately (a proxy for crowding/crew)."""

    def __init__(self, keep: int = 4):
        self.keep = keep
        self._d: dict[int, list[float]] = defaultdict(list)

    def add(self, mmsi: int, excess_s: float) -> None:
        lst = self._d[mmsi]
        lst.append(excess_s)
        del lst[:-self.keep]

    def excess(self, mmsi: int) -> float:
        lst = self._d.get(mmsi)
        return float(sum(lst) / len(lst)) if lst else 0.0
