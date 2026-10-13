"""Evaluation: ETA accuracy vs baselines, interval calibration, crowding calibration; report + plots."""
from __future__ import annotations

import json
import math
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd

BUCKETS = [(0, 300, "0–5 min"), (300, 600, "5–10 min"), (600, 1200, "10–20 min"), (1200, 7200, "20+ min")]


def _err_stats(err: np.ndarray) -> dict:
    a = np.abs(err)
    return {"mae_s": float(a.mean()), "rmse_s": float(np.sqrt((err**2).mean())),
            "p90_abs_s": float(np.quantile(a, 0.9)), "bias_s": float(err.mean()),
            "within_1min": float((a <= 60).mean()), "n": int(len(err))}


def eta_metrics(df: pd.DataFrame, p10: np.ndarray, p50: np.ndarray, p90: np.ndarray) -> dict:
    y = df["y_remaining_s"].to_numpy(dtype=float)
    sched = df["sched_remaining_s"].to_numpy(dtype=float)
    official = df["official_remaining_s"].to_numpy(dtype=float)
    official = np.where(np.isfinite(official), official, sched)  # the app falls back to schedule
    methods = {
        "schedule": sched,
        "official": official,
        "physics": df["naive_remaining_s"].to_numpy(dtype=float),
        "model": p50,
    }
    out: dict = {"overall": {}, "by_horizon": {}, "by_route": {}}
    for name, pred in methods.items():
        out["overall"][name] = _err_stats(pred - y)
    for lo, hi, label in BUCKETS:
        m = (y >= lo) & (y < hi)
        if m.sum() == 0:
            continue
        out["by_horizon"][label] = {name: _err_stats(pred[m] - y[m]) for name, pred in methods.items()}
    for route, idx in df.groupby("route_id").indices.items():
        out["by_route"][route] = {name: _err_stats(pred[idx] - y[idx]) for name, pred in methods.items()}
    inside = (y >= p10) & (y <= p90)
    out["interval"] = {
        "coverage_p10_p90": float(inside.mean()), "target": 0.8,
        "below_p10": float((y < p10).mean()), "above_p90": float((y > p90).mean()),
        "mean_width_s": float((p90 - p10).mean()),
    }
    o, mo = out["overall"]["official"]["mae_s"], out["overall"]["model"]["mae_s"]
    out["improvement_vs_official_pct"] = 100.0 * (o - mo) / o if o > 0 else None
    s = out["overall"]["schedule"]["mae_s"]
    out["improvement_vs_schedule_pct"] = 100.0 * (s - mo) / s if s > 0 else None
    return out


def detector_metrics(events: pd.DataFrame, truth: pd.DataFrame) -> dict:
    """Compare detected stop events against simulator ground truth (simulation only)."""
    t = truth[truth["arrival_ts"].notna()]
    errs_a, errs_d = [], []
    by = {k: g for k, g in t.groupby(["mmsi", "stop_id"])}
    for ev in events.itertuples():
        g = by.get((ev.mmsi, ev.stop_id))
        if g is None:
            continue
        diff = (g["arrival_ts"] - ev.arrival_ts).abs()
        j = diff.idxmin()
        if diff[j] > 300:
            continue
        errs_a.append(ev.arrival_ts - g.at[j, "arrival_ts"])
        if not math.isnan(g.at[j, "departure_ts"]) and ev.departure_ts == ev.departure_ts:
            errs_d.append(ev.departure_ts - g.at[j, "departure_ts"])
    ea, ed = np.array(errs_a), np.array(errs_d)
    return {"matched": int(len(ea)), "truth_arrivals": int(len(t)), "detected": int(len(events)),
            "arrival": _err_stats(ea) if len(ea) else None, "departure": _err_stats(ed) if len(ed) else None}


