interface Props {
  now: number;
  p10: number;
  p50: number;
  p90: number;
  official: number | null;
  color: string;
  horizonMin?: number;
}

/** A tiny timeline: shaded p10–p90 band, a tick at the most likely time, a hollow marker for the official ETA. */
export default function EtaBar({ now, p10, p50, p90, official, color, horizonMin = 30 }: Props) {
  const pos = (ts: number) => `${Math.min(100, Math.max(0, ((ts - now) / 60 / horizonMin) * 100))}%`;
  const left = pos(p10);
  const width = `calc(${pos(p90)} - ${left})`;
  return (
    <div className="etabar" aria-hidden="true">
      <div className="etabar-track" />
      <div className="etabar-band" style={{ left, width, background: color }} />
      <div className="etabar-mid" style={{ left: pos(p50), background: color }} />
      {official != null && <div className="etabar-official" style={{ left: pos(official) }} title="Official prediction" />}
    </div>
  );
}
