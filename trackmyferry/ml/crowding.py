"""Crowding: forecast boardings, then the chance a boat is full when it reaches your stop.

Data: NYC Open Data "NYC Ferry Ridership" (hourly boardings by date, hour, route, direction, stop).
It is published with a lag and has no alightings, so this is a *forecast*, not a live count.

1. Boardings model — HistGradientBoosting with Poisson loss on hourly boardings per
   (route, direction, stop, hour, day type, month), with the number of scheduled departures that
   hour as a feature. Dividing by departures gives expected boardings per departure.
2. Load model — people already on board when a boat reaches stop i are the boardings at earlier
   stops, thinned by the chance they have not got off yet. The data has no alightings, so we use
   the return-trip symmetry assumption common in transit OD estimation: where people get *off* in
   the morning peak mirrors where they get *on* in the evening peak (and vice versa); midday and
   weekend travel mirror themselves.
3. Uncertainty — counts are over-dispersed, so boardings are Negative Binomial with a dispersion r
   fitted on training residuals; thinning keeps NB form. The total (on board + wanting to board) is
   moment-matched to a Negative Binomial and P(total > capacity) is the chance someone is left on
   the dock.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy.stats import nbinom, poisson
from sklearn.ensemble import HistGradientBoostingRegressor

from ..gtfs.static import GTFS, TZ, Pattern, TripInstance

FEATURES = ["route_code", "direction_id", "stop_code", "hour", "dow", "weekend", "month", "departures"]
CATEGORICAL = ["route_code", "stop_code"]


DAYPARTS = {"am": range(6, 10), "midday": range(10, 16), "pm": range(16, 20)}
MIRROR = {"am": "pm", "pm": "am", "midday": "midday", "evening": "am", "early": "pm"}


def daypart(hour: int) -> str:
    for name, hours in DAYPARTS.items():
        if hour in hours:
            return name
    return "early" if hour < 6 else "evening"


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def infer_direction_ids(gtfs: GTFS) -> dict[tuple[str, str], int]:
    """Map Open Data direction labels (NB/SB/EB/WB) to GTFS direction_id using pattern geometry."""
    out: dict[tuple[str, str], int] = {}
    for p in gtfs.patterns.values():
        first, last = gtfs.stops[p.stop_ids[0]], gtfs.stops[p.stop_ids[-1]]
        dlat, dlon = last.lat - first.lat, (last.lon - first.lon) * math.cos(math.radians(first.lat))
        if abs(dlat) >= abs(dlon):
            label = "NB" if dlat > 0 else "SB"
        else:
            label = "EB" if dlon > 0 else "WB"
        out.setdefault((p.route_id, label), p.direction_id)
    return out


class CrowdingModel:
    def __init__(self, gtfs: GTFS, capacity: int = 150):
        self.gtfs = gtfs
        self.capacity = capacity
        self.model: HistGradientBoostingRegressor | None = None
        self.route_codes: dict[str, int] = {}
        self.stop_codes: dict[str, int] = {}
        self.dispersion = 10.0
        self.alight_weight: dict[str, float] = {}
        self.daypart_weight: dict[str, dict[str, float]] = {}  # "Weekday|am" -> stop -> share of boardings
        self.capacity_by_route: dict[str, int] = {}
        self._name_to_stop = {_norm(s.name): s.stop_id for s in gtfs.stops.values()}
        self._dir_map = infer_direction_ids(gtfs)
        self._dep_cache: dict[date, dict[tuple, int]] = {}
        self._mu_cache: dict[tuple, float] = {}

    # ------------------------------------------------------------------ data prep
    def departures_per_hour(self, d: date) -> dict[tuple, int]:
        if d not in self._dep_cache:
            counts: dict[tuple, int] = defaultdict(int)
            for inst in self.gtfs.instances_for_date(d):
                for k, sid in enumerate(inst.stop_ids[:-1]):
                    h = datetime.fromtimestamp(float(inst.dep[k]), TZ).hour
                    counts[(inst.route_id, inst.direction_id, sid, h)] += 1
            if len(self._dep_cache) > 60:
                self._dep_cache.clear()
            self._dep_cache[d] = dict(counts)
        return self._dep_cache[d]

    def prepare(self, ridership: pd.DataFrame) -> pd.DataFrame:
        """Open Data rows -> model frame (one row per date/hour/route/direction/stop)."""
        df = ridership.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["hour"] = df["hour"].astype(int)
        df["boardings"] = df["boardings"].astype(float)
        df["stop_id"] = [self._name_to_stop.get(_norm(s)) for s in df["stop"]]
        df["direction_id"] = [self._dir_map.get((r, d)) for r, d in zip(df["route"], df["direction"], strict=True)]
        df = df.dropna(subset=["stop_id", "direction_id"]).rename(columns={"route": "route_id"})
        df["direction_id"] = df["direction_id"].astype(int)
        # A scheduled departure with no row means zero boardings, but only for hours the data covers;
        # an hour with no rows at all is missing data, not an empty ferry system.
        keyed = df.groupby(["date", "hour", "route_id", "direction_id", "stop_id"], as_index=False)["boardings"].sum()
        covered = set(zip(keyed["date"], keyed["hour"], strict=True))
        frames = []
        for d in sorted(keyed["date"].unique()):
            dep = self.departures_per_hour(d)
            sched = pd.DataFrame([(d, h, r, di, s, n) for (r, di, s, h), n in dep.items() if (d, h) in covered],
                                 columns=["date", "hour", "route_id", "direction_id", "stop_id", "departures"])
            frames.append(sched)
        sched = pd.concat(frames, ignore_index=True)
        out = sched.merge(keyed, how="left", on=["date", "hour", "route_id", "direction_id", "stop_id"])
        out["boardings"] = out["boardings"].fillna(0.0)
        return self._time_features(out)

    def _time_features(self, df: pd.DataFrame) -> pd.DataFrame:
        dts = pd.to_datetime(df["date"])
        df["dow"] = dts.dt.weekday
        df["weekend"] = (df["dow"] >= 5).astype(int)
        df["month"] = dts.dt.month
        return df

    def _encode(self, df: pd.DataFrame, grow: bool = False) -> pd.DataFrame:
        for col, codes, src in (("route_code", self.route_codes, "route_id"), ("stop_code", self.stop_codes, "stop_id")):
            if grow:
                for v in df[src].unique():
                    codes.setdefault(v, len(codes))
            df[col] = [codes.get(v, np.nan) for v in df[src]]
        return df[FEATURES].astype(float)

    # ------------------------------------------------------------------ fit
    def fit(self, ridership: pd.DataFrame) -> CrowdingModel:
        df = self.prepare(ridership)
        X = self._encode(df, grow=True)
        cat_idx = [FEATURES.index(c) for c in CATEGORICAL]
        self.model = HistGradientBoostingRegressor(loss="poisson", max_iter=400, learning_rate=0.06,
                                                   max_leaf_nodes=31, min_samples_leaf=30,
                                                   categorical_features=cat_idx, random_state=0)
        self.model.fit(X, df["boardings"])
        mu = self.model.predict(X)
        y = df["boardings"].to_numpy()
        excess = float(np.sum((y - mu) ** 2 - mu))
        self.dispersion = float(np.clip(np.sum(mu**2) / excess, 2.0, 200.0)) if excess > 0 else 200.0
        totals = df.groupby("stop_id")["boardings"].sum()
        self.alight_weight = (totals / totals.sum()).to_dict()
        df["daytype"] = np.where(df["weekend"] == 1, "Weekend", "Weekday")
        df["daypart"] = [daypart(h) for h in df["hour"]]
        self.daypart_weight = {}
        for (dtype, part), g in df.groupby(["daytype", "daypart"]):
            tot = g.groupby("stop_id")["boardings"].sum()
            if tot.sum() > 0:
                self.daypart_weight[f"{dtype}|{part}"] = (tot / tot.sum()).to_dict()
        self._mu_cache.clear()
        return self

    # ------------------------------------------------------------------ predict
    def _hourly_rate(self, route_id: str, direction_id: int, stop_id: str, d: date, hour: int) -> float:
        """Expected boardings per departure in one clock hour (0 if nothing is scheduled)."""
        key = (route_id, direction_id, stop_id, d, hour)
        if key not in self._mu_cache:
            n_dep = self.departures_per_hour(d).get((route_id, direction_id, stop_id, hour), 0)
            if n_dep == 0 or self.model is None:
                self._mu_cache[key] = math.nan
            else:
                row = pd.DataFrame([{"route_id": route_id, "direction_id": direction_id, "stop_id": stop_id,
                                     "hour": hour, "date": d, "departures": n_dep}])
                row = self._time_features(row)
                mu = float(self.model.predict(self._encode(row))[0])
                self._mu_cache[key] = max(mu, 0.0) / n_dep
            if len(self._mu_cache) > 50_000:
                self._mu_cache.clear()
        return self._mu_cache[key]

    def boardings_per_departure(self, route_id: str, direction_id: int, stop_id: str, ts: float) -> float:
        """Hourly rates are interpolated to the minute, so a 08:10 boat sees the 08:00-09:00 peak
        rather than the flat hourly average (rates are anchored at each hour's midpoint)."""
        dt = datetime.fromtimestamp(ts, TZ)
        here = self._hourly_rate(route_id, direction_id, stop_id, dt.date(), dt.hour)
        if math.isnan(here):
            return 0.0
        frac = (dt.minute + dt.second / 60) / 60 - 0.5
        other_hour = dt.hour + (1 if frac > 0 else -1)
        if not 0 <= other_hour <= 23:
            return here
        other = self._hourly_rate(route_id, direction_id, stop_id, dt.date(), other_hour)
        if math.isnan(other):
            return here
        return (1 - abs(frac)) * here + abs(frac) * other

    def alight_weights(self, pattern: Pattern, ts: float) -> list[float]:
        dt = datetime.fromtimestamp(ts, TZ)
        dtype = "Weekend" if dt.weekday() >= 5 else "Weekday"
        part = daypart(dt.hour) if dtype == "Weekday" else "midday"
        table = self.daypart_weight.get(f"{dtype}|{MIRROR[part]}") or self.alight_weight
        return [table.get(s, 0.0) + 1e-3 for s in pattern.stop_ids]

    def load_distribution(self, inst: TripInstance, pattern: Pattern, stop_index: int,
                          eta_ts: float) -> tuple[float, float]:
        """Mean and variance of (people on board after alighting at stop i) + (people wanting to board)."""
        r = self.dispersion
        w = self.alight_weights(pattern, eta_ts)
        offset = eta_ts - float(inst.arr[stop_index]) if stop_index > 0 else eta_ts - float(inst.dep[0])
        mean = var = 0.0
        for k in range(stop_index + 1):
            ts_k = float(inst.dep[k]) + offset
            mu = self.boardings_per_departure(inst.route_id, inst.direction_id, pattern.stop_ids[k], ts_k)
            if k < stop_index:
                denom = sum(w[k + 1:])
                s = sum(w[stop_index + 1:]) / denom if denom > 0 else 0.0
            else:
                s = 1.0
            m = mu * s
            mean += m
            var += m + m * m / r
        return mean, var

    def p_full(self, inst: TripInstance, pattern: Pattern, stop_index: int, eta_ts: float) -> float:
        if self.model is None or stop_index >= len(pattern.stop_ids) - 1:
            return 0.0
        cap = self.capacity_by_route.get(inst.route_id, self.capacity)
        m, v = self.load_distribution(inst, pattern, stop_index, eta_ts)
        if m <= 0:
            return 0.0
        if v <= m * 1.0001:
            return float(poisson.sf(cap, m))
        r_eff = m * m / (v - m)
        return float(nbinom.sf(cap, r_eff, r_eff / (r_eff + m)))

    # ------------------------------------------------------------------ persistence
    def save(self, directory: Path | str) -> None:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.model, d / "crowding.joblib")
        (d / "crowding.json").write_text(json.dumps({
            "capacity": self.capacity, "capacity_by_route": self.capacity_by_route,
            "route_codes": self.route_codes, "stop_codes": self.stop_codes,
            "dispersion": self.dispersion, "alight_weight": self.alight_weight,
            "daypart_weight": self.daypart_weight, "features": FEATURES,
        }, indent=2))

    @classmethod
    def load(cls, directory: Path | str, gtfs: GTFS) -> CrowdingModel:
        d = Path(directory)
        meta = json.loads((d / "crowding.json").read_text())
        m = cls(gtfs, capacity=meta["capacity"])
        m.model = joblib.load(d / "crowding.joblib")
        m.capacity_by_route = meta.get("capacity_by_route", {})
        m.route_codes, m.stop_codes = meta["route_codes"], meta["stop_codes"]
        m.dispersion, m.alight_weight = meta["dispersion"], meta["alight_weight"]
        m.daypart_weight = meta.get("daypart_weight", {})
        return m

    @staticmethod
    def exists(directory: Path | str) -> bool:
        return (Path(directory) / "crowding.joblib").exists()
