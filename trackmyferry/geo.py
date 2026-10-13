"""Geodesy helpers: distances, bearings and fast projection of points onto route shapes.

Ferry routes are a few kilometres to ~30 km long, so a local equirectangular projection centred on
New York Harbor is accurate to well under a metre. All polyline math below is done in that plane.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6_371_008.8
KNOT_MS = 0.514444  # metres per second in one knot


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Works on scalars or numpy arrays."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    d = 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    return float(d) if np.ndim(d) == 0 else d


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings (0..180)."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


class LocalProjection:
    """Equirectangular projection to metres around a reference point."""

    def __init__(self, lat0: float = 40.72, lon0: float = -73.98):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = math.radians(1.0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
        self.ky = math.radians(1.0) * EARTH_RADIUS_M

    def to_xy(self, lat, lon):
        return (np.asarray(lon, dtype=float) - self.lon0) * self.kx, (np.asarray(lat, dtype=float) - self.lat0) * self.ky

    def to_latlon(self, x, y):
        return self.lat0 + np.asarray(y) / self.ky, self.lon0 + np.asarray(x) / self.kx


HARBOR = LocalProjection()


@dataclass(frozen=True)
class Projection:
    along_m: float  # distance along the shape from its start
    cross_m: float  # signed offset from the shape; positive = right of travel direction
    segment: int
    bearing_deg: float  # shape bearing at the projected point


class Polyline:
    """A route shape with cumulative distances, supporting projection and interpolation."""

    def __init__(self, coords: Sequence[Sequence[float]] | np.ndarray, proj: LocalProjection = HARBOR):
        pts = np.asarray(coords, dtype=float)
        if pts.ndim != 2 or pts.shape[0] < 2:
            raise ValueError("a polyline needs at least two (lat, lon) points")
        keep = np.r_[True, np.any(np.diff(pts, axis=0) != 0, axis=1)]
        pts = pts[keep]
        if len(pts) < 2:
            raise ValueError("polyline collapses to a single point")
        self.latlon = pts
        self.proj = proj
        x, y = proj.to_xy(pts[:, 0], pts[:, 1])
        self.xy = np.column_stack([x, y])
        self.seg_vec = np.diff(self.xy, axis=0)
        self.seg_len = np.hypot(self.seg_vec[:, 0], self.seg_vec[:, 1])
        self.cum = np.r_[0.0, np.cumsum(self.seg_len)]
        self.length = float(self.cum[-1])
        self.seg_bearing = (np.degrees(np.arctan2(self.seg_vec[:, 0], self.seg_vec[:, 1])) + 360.0) % 360.0
        self._l2 = np.where(self.seg_len > 0, self.seg_len**2, 1.0)

    def project(
        self,
        lat: float,
        lon: float,
        near_along: float | None = None,
        window_m: float | None = None,
        min_along: float | None = None,
    ) -> Projection:
        """Project a point onto the shape.

        `near_along`/`window_m` restrict the search to part of the shape (useful for shapes that
        double back on themselves); `min_along` forces forward progress.
        """
        px = (lon - self.proj.lon0) * self.proj.kx
        py = (lat - self.proj.lat0) * self.proj.ky
        a = self.xy[:-1]
        v = self.seg_vec
        t = ((px - a[:, 0]) * v[:, 0] + (py - a[:, 1]) * v[:, 1]) / self._l2
        t = np.minimum(np.maximum(t, 0.0), 1.0)
        dx = px - (a[:, 0] + t * v[:, 0])
        dy = py - (a[:, 1] + t * v[:, 1])
        d2 = dx * dx + dy * dy
        along = self.cum[:-1] + t * self.seg_len
        if (near_along is not None and window_m is not None) or min_along is not None:
            mask = np.ones(len(d2), dtype=bool)
            if near_along is not None and window_m is not None:
                mask &= np.abs(along - near_along) <= window_m
            if min_along is not None:
                mask &= along >= min_along
            if mask.any():
                d2 = np.where(mask, d2, np.inf)
        i = int(d2.argmin())
        dist = math.sqrt(float(d2[i]))
        side = v[i, 0] * (py - a[i, 1]) - v[i, 1] * (px - a[i, 0])  # > 0 means left of travel
        return Projection(float(along[i]), -dist if side > 0 else dist, i, float(self.seg_bearing[i]))

    def project_many(self, lats, lons, near_along: float | None = None, window_m: float | None = None):
        """Vectorised projection of many points: returns (along_m, |cross_m|) arrays."""
        px = (np.asarray(lons, dtype=float) - self.proj.lon0)[:, None] * self.proj.kx
        py = (np.asarray(lats, dtype=float) - self.proj.lat0)[:, None] * self.proj.ky
        a = self.xy[:-1]
        v = self.seg_vec
        t = ((px - a[:, 0]) * v[:, 0] + (py - a[:, 1]) * v[:, 1]) / self._l2
        t = np.minimum(np.maximum(t, 0.0), 1.0)
        dx = px - (a[:, 0] + t * v[:, 0])
        dy = py - (a[:, 1] + t * v[:, 1])
        d2 = dx * dx + dy * dy
        along = self.cum[:-1] + t * self.seg_len
        if near_along is not None and window_m is not None:
            far = np.abs(along - near_along) > window_m
            d2 = np.where(far & ~far.all(axis=1, keepdims=True), np.inf, d2)
        i = d2.argmin(axis=1)
        rows = np.arange(len(i))
        return along[rows, i], np.sqrt(d2[rows, i])

    def segment_index(self, along_m: float) -> int:
        s = min(max(float(along_m), 0.0), self.length)
        i = int(np.searchsorted(self.cum, s, side="right") - 1)
        return min(max(i, 0), len(self.seg_len) - 1)

    def point_at(self, along_m: float) -> tuple[float, float]:
        """(lat, lon) at a distance along the shape."""
        s = min(max(float(along_m), 0.0), self.length)
        i = self.segment_index(s)
        f = (s - self.cum[i]) / self.seg_len[i] if self.seg_len[i] > 0 else 0.0
        x = self.xy[i, 0] + f * self.seg_vec[i, 0]
        y = self.xy[i, 1] + f * self.seg_vec[i, 1]
        return self.proj.lat0 + y / self.proj.ky, self.proj.lon0 + x / self.proj.kx

    def bearing_at(self, along_m: float) -> float:
        return float(self.seg_bearing[self.segment_index(along_m)])

    def alignment_with(self, axis_bearing_deg: float, start_m: float, end_m: float) -> float:
        """Length-weighted mean of cos(angle to an axis) between two distances along the shape.

        Used to turn a river-current speed (measured along the river axis) into the share that
        helps or hinders a vessel over the rest of its run: +1 = fully with the axis.
        """
        lo, hi = sorted((float(np.clip(start_m, 0, self.length)), float(np.clip(end_m, 0, self.length))))
        if hi - lo < 1.0:
            return float(math.cos(math.radians(self.bearing_at(lo) - axis_bearing_deg)))
        seg_lo = np.maximum(self.cum[:-1], lo)
        seg_hi = np.minimum(self.cum[1:], hi)
        w = np.clip(seg_hi - seg_lo, 0.0, None)
        c = np.cos(np.radians(self.seg_bearing - axis_bearing_deg))
        return float((w * c).sum() / w.sum())

    def lonlat_coords(self) -> list[list[float]]:
        """GeoJSON-ordered coordinates."""
        return [[round(float(lon), 6), round(float(lat), 6)] for lat, lon in self.latlon]


def path_length_m(coords: Iterable[Sequence[float]]) -> float:
    pts = np.asarray(list(coords), dtype=float)
    if len(pts) < 2:
        return 0.0
    return float(np.sum(haversine_m(pts[:-1, 0], pts[:-1, 1], pts[1:, 0], pts[1:, 1])))
