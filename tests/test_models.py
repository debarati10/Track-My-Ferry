import numpy as np
import pandas as pd
import pytest

from trackmyferry.ml.crowding import CrowdingModel, infer_direction_ids
from trackmyferry.ml.eta_model import QuantileEtaModel
from trackmyferry.ml.evaluate import eta_metrics
from trackmyferry.processing.features import FEATURES


def synthetic_eta_frame(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(size=n) for f in FEATURES})
    df["route_id"] = rng.choice(["ER", "AS"], n)
    df["stop_id"] = rng.choice(["A", "B", "C"], n)
    df["dist_m"] = rng.uniform(200, 9000, n)
    df["naive_remaining_s"] = df["dist_m"] / 10.3 + 40
    df["official_remaining_s"] = np.where(rng.random(n) < 0.8, df["naive_remaining_s"] + 30, np.nan)
    df["sched_remaining_s"] = df["naive_remaining_s"] + 60
    df["current_assist_kn"] = rng.uniform(-3, 3, n)
    # truth: the current matters, and there's heteroscedastic noise
    y = df["dist_m"] / (10.3 + 0.5 * df["current_assist_kn"]) + 40 + rng.normal(0, 20 + df["dist_m"] / 300)
    df["y_remaining_s"] = y
    return df


def test_quantile_model_learns_and_round_trips(tmp_path):
    df = synthetic_eta_frame()
    train, test = df.iloc[:3000], df.iloc[3000:]
    m = QuantileEtaModel(params=dict(max_iter=150, learning_rate=0.1, random_state=0)).fit(train, train["y_remaining_s"])
    p10, p50, p90 = m.predict_df(test)
    assert np.all(p10 <= p50) and np.all(p50 <= p90)
    met = eta_metrics(test.assign(route_id=test["route_id"]), p10, p50, p90)
    assert met["overall"]["model"]["mae_s"] < met["overall"]["official"]["mae_s"]
    assert 0.55 < met["interval"]["coverage_p10_p90"] < 0.95

    m.save(tmp_path)
    m2 = QuantileEtaModel.load(tmp_path)
    rows = test.copy()
    rows["route_code"] = [m2.vocab.route(r) for r in rows["route_id"]]
    rows["stop_code"] = [m2.vocab.stop(s) for s in rows["stop_id"]]
    q10, q50, q90 = m2.predict(rows.to_dict("records"))
    assert np.allclose(q50, p50)


def test_direction_inference(gtfs):
    d = infer_direction_ids(gtfs)
    assert d[("ER", "NB")] == 0 and d[("ER", "SB")] == 1
    assert d[("SB", "SB")] == 0  # South Brooklyn's outbound runs south to Bay Ridge


def test_crowding_model_probabilities(gtfs, sim_run, tmp_path):
    sim, _ = sim_run
    rid = pd.DataFrame(sim.ridership_rows())
    model = CrowdingModel(gtfs, capacity=150).fit(rid)
    assert model.dispersion >= 2
    # an East River trip inside the simulated window (about 09:30-10:00), where ridership was observed
    day = gtfs.instances_for_date(pd.Timestamp("2026-10-14").date())
    inst = next(i for i in day if i.route_id == "ER" and i.direction_id == 0 and i.trip_id.endswith(("0930", "0950")))
    p = gtfs.patterns[inst.pattern_id]
    probs = [model.p_full(inst, p, k, float(inst.arr[k])) for k in range(len(p.stop_ids) - 1)]
    assert all(0.0 <= x <= 1.0 for x in probs)
    model.capacity = 5  # a tiny boat is almost always full mid-route
    assert model.p_full(inst, p, len(p.stop_ids) - 2, float(inst.arr[-2])) > 0.5
    model.save(tmp_path)
    again = CrowdingModel.load(tmp_path, gtfs)
    assert again.dispersion == pytest.approx(model.dispersion)
