"""Quantile gradient-boosted ETA model.

Three `HistGradientBoostingRegressor`s (scikit-learn's LightGBM-style histogram GBM) with pinball
loss at the 10th, 50th and 90th percentiles. They learn a *correction* on top of a baseline — the
operator's own prediction when available, otherwise a physics estimate — which trees handle much
better than raw seconds (they can't extrapolate linear trends on their own).

Output: seconds until arrival as (p10, p50, p90), so the app can say "arrives in 6–8 min".

The p10–p90 band is then *conformally calibrated* (conformalized quantile regression, Romano et al.
2019): on held-out calibration days we measure how far actual arrivals fall outside the band and
widen (or narrow) it by the empirical quantile of that score, separately per horizon, which gives
the band its stated 80% coverage without retraining the trees.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from ..processing.features import CATEGORICAL, FEATURES, DwellPriors, Vocab

QUANTILES = (0.1, 0.5, 0.9)
TARGET_COVERAGE = 0.8


def horizon_bucket(stops_ahead: float) -> str:
    if stops_ahead <= 1:
        return "1"
    if stops_ahead <= 2:
        return "2"
    if stops_ahead <= 4:
        return "3-4"
    return "5+"


def baseline_seconds(df: pd.DataFrame) -> np.ndarray:
    off = df["official_remaining_s"].to_numpy(dtype=float)
    naive = df["naive_remaining_s"].to_numpy(dtype=float)
    return np.where(np.isfinite(off), off, naive)


class QuantileEtaModel:
    def __init__(self, vocab: Vocab | None = None, models: dict | None = None, version: str | None = None,
                 priors: DwellPriors | None = None, metrics: dict | None = None, params: dict | None = None,
                 cqr_margins: dict[str, float] | None = None):
        self.vocab = vocab or Vocab()
        self.cqr_margins = cqr_margins or {}
        self.models = models or {}
        self.version = version or "untrained"
        self.priors = priors
        self.metrics = metrics or {}
        self.params = params or dict(max_iter=500, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=60,
                                     l2_regularization=1.0, early_stopping=True, validation_fraction=0.1,
                                     n_iter_no_change=25, random_state=0)

    # ------------------------------------------------------------------ data
    def encode(self, df: pd.DataFrame, grow: bool = False) -> pd.DataFrame:
        X = df.copy()
        if "route_id" in X:
            X["route_code"] = [self.vocab.route(r, grow) for r in X["route_id"]]
        X["stop_code"] = [self.vocab.stop(s, grow) for s in X["stop_id"]]
        return X[FEATURES].astype(float)

    # ------------------------------------------------------------------ train / predict
    def fit(self, df: pd.DataFrame, y: np.ndarray | pd.Series) -> QuantileEtaModel:
        X = self.encode(df, grow=True)
        target = np.asarray(y, dtype=float) - baseline_seconds(df)
        cat_idx = [FEATURES.index(c) for c in CATEGORICAL]
        self.models = {}
        for q in QUANTILES:
            m = HistGradientBoostingRegressor(loss="quantile", quantile=q, categorical_features=cat_idx, **self.params)
            m.fit(X, target)
            self.models[q] = m
        self.version = time.strftime("gbm-%Y%m%d-%H%M%S")
        return self

    def _raw(self, df: pd.DataFrame, X: pd.DataFrame) -> np.ndarray:
        base = baseline_seconds(df)
        preds = np.column_stack([base + self.models[q].predict(X) for q in QUANTILES])
        preds.sort(axis=1)  # guard against quantile crossing
        return preds

    def _apply_cqr(self, preds: np.ndarray, stops_ahead: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if self.cqr_margins:
            m = np.array([self.cqr_margins.get(horizon_bucket(s), 0.0) for s in stops_ahead])
            preds[:, 0] -= m
            preds[:, 2] += m
            preds[:, 0] = np.minimum(preds[:, 0], preds[:, 1])
            preds[:, 2] = np.maximum(preds[:, 2], preds[:, 1])
        return preds[:, 0], preds[:, 1], preds[:, 2]

    def predict_df(self, df: pd.DataFrame, calibrated: bool = True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        X = self.encode(df) if "route_id" in df or "stop_id" in df else df[FEATURES].astype(float)
        preds = self._raw(df, X)
        if not calibrated:
            return preds[:, 0], preds[:, 1], preds[:, 2]
        return self._apply_cqr(preds, df["stops_ahead"].to_numpy())

    def predict(self, rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        df = pd.DataFrame(rows)
        X = df[FEATURES].astype(float)  # live rows already carry codes from this model's vocab
        return self._apply_cqr(self._raw(df, X), df["stops_ahead"].to_numpy())

    def calibrate(self, df: pd.DataFrame, y: np.ndarray | pd.Series, coverage: float = TARGET_COVERAGE) -> dict:
        """Split-conformal widening of the p10-p90 band, per horizon bucket, on held-out data."""
        p10, _, p90 = self.predict_df(df, calibrated=False)
        y = np.asarray(y, dtype=float)
        score = np.maximum(p10 - y, y - p90)
        buckets = np.array([horizon_bucket(s) for s in df["stops_ahead"].to_numpy()])
        self.cqr_margins = {}
        for b in np.unique(buckets):
            s = np.sort(score[buckets == b])
            n = len(s)
            k = min(n - 1, int(np.ceil((n + 1) * coverage)) - 1)
            self.cqr_margins[str(b)] = float(s[k])
        return dict(self.cqr_margins)

    # ------------------------------------------------------------------ persistence
    def save(self, directory: Path | str) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.models, d / "eta_quantile.joblib")
        meta = {
            "version": self.version, "features": FEATURES, "categorical": CATEGORICAL,
            "quantiles": list(QUANTILES), "vocab": asdict(self.vocab), "params": self.params,
            "priors": self.priors.to_dict() if self.priors else None, "metrics": self.metrics,
            "cqr_margins": self.cqr_margins,
        }
        (d / "eta_model.json").write_text(json.dumps(meta, indent=2, default=float))
        return d

    @classmethod
    def load(cls, directory: Path | str) -> QuantileEtaModel:
        d = Path(directory)
        meta = json.loads((d / "eta_model.json").read_text())
        if meta["features"] != FEATURES:
            raise ValueError("saved model was trained with a different feature list; retrain it")
        models = {float(k): v for k, v in joblib.load(d / "eta_quantile.joblib").items()}
        return cls(
            vocab=Vocab(**meta["vocab"]), models=models, version=meta["version"],
            priors=DwellPriors.from_dict(meta["priors"]) if meta.get("priors") else None,
            metrics=meta.get("metrics", {}), params=meta.get("params"), cqr_margins=meta.get("cqr_margins"),
        )

    @staticmethod
    def exists(directory: Path | str) -> bool:
        d = Path(directory)
        return (d / "eta_quantile.joblib").exists() and (d / "eta_model.json").exists()
