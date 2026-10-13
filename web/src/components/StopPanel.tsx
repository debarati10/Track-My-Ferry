import { useEffect, useState } from "react";
import type { Network, StopArrivals } from "../types";
import { getJSON } from "../lib/api";
import { clockTime, deltaVsOfficial, etaLabel, seatChance } from "../lib/format";
import EtaBar from "./EtaBar";

interface Props {
  stopId: string;
  network: Network | null;
  now: number;
  onSelectVessel: (mmsi: number) => void;
}

export default function StopPanel({ stopId, network, now, onSelectVessel }: Props) {
  const [data, setData] = useState<StopArrivals | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    const ctrl = new AbortController();
    const load = () =>
      getJSON<StopArrivals>(`/api/stops/${encodeURIComponent(stopId)}/arrivals?limit=6`, ctrl.signal)
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e: Error) => e.name !== "AbortError" && setError("Couldn't load arrivals. Retrying…"));
    load();
    const id = window.setInterval(load, 5000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [stopId]);

  const stopFeature = network?.geojson.features.find((f) => f.properties.kind === "stop" && f.properties.stop_id === stopId);
  const name = data?.stop.name ?? (stopFeature?.properties.name as string | undefined) ?? stopId;
  const routes = (stopFeature?.properties.routes as string[] | undefined) ?? [];
  const colorOf = (rid: string) => network?.routes.find((r) => r.route_id === rid)?.color ?? "#9BA5AE";

  return (
    <section className="panel-section" aria-labelledby="stop-title">
      <p className="eyebrow">Landing</p>
      <h2 id="stop-title">{name}</h2>
      <div className="chips">
        {routes.map((r) => (
          <span key={r} className="route-chip" style={{ borderColor: colorOf(r), color: colorOf(r) }}>
            {network?.routes.find((x) => x.route_id === r)?.short_name ?? r}
          </span>
        ))}
      </div>

      <h3 className="sub">Next boats</h3>
      {error && <p className="muted">{error}</p>}
      {!data && !error && <p className="muted">Loading arrivals…</p>}
      {data && data.arrivals.length === 0 && (
        <p className="muted">No boats are heading here in the next hour. Service may have ended for the day.</p>
      )}
      <ul className="arrivals">
        {data?.arrivals.map((a) => {
          const color = a.route_color ?? "#B9D9EB";
          const seat = seatChance(a.p_full);
          const delta = deltaVsOfficial(a.eta_p50, a.official_eta);
          return (
            <li key={`${a.trip_id}-${a.mmsi}`}>
              <button className="arrival" onClick={() => onSelectVessel(a.mmsi)}>
                <span className="arrival-top">
                  <span className="route-dot" style={{ background: color }} />
                  <span className="arrival-route">{a.route_name}</span>
                  <span className="arrival-eta">{etaLabel(a.eta_p10, a.eta_p50, a.eta_p90, now)}</span>
                </span>
                <span className="arrival-meta">
                  {a.terminates ? `Ends here, from ${a.origin ?? "—"}` : `to ${a.headsign ?? "—"}`} ·{" "}
                  {a.vessel_name ?? `Vessel ${a.mmsi}`} · about {clockTime(a.eta_p50)}
                </span>
                <EtaBar now={now} p10={a.eta_p10} p50={a.eta_p50} p90={a.eta_p90} official={a.official_eta} color={color} />
                <span className="arrival-foot">
                  {delta && <span className="muted">{delta}</span>}
                  {seat && <span className={`seat seat-${seat.level}`}>{seat.label}</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {data && data.scheduled_departures.length > 0 && (
        <>
          <h3 className="sub">Later departures (timetable)</h3>
          <ul className="timetable">
            {data.scheduled_departures.map((d) => (
              <li key={d.trip_id}>
                <span className="route-dot" style={{ background: d.route_color ?? "#9BA5AE" }} />
                <span className="tt-name">{d.route_name} to {d.headsign ?? "—"}</span>
                <span className="num">{clockTime(d.departure_ts)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="legend-note">
        Shaded band: 80% of arrivals fall in this range. Hollow marker: NYC Ferry's own prediction.
      </p>
    </section>
  );
}
