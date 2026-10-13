"""End-to-end training: replay recorded data -> features/labels -> models -> evaluation report.

    trackmyferry train --data data/sim --models models --test-days 5

Works the same on simulated data and on data captured by `trackmyferry record`. Ground-truth checks
(detector accuracy, trip matching, crowding outcomes) run only when `<data>/truth/` exists.
"""
from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from ..gtfs.static import GTFS
from ..messages import Topics, VesselState
from ..processing.pipeline import Pipeline
from ..record import replay
from .crowding import CrowdingModel
from .dataset import build_eta_table, split_by_day
from .eta_model import QuantileEtaModel
from .evaluate import (
    crowding_metrics,
    detector_metrics,
    eta_metrics,
    plot_calibration,
    plot_eta,
    save_json,
    write_report,
)


def load_truth(data_dir: Path) -> pd.DataFrame | None:
    path = data_dir / "truth" / "stop_visits.jsonl.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt") as fh:
        return pd.DataFrame([json.loads(line) for line in fh])


def matching_accuracy(raw_dir: Path, gtfs: GTFS, truth: pd.DataFrame, day: date) -> float:
    spans: dict[int, list[tuple[float, float, str]]] = defaultdict(list)
    for (mmsi, trip_id, _service_date), g in truth.groupby(["mmsi", "trip_id", "service_date"]):
        start = g.loc[g["stop_index"] == 0, "departure_ts"].min()
        spans[int(mmsi)].append((float(start), float(g["arrival_ts"].max()), trip_id))
    pipe = Pipeline(gtfs, prediction_interval_s=1e9)
    ok = total = 0
    for msg in replay(raw_dir, Topics.INGEST, [day]):
        for out in pipe.handle(msg):
            if isinstance(out, VesselState) and out.status == "underway":
                hit = [s for s in spans.get(out.mmsi, []) if s[0] <= out.ts <= s[1]]
                if hit:
                    total += 1
                    ok += int(out.trip_id == hit[0][2])
    return ok / total if total else float("nan")


def evaluate_crowding(crowd: CrowdingModel, gtfs: GTFS, truth: pd.DataFrame, test_days: set) -> tuple[np.ndarray, np.ndarray]:
    rows = truth[truth["departure_ts"].notna()].copy()
    rows["day"] = pd.to_datetime(rows["service_date"], format="%Y%m%d").dt.date
    rows = rows[rows["day"].isin(test_days)]
    probs, outcome = [], []
    for r in rows.itertuples():
        inst = gtfs.instance(r.trip_id, r.service_date)
        if inst is None:
            continue
        p = gtfs.patterns[inst.pattern_id]
        if r.stop_index >= len(p.stop_ids) - 1:
            continue
        eta = r.arrival_ts if r.arrival_ts == r.arrival_ts and r.arrival_ts is not None else r.departure_ts
        probs.append(crowd.p_full(inst, p, int(r.stop_index), float(eta)))
        outcome.append(int(r.left_behind > 0))
    return np.array(probs), np.array(outcome)


def boardings_eval(crowd: CrowdingModel, ridership: pd.DataFrame, test_days: set) -> dict:
    df = crowd.prepare(ridership)
    df["pred"] = crowd.model.predict(crowd._encode(df.copy()))
    df["date"] = pd.to_datetime(df["date"])
    lag = df[["date", "hour", "route_id", "direction_id", "stop_id", "boardings"]].copy()
    lag["date"] = lag["date"] + pd.Timedelta(days=7)
    df = df.merge(lag.rename(columns={"boardings": "last_week"}), how="left",
                  on=["date", "hour", "route_id", "direction_id", "stop_id"])
    test = df[df["date"].dt.date.isin(test_days) & df["last_week"].notna()]
    return {"model_mae": float((test["pred"] - test["boardings"]).abs().mean()),
            "naive_mae": float((test["last_week"] - test["boardings"]).abs().mean()), "n": int(len(test))}


def _cached_features(data_dir: Path, gtfs: GTFS, snapshot_every_s: float, reuse: bool):
    cache = data_dir / "features_cache.pkl"
    raw_mtime = max((p.stat().st_mtime for p in (data_dir / "raw").rglob("*.jsonl.gz")), default=0)
    if reuse and cache.exists() and cache.stat().st_mtime > raw_mtime:
        print("1/5 loading cached features (delete features_cache.pkl to rebuild)")
        return pd.read_pickle(cache)
    print("1/5 replaying recorded feeds through the pipeline to build features + labels")
    out = build_eta_table(data_dir / "raw", gtfs, snapshot_every_s=snapshot_every_s)
    pd.to_pickle(out, cache)
    return out


