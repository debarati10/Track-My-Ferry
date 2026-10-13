import type { VesselState } from "../types";

const KNOT_MS = 0.514444;
const M_PER_DEG_LAT = 111_320;
const MAX_EXTRAPOLATE_S = 30;

/** Where the boat should be now: last fix + speed x heading x elapsed time (dead reckoning). */
export function deadReckon(v: VesselState, dataNow: number): [number, number] {
  if (v.status !== "underway" || !v.sog_kn || v.sog_kn < 1 || v.cog == null) return [v.lon, v.lat];
  const dt = Math.min(Math.max(dataNow - v.ts, 0), MAX_EXTRAPOLATE_S);
  const d = v.sog_kn * KNOT_MS * dt;
  const rad = (v.cog * Math.PI) / 180;
  const lat = v.lat + (d * Math.cos(rad)) / M_PER_DEG_LAT;
  const lon = v.lon + (d * Math.sin(rad)) / (M_PER_DEG_LAT * Math.cos((v.lat * Math.PI) / 180));
  return [lon, lat];
}

/**
 * Smooths what's drawn: each frame, move the displayed point part of the way toward the
 * dead-reckoned target, so new fixes glide in instead of jumping.
 */
export class MotionSmoother {
  private shown = new Map<number, [number, number]>();

  step(v: VesselState, dataNow: number, dtFrameS: number): [number, number] {
    const target = deadReckon(v, dataNow);
    const prev = this.shown.get(v.mmsi);
    if (!prev) {
      this.shown.set(v.mmsi, target);
      return target;
    }
    const jumpDeg = Math.hypot(target[0] - prev[0], target[1] - prev[1]);
    if (jumpDeg > 0.01) {
      // > ~1 km: a gap in the data, don't animate across land
      this.shown.set(v.mmsi, target);
      return target;
    }
    const k = 1 - Math.exp(-dtFrameS / 0.5);
    const next: [number, number] = [prev[0] + (target[0] - prev[0]) * k, prev[1] + (target[1] - prev[1]) * k];
    this.shown.set(v.mmsi, next);
    return next;
  }

  forget(mmsi: number): void {
    this.shown.delete(mmsi);
  }
}
