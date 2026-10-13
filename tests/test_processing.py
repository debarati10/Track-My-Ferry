from collections import defaultdict

import numpy as np
import pandas as pd

from trackmyferry.geo import HARBOR, KNOT_MS
from trackmyferry.messages import Prediction, StopEvent, VesselPosition, VesselState
from trackmyferry.processing.events import StopEventDetector
from trackmyferry.processing.features import current_assist_kn
from trackmyferry.processing.pipeline import Pipeline
from trackmyferry.processing.tracker import Tracker


def _approach(gtfs, stop_id="DUMBO"):
    """A synthetic boat (10 s fixes, physically consistent speeds) that docks for ~60 s and leaves."""
    s = gtfs.stops[stop_id]
    x0, y0 = HARBOR.to_xy(s.lat, s.lon)
    approach = [(400, 20), (300, 19), (205, 17), (120, 12), (60, 7), (25, 4), (8, 1.5)]
    dwell = [(0, 0)] * 6
    depart = [(15, 4), (70, 9), (170, 15), (300, 20)]
    fixes, t = [], 1_790_000_000.0
    for i, (d, v) in enumerate(approach + dwell + depart):
        side = -1 if i < len(approach) + len(dwell) else 1  # arrive from the south, leave to the north
        lat, lon = HARBOR.to_latlon(x0, y0 + side * d)
        fixes.append(VesselPosition(mmsi=1, ts=t, lat=float(lat), lon=float(lon), sog_kn=float(v)))
        t += 10
    return fixes


def test_detector_arrival_then_departure(gtfs):
    tracker = Tracker()
    det = StopEventDetector({k: gtfs.stops[k] for k in ("DUMBO", "PIER11")})
    events = []
    for pos in _approach(gtfs):
        tr = tracker.update(pos)
        assert tr is not None, "synthetic track should never be rejected"
        prev = tr.fixes[-2] if len(tr.fixes) > 1 else None
        events += det.update(1, tr.last, prev)
    assert [e.departure_ts is None for e in events] == [True, False]
    arr, dep = events
    assert arr.stop_id == dep.stop_id == "DUMBO"
    assert 1_790_000_050 <= arr.arrival_ts <= 1_790_000_070
    assert 1_790_000_118 <= dep.departure_ts <= 1_790_000_132


def test_tracker_rejects_gps_jump():
    tr = Tracker()
    assert tr.update(VesselPosition(1, 0.0, 40.70, -74.00, 20)) is not None
    assert tr.update(VesselPosition(1, 10.0, 40.75, -74.00, 20)) is None  # 5.5 km in 10 s
    assert tr.update(VesselPosition(1, 20.0, 40.7008, -74.00, 20)) is not None
    assert tr.update(VesselPosition(1, 20.0, 40.7008, -74.00, 20)) is None  # duplicate timestamp


def test_current_assist_sign(gtfs):
    north = gtfs.patterns["ER-0-0"]
    south = gtfs.patterns["ER-1-0"]
    flood = 2.0  # + = flood, running up the East River
    assert current_assist_kn(north, 0, north.shape.length, flood) > 1.0
    assert current_assist_kn(south, 0, south.shape.length, flood) < -1.0


def test_pipeline_against_ground_truth(gtfs, sim_run):
    sim, msgs = sim_run
    pipe = Pipeline(gtfs)
    states, events, preds = [], [], []
    for m in msgs:
        for o in pipe.handle(m):
            (states if isinstance(o, VesselState) else events if isinstance(o, StopEvent) else preds).append(o)
    truth = pd.DataFrame(sim.truth)

    # every simulated boat is discovered as a ferry without an allowlist
    assert len(pipe.registry.discovered) == len(sim.all_vessels()) - sum(not v.block for v in sim.all_vessels())

    # arrival detection is accurate to seconds
    done = [e for e in events if e.departure_ts is not None]
    errs = []
    for e in done:
        t = truth[(truth.mmsi == e.mmsi) & (truth.stop_id == e.stop_id)]
        t = t[(t.arrival_ts - e.arrival_ts).abs() < 300]
        if len(t):
            errs.append(e.arrival_ts - t.arrival_ts.iloc[0])
    assert len(errs) >= 0.9 * len(done) and len(done) > 50
    assert np.mean(np.abs(errs)) < 15

    # trip matching is right almost all the time while boats are moving
    spans = defaultdict(list)
    for (mmsi, trip), g in truth.groupby(["mmsi", "trip_id"]):
        start = g.loc[g.stop_index == 0, "departure_ts"].min()
        spans[mmsi].append((start if start == start else g.departure_ts.min(), g.arrival_ts.max(), trip))
    ok = total = 0
    for s in states:
        if s.status != "underway":
            continue
        hit = [x for x in spans[s.mmsi] if x[0] <= s.ts <= x[1]]
        if hit:
            total += 1
            ok += s.trip_id == hit[0][2]
    assert total > 1000 and ok / total > 0.95

    # predictions are well-formed
    assert preds and all(isinstance(p, Prediction) and p.eta_p10 <= p.eta_p50 <= p.eta_p90 for p in preds)
    assert sum(p.official_eta is not None for p in preds) > 0.9 * len(preds)
    assert all(p.stops_ahead >= 1 for p in preds)


def test_kinematics_are_physical(sim_run):
    _, msgs = sim_run
    sogs = [m.sog_kn for m in msgs if isinstance(m, VesselPosition) and m.sog_kn]
    assert max(sogs) < 30 and np.median(sogs) > 10
    assert KNOT_MS > 0.5