def train_all(data_dir: Path | str, gtfs: GTFS, model_dir: Path | str = "models", test_days: int = 5,
              report_dir: Path | str = "docs", snapshot_every_s: float = 30.0, capacity: int = 150,
              calibration_days: int = 3, reuse_features: bool = True) -> dict:
    data_dir, model_dir, report_dir = Path(data_dir), Path(model_dir), Path(report_dir)
    t0 = time.time()
    df, priors, stats, events = _cached_features(data_dir, gtfs, snapshot_every_s, reuse_features)
    print(f"    {stats['rows']:,} labelled rows from {stats['messages']:,} messages")
    train, test = split_by_day(df, test_days)
    fit, calib = split_by_day(train, calibration_days)
    test_set = set(test["service_day"].unique())

    print(f"2/5 training quantile ETA model on {len(fit):,} rows ({fit['service_day'].nunique()} days), "
          f"calibrating the band on {calib['service_day'].nunique()} held-out days")
    model = QuantileEtaModel(priors=priors).fit(fit, fit["y_remaining_s"])
    raw10, _, raw90 = model.predict_df(test, calibrated=False)
    raw_cov = float(((test["y_remaining_s"] >= raw10) & (test["y_remaining_s"] <= raw90)).mean())
    margins = model.calibrate(calib, calib["y_remaining_s"])
    p10, p50, p90 = model.predict_df(test)
    eta = eta_metrics(test, p10, p50, p90)
    eta["interval"]["uncalibrated_coverage"] = raw_cov
    eta["interval"]["cqr_margins_s"] = margins
    model.metrics = {"overall": eta["overall"], "interval": eta["interval"],
                     "improvement_vs_official_pct": eta["improvement_vs_official_pct"]}
    model.save(model_dir)
    print(f"    MAE model {eta['overall']['model']['mae_s']:.0f}s vs official {eta['overall']['official']['mae_s']:.0f}s "
          f"vs timetable {eta['overall']['schedule']['mae_s']:.0f}s; p10-p90 coverage {eta['interval']['coverage_p10_p90']:.0%}")

    crowd_res = None
    rid_path = data_dir / "ridership.csv"
    truth = load_truth(data_dir)
    if rid_path.exists():
        print("3/5 training crowding model")
        rid = pd.read_csv(rid_path, dtype=str)
        rid_days = pd.to_datetime(rid["date"]).dt.date
        crowd = CrowdingModel(gtfs, capacity=capacity).fit(rid[~rid_days.isin(test_set)])
        crowd.save(model_dir)
        if truth is not None:
            probs, outcome = evaluate_crowding(crowd, gtfs, truth, test_set)
            crowd_res = crowding_metrics(probs, outcome, boardings_eval(crowd, rid, test_set))
            print(f"    Brier {crowd_res['brier']:.4f} (base {crowd_res['brier_base_rate']:.4f}), AUC {crowd_res['auc']}")
    det = None
    match_acc = None
    if truth is not None:
        print("4/5 checking detector and trip matching against ground truth")
        det = detector_metrics(events, truth)
        match_acc = matching_accuracy(data_dir / "raw", gtfs, truth, sorted(test_set)[-1])
        print(f"    arrival MAE {det['arrival']['mae_s']:.1f}s; trip matching {match_acc:.1%}")

    print("5/5 writing report")
    info = {"source": "simulated" if truth is not None else "recorded", "train_days": int(fit["service_day"].nunique()),
            "calibration_days": int(calib["service_day"].nunique()),
            "test_days": int(len(test_set)), "match_accuracy": match_acc}
    plot_eta(eta, report_dir / "img" / "eta_mae_by_horizon.png")
    if crowd_res:
        plot_calibration(crowd_res["calibration"], report_dir / "img" / "crowding_calibration.png")
    write_report(report_dir / "RESULTS.md", eta, crowd_res, det, info)
    summary = {"eta": eta, "crowding": crowd_res, "detector": det, "info": info, "stats": stats,
               "model_version": model.version, "train_s": round(time.time() - t0, 1)}
    save_json(summary, model_dir / "metrics.json")
    return summary
