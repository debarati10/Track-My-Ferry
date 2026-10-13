import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import MapView from "./components/MapView";
import StopPanel from "./components/StopPanel";
import VesselPanel from "./components/VesselPanel";
import Overview from "./components/Overview";
import type { Health, LiveMessage, ModelInfo, Network, Prediction, Selection, VesselState } from "./types";
import { getJSON } from "./lib/api";
import { ClockSync } from "./lib/clock";
import { LiveConnection, type ConnectionStatus } from "./lib/live";
import { ago, clockTime } from "./lib/format";
import { savedTheme, saveTheme, THEMES, type MapTheme } from "./lib/mapStyle";

const THEME_ICONS: Record<MapTheme, ReactElement> = {
  city: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M2 14V7l4-2v9M6 14V3l5 2v9M11 14V8l3 1.5V14M1 14h14" fill="none" stroke="currentColor"
        strokeWidth="1.4" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  ),
  night: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M13.5 9.8A5.8 5.8 0 0 1 6.2 2.5a5.8 5.8 0 1 0 7.3 7.3Z" fill="none" stroke="currentColor"
        strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  ),
};

const STALE_S = 900;

export default function App() {
  const [network, setNetwork] = useState<Network | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [model, setModel] = useState<ModelInfo | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [selection, setSelection] = useState<Selection>(null);
  const [mapTheme, setMapTheme] = useState<MapTheme>(savedTheme);
  const [, setTick] = useState(0);
  const vessels = useRef(new Map<number, VesselState>());
  const predictions = useRef<Record<string, Prediction[]>>({});
  const lastData = useRef<number | null>(null);
  const clock = useMemo(() => new ClockSync(), []);

  useEffect(() => {
    getJSON<Network>("/api/network").then(setNetwork).catch(() => setNetwork(null));
    getJSON<Health>("/api/health").then(setHealth).catch(() => undefined);
    getJSON<ModelInfo>("/api/model").then(setModel).catch(() => undefined);

    const onMessage = (msg: LiveMessage) => {
      if (msg.type === "snapshot") {
        vessels.current = new Map(msg.vessels.map((v) => [v.mmsi, v]));
        predictions.current = msg.predictions;
        clock.observe(msg.ts);
        lastData.current = msg.ts;
      } else if (msg.type === "vessels") {
        let newest = 0;
        for (const v of msg.vessels) {
          vessels.current.set(v.mmsi, v);
          newest = Math.max(newest, v.ts);
        }
        if (newest) {
          clock.observe(newest);
          lastData.current = newest;
        }
      } else if (msg.type === "predictions") {
        predictions.current = { ...predictions.current, ...msg.predictions };
      }
    };
    const conn = new LiveConnection(onMessage, setStatus);
    conn.connect();
    const tick = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => {
      conn.close();
      window.clearInterval(tick);
    };
  }, [clock]);

  const now = clock.now();
  const getVessels = useCallback(() => {
    const t = clock.now();
    return [...vessels.current.values()].filter((v) => t - v.ts < STALE_S);
  }, [clock]);
  const fresh = getVessels();
  const selectStop = (id: string) => setSelection({ kind: "stop", id });
  const selectVessel = (id: number) => setSelection({ kind: "vessel", id });
  const isSim = health?.mode === "sim";
  const chooseTheme = (t: MapTheme) => {
    setMapTheme(t);
    saveTheme(t);
  };

  return (
    <div className="app" data-map={mapTheme}>
      <MapView network={network} getVessels={getVessels} clock={clock} selection={selection} onSelect={setSelection}
        theme={mapTheme} />

      <div className="map-theme" role="group" aria-label="Map style">
        {(Object.keys(THEMES) as MapTheme[]).map((t) => (
          <button key={t} type="button" aria-pressed={mapTheme === t} onClick={() => chooseTheme(t)}>
            {THEME_ICONS[t]}
            {THEMES[t].label}
          </button>
        ))}
      </div>

      <aside className="panel" aria-label="Track My Ferry details">
        <header className="panel-head">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true" />
            <div>
              <h1>Track My Ferry</h1>
              <p className="tagline">Live NYC Ferry arrivals</p>
            </div>
          </div>
          <div className="status-row">
            <span className={`pill pill-${status}`}>
              <i aria-hidden="true" />
              {status === "live" ? "Live" : status === "connecting" ? "Connecting" : "Reconnecting"}
            </span>
            <span className="muted small num">
              {fresh.length} boats · {lastData.current ? `${clockTime(now)} · updated ${ago(lastData.current, now)}` : "waiting for data"}
            </span>
          </div>
          {isSim && (
            <p className="sim-note">
              Demo mode: simulated boats on a sample network{clock.speed > 1.5 ? `, running ${Math.round(clock.speed)}× faster` : ""}.
            </p>
          )}
        </header>

        <div className="panel-body">
          {selection && (
            <button className="back" onClick={() => setSelection(null)}>
              ← All routes
            </button>
          )}
          {selection?.kind === "stop" && (
            <StopPanel stopId={selection.id} network={network} now={now} onSelectVessel={selectVessel} />
          )}
          {selection?.kind === "vessel" && (
            <VesselPanel
              vessel={vessels.current.get(selection.id)}
              predictions={predictions.current[String(selection.id)] ?? []}
              network={network}
              now={now}
              onSelectStop={selectStop}
            />
          )}
          {!selection && <Overview network={network} vessels={fresh} model={model} onSelectStop={selectStop} />}
        </div>

        <footer className="panel-foot muted small">
          Model {model?.version ?? health?.model ?? "…"} · positions via AIS · schedule via NYC Ferry GTFS
        </footer>
      </aside>
    </div>
  );
}
