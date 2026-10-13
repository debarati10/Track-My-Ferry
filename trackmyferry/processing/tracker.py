"""Per-vessel track buffer with basic AIS cleaning.

AIS has duplicates, late/out-of-order messages and occasional GPS jumps. The tracker keeps the last
~15 minutes of clean fixes per vessel and rejects points that would require an impossible speed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..geo import KNOT_MS, haversine_m
from ..messages import VesselPosition

MAX_PLAUSIBLE_KN = 45.0
HISTORY_S = 900.0


@dataclass
class Fix:
    ts: float
    lat: float
    lon: float
    sog_kn: float
    cog: float | None


@dataclass
class Track:
    mmsi: int
    name: str | None = None
    fixes: deque = field(default_factory=lambda: deque(maxlen=600))
    rejected: int = 0

    @property
    def last(self) -> Fix | None:
        return self.fixes[-1] if self.fixes else None

    def recent(self, seconds: float, now: float | None = None) -> list[Fix]:
        if not self.fixes:
            return []
        now = now if now is not None else self.fixes[-1].ts
        return [f for f in self.fixes if f.ts >= now - seconds]

    def mean_sog(self, seconds: float = 120.0) -> float:
        rec = self.recent(seconds)
        return float(np.mean([f.sog_kn for f in rec])) if rec else 0.0

    def is_stationary(self, seconds: float = 45.0, max_kn: float = 2.0) -> bool:
        rec = self.recent(seconds)
        return bool(rec) and max(f.sog_kn for f in rec) <= max_kn


class Tracker:
    def __init__(self) -> None:
        self.tracks: dict[int, Track] = {}
        self.out_of_order = 0
        self.jumps = 0

    def update(self, pos: VesselPosition) -> Track | None:
        """Add a position; returns the track, or None if the point was rejected."""
        tr = self.tracks.get(pos.mmsi)
        if tr is None:
            tr = self.tracks[pos.mmsi] = Track(pos.mmsi, pos.name)
        if pos.name:
            tr.name = pos.name
        last = tr.last
        sog = pos.sog_kn
        if last is not None:
            dt = pos.ts - last.ts
            if dt <= 0.5:  # duplicate or out of order: ignore for state
                self.out_of_order += 1
                return None
            d = haversine_m(last.lat, last.lon, pos.lat, pos.lon)
            if d > 150 and d / dt > MAX_PLAUSIBLE_KN * KNOT_MS:
                self.jumps += 1
                tr.rejected += 1
                if tr.rejected < 3:
                    return None
                tr.fixes.clear()  # three in a row: the old position was the outlier
            if sog is None:
                sog = d / dt / KNOT_MS
        tr.rejected = 0
        tr.fixes.append(Fix(pos.ts, pos.lat, pos.lon, float(sog or 0.0), pos.cog))
        while tr.fixes and tr.fixes[0].ts < pos.ts - HISTORY_S:
            tr.fixes.popleft()
        return tr