def crowding_metrics(p: np.ndarray, outcome: np.ndarray, boardings_eval: dict | None = None) -> dict:
    from sklearn.metrics import brier_score_loss, roc_auc_score

    base = float(outcome.mean())
    res = {"n": int(len(p)), "event_rate": base, "brier": float(brier_score_loss(outcome, p)),
           "brier_base_rate": float(brier_score_loss(outcome, np.full_like(p, base)))}
    res["brier_skill"] = 1 - res["brier"] / res["brier_base_rate"] if res["brier_base_rate"] > 0 else None
    res["auc"] = float(roc_auc_score(outcome, p)) if 0 < outcome.sum() < len(outcome) else None
    bins = [0, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0001]
    cal = []
    for lo, hi in pairwise(bins):
        m = (p >= lo) & (p < hi)
        if m.sum():
            cal.append({"bin": f"{lo:.2f}–{min(hi, 1):.2f}", "n": int(m.sum()),
                        "mean_pred": float(p[m].mean()), "observed": float(outcome[m].mean())})
    res["calibration"] = cal
    if boardings_eval:
        res["boardings"] = boardings_eval
    return res


# ---------------------------------------------------------------------- reporting
def plot_eta(metrics: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = list(metrics["by_horizon"])
    methods = [("schedule", "Timetable", "#9BA5AE"), ("official", "Official prediction", "#5F6670"),
               ("physics", "Physics baseline", "#C9B37E"), ("model", "Track My Ferry model", "#1D4F91")]
    x = np.arange(len(labels))
    w = 0.2
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
    for i, (key, name, color) in enumerate(methods):
        vals = [metrics["by_horizon"][lab][key]["mae_s"] / 60 for lab in labels]
        ax.bar(x + (i - 1.5) * w, vals, w, label=name, color=color)
    ax.set_xticks(x, labels)
    ax.set_ylabel("Mean absolute error (minutes)")
    ax.set_xlabel("Time until the boat actually arrives")
    ax.set_title("Arrival-time error by horizon (simulated test days)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def plot_calibration(cal: list[dict], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(4.6, 4.2), dpi=150)
    xs = [c["mean_pred"] for c in cal]
    ys = [c["observed"] for c in cal]
    ax.plot([0, max(xs + ys + [0.05])], [0, max(xs + ys + [0.05])], color="#9BA5AE", lw=1, ls="--", label="Perfect")
    ax.plot(xs, ys, marker="o", color="#1D4F91", label="Track My Ferry")
    for c in cal:
        ax.annotate(f"n={c['n']}", (c["mean_pred"], c["observed"]), fontsize=6, xytext=(4, -8), textcoords="offset points")
    ax.set_xlabel("Predicted chance the boat is full")
    ax.set_ylabel("Observed share left on the dock")
    ax.set_title("Crowding calibration")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def write_report(path: Path, eta: dict, crowd: dict | None, detector: dict | None, info: dict) -> None:
    def mins(s):
        return f"{s / 60:.2f} min"

    lines = ["# Track My Ferry evaluation report", "",
             f"Data: {info.get('source', 'simulated')} · train days: {info.get('train_days')} · "
             f"calibration days: {info.get('calibration_days', 0)} · test days: {info.get('test_days')} · "
             f"test rows: {eta['overall']['model']['n']:,}", "",
             "> These numbers come from the built-in simulator, not real NYC Ferry operations. They show the "
             "pipeline works end to end; rerun `trackmyferry train` on recorded live data for real results.", "",
             "## Arrival-time accuracy", "",
             "| Method | MAE | RMSE | 90th pct abs error | Bias | Within 1 min |", "|---|---|---|---|---|---|"]
    names = {"schedule": "Timetable", "official": "Official prediction (GTFS-RT)", "physics": "Physics baseline",
             "model": "**Track My Ferry (p50)**"}
    for k, label in names.items():
        m = eta["overall"][k]
        lines.append(f"| {label} | {mins(m['mae_s'])} | {mins(m['rmse_s'])} | {mins(m['p90_abs_s'])} | "
                     f"{m['bias_s']:+.0f} s | {m['within_1min']:.0%} |")
    lines += ["", f"Track My Ferry cuts mean absolute error by **{eta['improvement_vs_official_pct']:.0f}%** vs the official "
              f"prediction and **{eta['improvement_vs_schedule_pct']:.0f}%** vs the timetable.", "",
              "### By horizon (MAE)", "", "| Horizon | Timetable | Official | Physics | Track My Ferry | n |", "|---|---|---|---|---|---|"]
    for lab, d in eta["by_horizon"].items():
        lines.append(f"| {lab} | {mins(d['schedule']['mae_s'])} | {mins(d['official']['mae_s'])} | "
                     f"{mins(d['physics']['mae_s'])} | {mins(d['model']['mae_s'])} | {d['model']['n']:,} |")
    lines += ["", "### By route (MAE)", "", "| Route | Official | Track My Ferry |", "|---|---|---|"]
    for r, d in eta["by_route"].items():
        lines.append(f"| {r} | {mins(d['official']['mae_s'])} | {mins(d['model']['mae_s'])} |")
    iv = eta["interval"]
    lines += ["", "### Uncertainty band", "",
              f"The p10–p90 band contained the actual arrival **{iv['coverage_p10_p90']:.0%}** of the time "
              f"(target 80%); {iv['below_p10']:.0%} arrived earlier, {iv['above_p90']:.0%} later. "
              f"Average band width: {mins(iv['mean_width_s'])}."
              + (f" Before conformal calibration it was {iv['uncalibrated_coverage']:.0%}; margins per stops-ahead "
                 f"bucket (s): {', '.join(f'{k}: {v:+.0f}' for k, v in iv['cqr_margins_s'].items())}."
                 if "uncalibrated_coverage" in iv else ""), "",
              "![ETA error by horizon](img/eta_mae_by_horizon.png)", ""]
    if detector:
        a, d = detector["arrival"], detector["departure"]
        lines += ["## Arrival / departure detection (labels)", "",
                  f"Matched {detector['matched']:,} of {detector['truth_arrivals']:,} true stop visits. "
                  f"Arrival time error: MAE {a['mae_s']:.1f} s, 90th pct {a['p90_abs_s']:.1f} s. "
                  f"Departure time error: MAE {d['mae_s']:.1f} s, 90th pct {d['p90_abs_s']:.1f} s.", ""]
    if info.get("match_accuracy") is not None:
        lines += [f"Vessel-to-trip matching was correct for **{info['match_accuracy']:.1%}** of underway positions.", ""]
    if crowd:
        lines += ["## Crowding", "",
                  "Ridership data counts people who boarded, not people who wanted to, so demand is censored at "
                  "capacity on the busiest trips. Expect the model to under-call the most extreme rush-hour "
                  "crowding; it ranks risky departures well (AUC) and is calibrated in the 1–50% range.", "",
                  f"Departures evaluated: {crowd['n']:,}; share with someone left on the dock: {crowd['event_rate']:.2%}.",
                  f"Brier score {crowd['brier']:.4f} vs {crowd['brier_base_rate']:.4f} for a constant base rate "
                  f"(skill {crowd['brier_skill']:.2f}); AUC {crowd['auc']:.3f}." if crowd.get("auc") else "", ""]
        if crowd.get("boardings"):
            b = crowd["boardings"]
            lines += [f"Hourly boardings forecast MAE: {b['model_mae']:.2f} vs {b['naive_mae']:.2f} for "
                      f"'same hour last week'.", ""]
        lines += ["| Predicted | Observed | n |", "|---|---|---|"]
        for c in crowd["calibration"]:
            lines.append(f"| {c['mean_pred']:.3f} | {c['observed']:.3f} | {c['n']:,} |")
        lines += ["", "![Crowding calibration](img/crowding_calibration.png)", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def save_json(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=lambda o: float(o) if isinstance(o, np.floating) else str(o)))
