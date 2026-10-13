"""WebSocket fan-out for the live map.

Vessel states arrive several times a second; clients get one batched update per `interval_s` with
only the vessels that changed, plus prediction bursts as they happen.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .. import metrics

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, interval_s: float = 1.0, max_queue: int = 200):
        self.interval_s = interval_s
        self.max_queue = max_queue
        self.clients: set[asyncio.Queue] = set()
        self._dirty_vessels: dict[int, dict[str, Any]] = {}
        self._dirty_predictions: dict[int, list[dict[str, Any]]] = {}

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        self.clients.add(q)
        metrics.WS_CLIENTS.set(len(self.clients))
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)
        metrics.WS_CLIENTS.set(len(self.clients))

    def vessel(self, state: dict[str, Any]) -> None:
        self._dirty_vessels[int(state["mmsi"])] = state

    def predictions(self, mmsi: int, preds: list[dict[str, Any]]) -> None:
        self._dirty_predictions[int(mmsi)] = preds

    def _send(self, payload: str) -> None:
        for q in list(self.clients):
            if q.full():  # slow client: drop its oldest frame rather than stall everyone
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(payload)

    def flush(self) -> None:
        if self._dirty_vessels:
            vessels, self._dirty_vessels = list(self._dirty_vessels.values()), {}
            self._send(json.dumps({"type": "vessels", "vessels": vessels}))
        if self._dirty_predictions:
            preds, self._dirty_predictions = self._dirty_predictions, {}
            self._send(json.dumps({"type": "predictions", "predictions": {str(k): v for k, v in preds.items()}}))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                self.flush()
            except Exception:
                log.exception("hub flush failed")
