"""The processing pipeline: raw messages in, vessel states / stop events / predictions out.

`Pipeline.handle(msg)` is synchronous and side-effect free apart from its own state, so the very
same object runs inside the streaming service (`processing.service`), the all-in-one demo, and the
offline replays that build training data — no train/serve skew.
"""
from __future__ import annotations

import logging
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..gtfs.static import GTFS, Pattern, TripInstance, local_date
from ..messages import (
    EnvObservation,
    Message,
    Prediction,
    ServiceAlert,
    StopEvent,
    TripUpdate,
    VesselPosition,
    VesselState,
    VesselStatic,
)
from .events import StopEventDetector
from .features import DwellPriors, EnvState, VesselDwellHistory, Vocab, build_rows, progress_indices
from .fleet import FleetRegistry
from .matcher import Candidate, TripMatcher, Visit, softmax_confidence
from .tracker import Tracker

log = logging.getLogger(__name__)

REMATCH_EVERY_S = 10.0
SWITCH_MARGIN = 1.5
STALE_VESSEL_S = 900.0


class EtaPredictor(Protocol):
    version: str
    vocab: Vocab

    def predict(self, rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...


class CrowdingPredictor(Protocol):
    def p_full(self, inst: TripInstance, pattern: Pattern, stop_index: int, eta_ts: float) -> float: ...


class PhysicsBaseline:
    """Fallback before a model is trained: wait for schedule + free run + typical dwells."""

    version = "physics-baseline"

    def __init__(self) -> None:
        self.vocab = Vocab()

    def predict(self, rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p50 = np.array([max(r["naive_remaining_s"], 0.0) for r in rows])
        return np.maximum(0.85 * p50 - 30, 0), p50, 1.2 * p50 + 60


@dataclass
class Assignment:
    mmsi: int
    pattern: Pattern
    trip: TripInstance | None
    along_m: float
    cost: float
    confidence: float
    since_ts: float
    lateness_s: float | None

    @property
    def trip_key(self) -> str | None:
        return self.trip.key if self.trip else None


def normalize_name(name: str | None) -> str:
    return "".join(ch for ch in (name or "").upper() if ch.isalnum())


class Pipeline:
    def __init__(
        self,
        gtfs: GTFS,
        eta_model: EtaPredictor | None = None,
        crowding: CrowdingPredictor | None = None,
        registry: FleetRegistry | None = None,
        priors: DwellPriors | None = None,
        prediction_interval_s: float = 15.0,
        max_stops_ahead: int = 8,
        snapshot_hook: Callable[[float, int, Assignment, list[dict]], None] | None = None,
        emit_states: bool = True,
    ):
        self.gtfs = gtfs
        self.eta = eta_model or PhysicsBaseline()
        self.crowding = crowding
        used = {s for p in gtfs.patterns.values() for s in p.stop_ids}
        self.registry = registry or FleetRegistry({s: gtfs.stops[s].name for s in used})
        self.tracker = Tracker()
        self.detector = StopEventDetector({s: gtfs.stops[s] for s in used})
        self.matcher = TripMatcher(gtfs)
        self.priors = priors or DwellPriors()
        self.dwell_history = VesselDwellHistory()
        self.prediction_interval_s = prediction_interval_s
        self.max_stops_ahead = max_stops_ahead
        self.snapshot_hook = snapshot_hook
        self.emit_states = emit_states

        self.names: dict[int, str] = {}
        self.visits: dict[int, deque[Visit]] = defaultdict(lambda: deque(maxlen=12))
        self.assign: dict[int, Assignment] = {}
        self.trip_holder: dict[str, int] = {}
        self.official: dict[str, dict[str, float]] = {}
        self.official_ts: dict[str, float] = {}
        self.vehicle_hints: dict[str, tuple[str, float]] = {}
        self.alerts: dict[str, ServiceAlert] = {}
        self.env = EnvState()
        self.latest_state: dict[int, VesselState] = {}
        self.latest_predictions: dict[int, list[Prediction]] = {}
        self._last_match: dict[int, float] = {}
        self._last_pred: dict[int, float] = {}
        self._active_cache: tuple[float, list[TripInstance]] = (-1.0, [])
        self._last_housekeeping = 0.0
        self.counters: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------ dispatch
    def handle(self, msg: Message) -> list[Message]:
        if isinstance(msg, VesselPosition):
            return self._on_position(msg)
        if isinstance(msg, TripUpdate):
            self._on_trip_update(msg)
        elif isinstance(msg, EnvObservation):
            self._on_env(msg)
        elif isinstance(msg, VesselStatic):
            if msg.name:
                self.names[msg.mmsi] = msg.name
            self.registry.note_static(msg.mmsi, msg.ship_type)
        elif isinstance(msg, ServiceAlert):
            self.alerts[msg.alert_id] = msg
        return []

    # ------------------------------------------------------------------ inputs
    def _on_trip_update(self, tu: TripUpdate) -> None:
        service_date = tu.start_date or local_date(tu.ts).strftime("%Y%m%d")
        key = f"{tu.trip_id}@{service_date}"
        etas = {s.stop_id: s.arrival_ts or s.departure_ts for s in tu.stop_updates
                if not s.skipped and (s.arrival_ts or s.departure_ts)}
        self.official[key] = etas
        self.official_ts[key] = tu.ts
        if tu.vehicle_label:
            self.vehicle_hints[normalize_name(tu.vehicle_label)] = (key, tu.ts)
        self.counters["trip_updates"] += 1

    def _on_env(self, obs: EnvObservation) -> None:
        if obs.kind == "current" and "speed_kn" in obs.values:
            self.env.current_kn = float(obs.values["speed_kn"])
        elif obs.kind == "wind" and "speed_kn" in obs.values:
            self.env.wind_kn = float(obs.values["speed_kn"])
        elif obs.kind == "weather" and "precip_mm" in obs.values:
            self.env.precip_mm = float(obs.values["precip_mm"])
        self.env.updated_ts = obs.ts

    def _on_position(self, pos: VesselPosition) -> list[Message]:
        self.counters["positions"] += 1
        if pos.name:
            self.names[pos.mmsi] = pos.name
        track = self.tracker.update(pos)
        if track is None:
            return []
        now = pos.ts
        prev = track.fixes[-2] if len(track.fixes) > 1 else None
        events = self.detector.update(pos.mmsi, track.last, prev)
        for ev in events:
            if ev.departure_ts is None:
                self.registry.note_visit(pos.mmsi, ev.stop_id, ev.arrival_ts)
        if not self.registry.is_ferry(pos.mmsi):
            return []

        out: list[Message] = []
        for ev in events:
            out.append(self._enrich_event(ev))
        if events or now - self._last_match.get(pos.mmsi, -1e9) >= REMATCH_EVERY_S:
            self._rematch(pos.mmsi, track, now)
            self._last_match[pos.mmsi] = now
        a = self.assign.get(pos.mmsi)
        docked = self.detector.docked_at(pos.mmsi)
        if a is not None:
            self._update_along(a, track.last.lat, track.last.lon, docked)

        state = self._state(pos.mmsi, track, a, docked, now)
        self.latest_state[pos.mmsi] = state
        if self.emit_states:
            out.append(state)
        if a is not None and a.trip is not None and now - self._last_pred.get(pos.mmsi, -1e9) >= self.prediction_interval_s:
            self._last_pred[pos.mmsi] = now
            preds = self._predict(pos.mmsi, a, track, docked, now)
            self.latest_predictions[pos.mmsi] = preds
            out.extend(preds)
        if now - self._last_housekeeping > 300:
            self._housekeeping(now)
        return out

    # ------------------------------------------------------------------ events & matching
    def _enrich_event(self, ev: StopEvent) -> StopEvent:
        a = self.assign.get(ev.mmsi)
        ev.vessel_name = self.names.get(ev.mmsi)
        if a is not None and a.trip is not None:
            ev.trip_id, ev.route_id, ev.direction_id = a.trip.trip_id, a.trip.route_id, a.trip.direction_id
            ev.service_date = a.trip.service_date
        visits = self.visits[ev.mmsi]
        if ev.departure_ts is None:
            visits.append(Visit(ev.stop_id, ev.arrival_ts, None))
            self.counters["arrivals"] += 1
        else:
            if visits and visits[-1].stop_id == ev.stop_id and visits[-1].departure_ts is None:
                visits[-1].departure_ts = ev.departure_ts
            self.counters["departures"] += 1
            if a is not None:
                idx = a.pattern.stop_index(ev.stop_id)
                if idx is not None and 0 < idx < len(a.pattern.stop_ids) - 1:
                    prior = self.priors.get(ev.stop_id, ev.arrival_ts)
                    self.priors.update(ev.stop_id, ev.arrival_ts, ev.dwell_s or 0.0)
                    self.dwell_history.add(ev.mmsi, (ev.dwell_s or 0.0) - prior)
        return ev

    def _active(self, now: float) -> list[TripInstance]:
        if now - self._active_cache[0] > 60:
            self._active_cache = (now, self.gtfs.instances_between(now - 4500, now + 2700))
        return self._active_cache[1]

    def _rematch(self, mmsi: int, track, now: float) -> None:
        docked = self.detector.docked_at(mmsi)
        hint = self.vehicle_hints.get(normalize_name(self.names.get(mmsi)))
        hint_key = hint[0] if hint and now - hint[1] < 300 else None
        cands = self.matcher.candidates(track, now, list(self.visits[mmsi]), docked, self._active(now), hint_key)
        cur = self.assign.get(mmsi)
        if not cands:
            if cur is not None and now - cur.since_ts > 60:
                self._release(mmsi)
            return

        best: Candidate | None = None
        for c in cands:
            holder = self.trip_holder.get(c.trip_key) if c.trip_key else None
            if holder is not None and holder != mmsi and holder in self.assign and self.assign[holder].cost <= c.cost:
                continue
            best = c
            break
        if best is None:
            best = cands[0]
        conf = softmax_confidence([c.cost for c in cands])

        if cur is not None:
            same = next((c for c in cands if c.trip_key == cur.trip_key and c.pattern.pattern_id == cur.pattern.pattern_id), None)
            if same is not None and (best.trip_key == cur.trip_key or best.cost >= same.cost - SWITCH_MARGIN):
                cur.cost, cur.confidence, cur.lateness_s = same.cost, conf, same.lateness_s
                return
        self._release(mmsi)
        if best.trip_key:
            holder = self.trip_holder.get(best.trip_key)
            if holder is not None and holder != mmsi:
                self._release(holder)  # we fit this trip better; the other boat re-matches next update
            self.trip_holder[best.trip_key] = mmsi
        self.assign[mmsi] = Assignment(mmsi, best.pattern, best.trip, best.along_m, best.cost, conf, now, best.lateness_s)
        self.counters["assignments"] += 1

    def _release(self, mmsi: int) -> None:
        a = self.assign.pop(mmsi, None)
        if a is not None and a.trip_key and self.trip_holder.get(a.trip_key) == mmsi:
            del self.trip_holder[a.trip_key]

    @staticmethod
    def _update_along(a: Assignment, lat: float, lon: float, docked) -> None:
        if docked is not None:
            idx = a.pattern.stop_index(docked[0])
            if idx is not None:
                a.along_m = a.pattern.stop_along[idx]
                return
        pr = a.pattern.shape.project(lat, lon, near_along=a.along_m, window_m=2500.0)
        a.along_m = max(a.along_m - 50.0, pr.along_m) if pr.along_m < a.along_m else pr.along_m

    # ------------------------------------------------------------------ outputs
    def _state(self, mmsi: int, track, a: Assignment | None, docked, now: float) -> VesselState:
        f = track.last
        st = VesselState(mmsi=mmsi, ts=now, lat=f.lat, lon=f.lon, name=self.names.get(mmsi), sog_kn=f.sog_kn,
                         cog=f.cog, status="docked" if docked else ("underway" if f.sog_kn > 2 else "unassigned"))
        if docked:
            st.current_stop_id = docked[0]
        if a is not None:
            p = a.pattern
            st.route_id, st.direction_id, st.pattern_id = p.route_id, p.direction_id, p.pattern_id
            st.along_m = round(a.along_m, 1)
            st.match_confidence = round(a.confidence, 3)
            if a.trip is not None:
                st.trip_id = a.trip.trip_id
                st.delay_s = None if a.lateness_s is None else round(a.lateness_s, 0)
            docked_idx = p.stop_index(docked[0]) if docked else None
            _, nxt = progress_indices(p, a.along_m, docked_idx)
            st.next_stop_id = p.stop_ids[nxt] if nxt is not None else None
        elif st.status != "docked":
            st.status = "unassigned"
        return st

    def _predict(self, mmsi: int, a: Assignment, track, docked, now: float) -> list[Prediction]:
        p, inst = a.pattern, a.trip
        docked_idx = p.stop_index(docked[0]) if docked else None
        dwell_elapsed = now - docked[1] if docked else 0.0
        rows = build_rows(
            now, p, inst, a.along_m, docked_idx, dwell_elapsed, track.last.sog_kn, track.mean_sog(120.0),
            self.env, self.priors, self.dwell_history.excess(mmsi), self.official.get(inst.key),
            self.eta.vocab, self.max_stops_ahead,
        )
        if not rows:
            return []
        if self.snapshot_hook is not None:
            self.snapshot_hook(now, mmsi, a, rows)
        p10, p50, p90 = self.eta.predict(rows)
        p50 = np.maximum(p50, 0.0)
        p10 = np.minimum(np.maximum(p10, 0.0), p50)
        p90 = np.maximum(p90, p50)
        preds = []
        for r, lo, mid, hi in zip(rows, p10, p50, p90, strict=True):
            eta50 = now + float(mid)
            p_full = None
            if self.crowding is not None:
                try:
                    p_full = round(float(self.crowding.p_full(inst, p, r["stop_index"], eta50)), 3)
                except Exception:  # crowding is best-effort; never block ETAs
                    log.debug("crowding prediction failed", exc_info=True)
            preds.append(Prediction(
                made_at=now, mmsi=mmsi, trip_id=inst.trip_id, route_id=inst.route_id,
                direction_id=inst.direction_id, stop_id=r["stop_id"], stops_ahead=int(r["stops_ahead"]),
                eta_p10=round(now + float(lo), 1), eta_p50=round(eta50, 1), eta_p90=round(now + float(hi), 1),
                official_eta=r["official_eta"], scheduled_eta=r["scheduled_eta"],
                vessel_name=self.names.get(mmsi), p_full=p_full, model_version=self.eta.version,
            ))
        self.counters["predictions"] += len(preds)
        return preds

    def _housekeeping(self, now: float) -> None:
        self._last_housekeeping = now
        for mmsi, st in list(self.latest_state.items()):
            if now - st.ts > STALE_VESSEL_S:
                self.latest_state.pop(mmsi, None)
                self.latest_predictions.pop(mmsi, None)
                self._release(mmsi)
        for key, ts in list(self.official_ts.items()):
            if now - ts > 3 * 3600:
                self.official.pop(key, None)
                self.official_ts.pop(key, None)

    # ------------------------------------------------------------------ queries for the API
    def arrivals_at(self, stop_id: str, now: float | None = None, limit: int = 6) -> list[Prediction]:
        now = now if now is not None else time.time()
        out = [p for preds in self.latest_predictions.values() for p in preds
               if p.stop_id == stop_id and p.eta_p90 >= now - 60]
        out.sort(key=lambda p: p.eta_p50)
        return out[:limit]


def isfinite(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))
