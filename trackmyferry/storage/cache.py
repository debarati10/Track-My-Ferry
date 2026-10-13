"""Latest live state (vessels + predictions), shared between processor and API instances.

`MemoryLiveStore` keeps it in-process. `RedisLiveStore` mirrors it into Redis so a freshly started
API replica can serve the current map immediately instead of waiting for new messages.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from ..messages import Prediction, VesselState

VESSELS_KEY = "trackmyferry:vessels"
PREDICTIONS_KEY = "trackmyferry:predictions"


class MemoryLiveStore:
    def __init__(self, stale_s: float = 900.0):
        self.stale_s = stale_s
        self.vessels: dict[int, dict[str, Any]] = {}
        self.predictions: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.updated_ts = 0.0

    def put_state(self, st: VesselState | dict) -> None:
        d = st.to_dict() if isinstance(st, VesselState) else st
        self.vessels[int(d["mmsi"])] = d
        self.updated_ts = max(self.updated_ts, float(d["ts"]))

    def put_predictions(self, mmsi: int, preds: list[Prediction | dict]) -> None:
        self.predictions[int(mmsi)] = [p.to_dict() if isinstance(p, Prediction) else p for p in preds]

    def add_prediction(self, p: Prediction | dict) -> None:
        """Streaming variant: predictions for a vessel arrive as a burst with the same made_at."""
        d = p.to_dict() if isinstance(p, Prediction) else p
        mmsi = int(d["mmsi"])
        cur = self.predictions[mmsi]
        if cur and cur[0]["made_at"] != d["made_at"]:
            cur.clear()
        cur.append(d)

    def get_vessels(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or self.updated_ts or time.time()
        return [v for v in self.vessels.values() if now - float(v["ts"]) <= self.stale_s]

    def get_vessel(self, mmsi: int) -> dict[str, Any] | None:
        return self.vessels.get(int(mmsi))

    def get_predictions(self, mmsi: int) -> list[dict[str, Any]]:
        return list(self.predictions.get(int(mmsi), []))

    def arrivals_at(self, stop_id: str, now: float | None = None, limit: int = 6) -> list[dict[str, Any]]:
        now = now or self.updated_ts or time.time()
        out = [p for preds in self.predictions.values() for p in preds
               if p["stop_id"] == stop_id and float(p["eta_p90"]) >= now - 60
               and int(p["mmsi"]) in self.vessels and now - float(self.vessels[int(p["mmsi"])]["ts"]) <= self.stale_s]
        out.sort(key=lambda p: p["eta_p50"])
        return out[:limit]


class RedisLiveStore(MemoryLiveStore):
    """Write-through to Redis hashes; `warm()` loads the last snapshot on start-up."""

    def __init__(self, url: str, stale_s: float = 900.0):
        super().__init__(stale_s)
        import redis  # pip install redis

        self.r = redis.Redis.from_url(url, decode_responses=True)

    def put_state(self, st: VesselState | dict) -> None:
        super().put_state(st)
        d = st.to_dict() if isinstance(st, VesselState) else st
        self.r.hset(VESSELS_KEY, str(d["mmsi"]), json.dumps(d))

    def put_predictions(self, mmsi: int, preds: list[Prediction | dict]) -> None:
        super().put_predictions(mmsi, preds)
        self.r.hset(PREDICTIONS_KEY, str(mmsi), json.dumps(self.predictions[int(mmsi)]))

    def add_prediction(self, p: Prediction | dict) -> None:
        super().add_prediction(p)
        mmsi = int(p["mmsi"] if isinstance(p, dict) else p.mmsi)
        self.r.hset(PREDICTIONS_KEY, str(mmsi), json.dumps(self.predictions[mmsi]))

    def warm(self) -> int:
        for k, v in self.r.hgetall(VESSELS_KEY).items():
            self.vessels[int(k)] = json.loads(v)
            self.updated_ts = max(self.updated_ts, float(self.vessels[int(k)]["ts"]))
        for k, v in self.r.hgetall(PREDICTIONS_KEY).items():
            self.predictions[int(k)] = json.loads(v)
        return len(self.vessels)


def make_store(redis_url: str | None) -> MemoryLiveStore:
    if redis_url:
        store = RedisLiveStore(redis_url)
        store.warm()
        return store
    return MemoryLiveStore()
