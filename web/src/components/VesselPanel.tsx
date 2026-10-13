import type { Network, Prediction, VesselState } from "../types";
import { clockTime, delayLabel, etaLabel, seatChance } from "../lib/format";

interface Props {
  vessel: VesselState | undefined;
  predictions: Prediction[];
  network: Network | null;
  now: number;
  onSelectStop: (stopId: string) => void;
}

export default function VesselPanel({ vessel, predictions, network, now, onSelectStop }: Props) {
  if (!vessel) {
    return (
      <section className="panel-section">
        <p className="muted">This boat isn't reporting right now.</p>
      </section>
    );
  }
  const route = network?.routes.find((r) => r.route_id === vessel.route_id);
  const stopName = (id: string | null) =>
    (network?.geojson.features.find((f) => f.properties.kind === "stop" && f.properties.stop_id === id)?.properties
      .name as string | undefined) ?? id ?? "—";
  const status =
    vessel.status === "docked" ? `Docked at ${stopName(vessel.current_stop_id)}` :
    vessel.status === "underway" ? `Heading to ${stopName(vessel.next_stop_id)}` : "Not on a scheduled trip";
  const upcoming = [...predictions].sort((a, b) => a.eta_p50 - b.eta_p50);

  return (
    <section className="panel-section" aria-labelledby="vessel-title">
      <p className="eyebrow">Boat</p>
      <h2 id="vessel-title">{vessel.name ?? `Vessel ${vessel.mmsi}`}</h2>
      {route && (
        <span className="route-chip" style={{ borderColor: route.color, color: route.color }}>
          {route.name}
        </span>
      )}
      <p className="status-line">{status}</p>
      <dl className="facts">
        <div><dt>Speed</dt><dd className="num">{vessel.sog_kn != null ? `${vessel.sog_kn.toFixed(1)} kn` : "—"}</dd></div>
        <div><dt>Schedule</dt><dd>{delayLabel(vessel.delay_s)}</dd></div>
        <div><dt>Trip match</dt><dd className="num">{vessel.match_confidence != null ? `${Math.round(vessel.match_confidence * 100)}%` : "—"}</dd></div>
        <div><dt>Last fix</dt><dd className="num">{clockTime(vessel.ts)}</dd></div>
      </dl>

      <h3 className="sub">Upcoming stops</h3>
      {upcoming.length === 0 && <p className="muted">No predictions for this boat yet.</p>}
      <ol className="stops-list">
        {upcoming.map((p) => {
          const seat = seatChance(p.p_full);
          return (
            <li key={p.stop_id}>
              <button className="stop-row" onClick={() => onSelectStop(p.stop_id)}>
                <span className="stop-name">{stopName(p.stop_id)}</span>
                <span className="stop-eta">{etaLabel(p.eta_p10, p.eta_p50, p.eta_p90, now)}</span>
                <span className="stop-sub">
                  about {clockTime(p.eta_p50)}
                  {p.official_eta != null && ` · official ${clockTime(p.official_eta)}`}
                  {seat && seat.level !== "good" && ` · ${seat.label}`}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
