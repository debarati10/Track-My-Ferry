import type { ModelInfo, Network, VesselState } from "../types";

interface Props {
  network: Network | null;
  vessels: VesselState[];
  model: ModelInfo | null;
  onSelectStop: (stopId: string) => void;
}

const POPULAR = ["Wall St/Pier 11", "East 34th Street", "Dumbo/Fulton Ferry", "North Williamsburg"];

export default function Overview({ network, vessels, model, onSelectStop }: Props) {
  const counts: Record<string, number> = {};
  vessels.forEach((v) => v.route_id && (counts[v.route_id] = (counts[v.route_id] ?? 0) + 1));
  const stops = network?.geojson.features.filter((f) => f.properties.kind === "stop") ?? [];
  const quick = POPULAR.map((n) => stops.find((s) => s.properties.name === n)).filter(Boolean);
  const ev = model?.evaluation;

  return (
    <section className="panel-section">
      <p className="lede">Tap a landing to see when the next boats arrive, or tap a boat to follow it.</p>
      <h3 className="sub">Routes</h3>
      <ul className="route-list">
        {network?.routes.map((r) => (
          <li key={r.route_id}>
            <span className="route-dot" style={{ background: r.color }} />
            <span>{r.name}</span>
            <span className="num muted">{counts[r.route_id] ?? 0} boats</span>
          </li>
        ))}
      </ul>
      {quick.length > 0 && (
        <>
          <h3 className="sub">Popular landings</h3>
          <div className="chips">
            {quick.map((s) => (
              <button key={String(s!.properties.stop_id)} className="chip-btn" onClick={() => onSelectStop(String(s!.properties.stop_id))}>
                {String(s!.properties.name)}
              </button>
            ))}
          </div>
        </>
      )}
      {ev && (
        <>
          <h3 className="sub">How accurate is it?</h3>
          <div className="accuracy">
            <div>
              <span className="big num">{(ev.mae_s.model / 60).toFixed(1)}</span>
              <span className="unit">min average error</span>
            </div>
            <div>
              <span className="big num muted">{(ev.mae_s.official / 60).toFixed(1)}</span>
              <span className="unit">NYC Ferry's own prediction</span>
            </div>
          </div>
          <p className="muted small">
            The time range shown covers the real arrival {Math.round(ev.interval.coverage_p10_p90 * 100)}% of the time.
            {ev.source === "simulated" && " Measured on simulated test days."}
          </p>
        </>
      )}
    </section>
  );
}
