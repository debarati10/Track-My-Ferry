"""Build the ETA training table by replaying recorded feeds through the live pipeline.

Every `snapshot_every_s` seconds, for every boat matched to a trip, the pipeline emits one feature
row per downstream stop (the same rows it would score live). The label is the arrival time at that
stop that the *detector* observes later — no simulator ground truth is used, so the procedure works
unchanged on real recorded AIS.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ..gtfs.static import GTFS, TZ
from ..messages import StopEvent, Topics
from ..processing.features import DwellPriors
from ..processing.pipeline import Pipeline
from ..record import replay

MAX_HORIZON_S = 2 * 3600


def build_eta_table(raw_dir: Path | str, gtfs: GTFS, days: list[date] | None = None,
                    snapshot_every_s: float = 30.0, max_ahead: int = 8,
                    progress: bool = True) -> tuple[pd.DataFrame, DwellPriors, dict, pd.DataFrame]:
    """Returns (training rows, learned dwell priors, stats, detected stop events)."""
    rows: list[dict] = []
    events: list[dict] = []
    arrivals: dict[tuple[int, str], list[float]] = defaultdict(list)

    def hook(now, mmsi, a, rws):
        for r in rws:
            r = dict(r)
            r.update(now=now, mmsi=mmsi, trip_key=a.trip_key, route_id=a.pattern.route_id,
                     direction_id=a.pattern.direction_id, match_confidence=a.confidence)
            rows.append(r)

    pipe = Pipeline(gtfs, prediction_interval_s=snapshot_every_s, max_stops_ahead=max_ahead,
                    snapshot_hook=hook, emit_states=False)
    t0 = time.time()
    n = 0
    last_day = None
    for msg in replay(raw_dir, Topics.INGEST, days):
        n += 1
        for out in pipe.handle(msg):
            if isinstance(out, StopEvent):
                if out.departure_ts is None:
                    arrivals[(out.mmsi, out.stop_id)].append(out.arrival_ts)
                else:
                    events.append(out.to_dict())
        if progress and n % 200_000 == 0:
            day = datetime.fromtimestamp(getattr(msg, "ts", 0), TZ).date()
            if day != last_day:
                print(f"  replayed {n:,} messages (at {day}, {time.time() - t0:.0f}s)", flush=True)
                last_day = day

    df = pd.DataFrame(rows)
    ev = pd.DataFrame(events)
    if df.empty:
        return df, pipe.priors, dict(pipe.counters), ev
    df["actual_arrival"] = _label(df, arrivals)
    df = df[df["actual_arrival"].notna()].copy()
    df["y_remaining_s"] = df["actual_arrival"] - df["now"]
    df["service_day"] = pd.to_datetime(df["now"], unit="s", utc=True).dt.tz_convert(TZ).dt.date
    stats = dict(pipe.counters)
    stats.update(rows=len(df), messages=n, replay_s=round(time.time() - t0, 1))
    return df.reset_index(drop=True), pipe.priors, stats, ev


def _label(df: pd.DataFrame, arrivals: dict[tuple[int, str], list[float]]) -> np.ndarray:
    """First detected arrival of the same boat at the target stop after the snapshot."""
    out = np.full(len(df), np.nan)
    for (mmsi, stop_id), idx in df.groupby(["mmsi", "stop_id"]).indices.items():
        arr = np.sort(np.asarray(arrivals.get((mmsi, stop_id), []), dtype=float))
        if arr.size == 0:
            continue
        now = df["now"].to_numpy()[idx]
        pos = np.searchsorted(arr, now + 1.0)
        ok = pos < arr.size
        cand = np.where(ok, arr[np.minimum(pos, arr.size - 1)], np.nan)
        sched = df["sched_remaining_s"].to_numpy()[idx]
        limit = np.minimum(np.maximum(sched, 0) * 2 + 2700, MAX_HORIZON_S)
        cand[(cand - now) > limit] = np.nan
        out[idx] = cand
    return out


def split_by_day(df: pd.DataFrame, test_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    days = sorted(df["service_day"].unique())
    cut = days[-test_days:]
    test = df["service_day"].isin(cut)
    return df[~test].copy(), df[test].copy()
