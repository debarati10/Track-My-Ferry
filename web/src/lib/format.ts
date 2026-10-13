const timeFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "numeric",
  minute: "2-digit",
});

export function clockTime(ts: number): string {
  return timeFmt.format(new Date(ts * 1000));
}

export function minutesUntil(ts: number, now: number): number {
  return Math.max(0, (ts - now) / 60);
}

/** "Now", "3 min", or "6–8 min" from an uncertainty band. */
export function etaLabel(p10: number, p50: number, p90: number, now: number): string {
  const lo = Math.round(minutesUntil(p10, now));
  const mid = Math.round(minutesUntil(p50, now));
  const hi = Math.round(minutesUntil(p90, now));
  if (hi <= 0) return "Now";
  if (mid <= 0) return "Arriving";
  if (hi - lo <= 1) return `${mid} min`;
  return `${Math.max(lo, 0)}–${hi} min`;
}

export function deltaVsOfficial(p50: number, official: number | null): string | null {
  if (official == null) return null;
  const d = Math.round((p50 - official) / 60);
  if (Math.abs(d) < 1) return "same as official";
  return d > 0 ? `${d} min later than official` : `${-d} min earlier than official`;
}

export function delayLabel(delay: number | null): string {
  if (delay == null) return "—";
  const m = Math.round(delay / 60);
  if (Math.abs(m) < 1) return "On time";
  return m > 0 ? `${m} min late` : `${-m} min early`;
}

export function ago(ts: number | null, now: number): string {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)} min ago`;
}

export function seatChance(pFull: number | null): { label: string; level: "good" | "busy" | "full" } | null {
  if (pFull == null) return null;
  if (pFull < 0.05) return { label: "Plenty of room", level: "good" };
  if (pFull < 0.25) return { label: `${Math.round(pFull * 100)}% chance it's full`, level: "busy" };
  return { label: `${Math.round(pFull * 100)}% chance it's full`, level: "full" };
}
