import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MapLibreMap, MapMouseEvent } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection, Network, Selection, VesselState } from "../types";
import { ClockSync } from "../lib/clock";
import { MotionSmoother } from "../lib/motion";
import { loadBasemap, placeholderStyle, type MapTheme } from "../lib/mapStyle";
import { drawShip, parseShipId, SHIP_PIXEL_RATIO } from "../lib/shipIcon";

const HARBOR_CENTER: [number, number] = [-73.982, 40.715];
const FRAME_MS = 50;
const PULSE_MS = 1800;
const UNMATCHED = "#8E9AA6";
const HIT_LAYERS = ["vessel-hit", "vessel-icon", "stops"];
/** The same font stack CARTO's own labels use, so the glyphs are always on their font server. */
const FONT = ["Montserrat Medium", "Open Sans Bold", "Noto Sans Regular"];

interface Props {
  network: Network | null;
  getVessels: () => VesselState[];
  clock: ClockSync;
  selection: Selection;
  onSelect: (sel: Selection) => void;
  theme: MapTheme;
}

interface StaticData {
  routes: FeatureCollection;
  stops: FeatureCollection;
}

function routeColors(network: Network | null): Record<string, string> {
  const out: Record<string, string> = {};
  network?.routes.forEach((r) => (out[r.route_id] = r.color));
  return out;
}

/** Theme-dependent colors for everything Track My Ferry draws on top of the basemap. */
function overlayPalette(theme: MapTheme) {
  return theme === "city"
    ? { casing: "#ffffff", casingOpacity: 0.95, stopFill: "#ffffff", stopStroke: "#26323d", ring: "#14202b",
        text: "#1f2a35", halo: "#ffffff", selectFill: "rgba(255, 255, 255, 0.45)" }
    : { casing: "#05070a", casingOpacity: 0.55, stopFill: "#121519", stopStroke: "#e7eaed", ring: "#b9d9eb",
        text: "#e7eaed", halo: "#121519", selectFill: "rgba(185, 217, 235, 0.10)" };
}

/** Add Track My Ferry's sources and layers. Runs after every basemap (re)load, since a style swap clears them. */
function installOverlay(map: MapLibreMap, theme: MapTheme) {
  if (map.getLayer("route-casing")) return;
  const p = overlayPalette(theme);
  const empty = { type: "FeatureCollection" as const, features: [] };
  for (const id of ["routes", "stops", "vessels"]) {
    if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: empty });
  }
  // route lines go above streets, bridges and buildings but under the basemap's place names;
  // stops and boats go on top of everything
  const base = map.getStyle()?.layers ?? [];
  let lastShape = -1;
  base.forEach((l, i) => {
    if (l.type !== "symbol") lastShape = i;
  });
  const firstLabel = base.slice(lastShape + 1).find((l) => l.type === "symbol")?.id;
  map.addLayer({ id: "route-casing", type: "line", source: "routes",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": p.casing, "line-opacity": p.casingOpacity,
      "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4, 13, 7, 16, 12] } }, firstLabel);
  map.addLayer({ id: "route-line", type: "line", source: "routes",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-opacity": 0.95,
      "line-width": ["interpolate", ["linear"], ["zoom"], 10, 2, 13, 3.5, 16, 6.5] } }, firstLabel);

  map.addLayer({ id: "stops", type: "circle", source: "stops",
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 3.5, 13, 5, 16, 8],
      "circle-color": p.stopFill, "circle-stroke-color": p.stopStroke,
      "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 11, 1.5, 15, 2.5] } });
  map.addLayer({ id: "stop-selected", type: "circle", source: "stops", filter: ["==", ["get", "stop_id"], ""],
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 9, 16, 14], "circle-color": "rgba(0, 0, 0, 0)",
      "circle-stroke-color": p.ring, "circle-stroke-width": 2.5 } });
  map.addLayer({ id: "stop-labels", type: "symbol", source: "stops", minzoom: 12.4,
    layout: { "text-field": ["get", "name"], "text-size": 11.5, "text-offset": [0, 1.2], "text-anchor": "top",
      "text-font": FONT, "text-max-width": 9 },
    paint: { "text-color": p.text, "text-halo-color": p.halo, "text-halo-width": 1.6 } });

  map.addLayer({ id: "vessel-pulse", type: "circle", source: "vessels", filter: ["==", ["get", "mmsi"], -1],
    paint: { "circle-radius": 14, "circle-color": "rgba(0, 0, 0, 0)", "circle-stroke-color": ["get", "color"],
      "circle-stroke-width": 2, "circle-stroke-opacity": 0.8 } });
  map.addLayer({ id: "vessel-selected", type: "circle", source: "vessels", filter: ["==", ["get", "mmsi"], -1],
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 13, 16, 22], "circle-color": p.selectFill,
      "circle-stroke-color": ["get", "color"], "circle-stroke-width": 2.5 } });
  // invisible, generous click target so small boats are easy to tap
  map.addLayer({ id: "vessel-hit", type: "circle", source: "vessels",
    paint: { "circle-radius": 16, "circle-color": "#000000", "circle-opacity": 0 } });
  map.addLayer({ id: "vessel-icon", type: "symbol", source: "vessels",
    layout: {
      "icon-image": ["concat", "ship:", ["get", "color"], ":", ["case", ["get", "docked"], "d", "m"]],
      "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.5, 12, 0.78, 14, 1, 16, 1.4],
      "icon-rotate": ["get", "cog"], "icon-rotation-alignment": "map", "icon-pitch-alignment": "map",
      "icon-allow-overlap": true, "icon-ignore-placement": true,
    },
    paint: { "icon-opacity": ["case", ["get", "docked"], 0.92, 1] } });
  map.addLayer({ id: "vessel-label", type: "symbol", source: "vessels", minzoom: 13.2,
    layout: { "text-field": ["get", "name"], "text-size": 10.5, "text-offset": [0, 2], "text-anchor": "top",
      "text-font": FONT, "text-allow-overlap": false },
    paint: { "text-color": p.text, "text-halo-color": p.halo, "text-halo-width": 1.5 } });
}

export default function MapView({ network, getVessels, clock, selection, onSelect, theme }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const ready = useRef(false);
  const onSelectRef = useRef(onSelect);
  const getVesselsRef = useRef(getVessels);
  const colorsRef = useRef<Record<string, string>>({});
  const dataRef = useRef<StaticData | null>(null);
  const selectionRef = useRef<Selection>(selection);
  const themeRef = useRef<MapTheme>(theme);
  onSelectRef.current = onSelect;
  getVesselsRef.current = getVessels;

  const applyStatic = (map: MapLibreMap) => {
    const d = dataRef.current;
    if (!d) return;
    (map.getSource("routes") as GeoJSONSource | undefined)?.setData(d.routes as never);
    (map.getSource("stops") as GeoJSONSource | undefined)?.setData(d.stops as never);
  };

  const applySelection = (map: MapLibreMap) => {
    const sel = selectionRef.current;
    const vessel = sel?.kind === "vessel" ? sel.id : -1;
    map.setFilter("stop-selected", ["==", ["get", "stop_id"], sel?.kind === "stop" ? sel.id : ""]);
    map.setFilter("vessel-selected", ["==", ["get", "mmsi"], vessel]);
    map.setFilter("vessel-pulse", ["==", ["get", "mmsi"], vessel]);
  };

  // create the map once
  useEffect(() => {
    if (!container.current) return;
    const map = new maplibregl.Map({
      container: container.current,
      style: placeholderStyle(themeRef.current),
      center: HARBOR_CENTER,
      zoom: 12.2,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    const smoother = new MotionSmoother();
    let raf = 0;
    let lastFrame = performance.now();
    let lastPush = 0;

    // ship icons are drawn the first time a color is needed (and again after a style swap)
    map.on("styleimagemissing", (e) => {
      const ship = parseShipId(e.id);
      if (ship && !map.hasImage(e.id)) {
        map.addImage(e.id, drawShip(ship.color, ship.moving), { pixelRatio: SHIP_PIXEL_RATIO });
      }
    });

    map.on("style.load", () => {
      installOverlay(map, themeRef.current);
      applyStatic(map);
      applySelection(map);
      ready.current = true;
    });

    const featuresAt = (e: MapMouseEvent) => {
      const layers = HIT_LAYERS.filter((id) => map.getLayer(id));
      if (!ready.current || !layers.length) return [];
      const { x, y } = e.point;
      return map.queryRenderedFeatures([[x - 4, y - 4], [x + 4, y + 4]], { layers });
    };
    map.on("click", (e) => {
      const f = featuresAt(e)[0];
      if (!f) return;
      if (f.layer.id === "stops") onSelectRef.current({ kind: "stop", id: String(f.properties?.stop_id) });
      else onSelectRef.current({ kind: "vessel", id: Number(f.properties?.mmsi) });
    });
    map.on("mousemove", (e) => {
      map.getCanvas().style.cursor = featuresAt(e).length ? "pointer" : "";
    });

    const frame = (t: number) => {
      raf = requestAnimationFrame(frame);
      if (!ready.current || t - lastPush < FRAME_MS) return;
      const dt = (t - lastFrame) / 1000;
      lastFrame = lastPush = t;
      const now = clock.now();
      const colors = colorsRef.current;
      const features = getVesselsRef.current().map((v) => {
        const [lon, lat] = smoother.step(v, now, dt);
        return {
          type: "Feature" as const,
          geometry: { type: "Point" as const, coordinates: [lon, lat] },
          properties: {
            mmsi: v.mmsi, name: v.name ?? String(v.mmsi), cog: v.cog ?? 0, docked: v.status === "docked",
            color: (v.route_id && colors[v.route_id]) || UNMATCHED,
          },
        };
      });
      (map.getSource("vessels") as GeoJSONSource | undefined)?.setData({ type: "FeatureCollection", features });
      if (selectionRef.current?.kind === "vessel" && map.getLayer("vessel-pulse")) {
        const phase = (t % PULSE_MS) / PULSE_MS;
        map.setPaintProperty("vessel-pulse", "circle-radius", 12 + phase * 22);
        map.setPaintProperty("vessel-pulse", "circle-stroke-opacity", 0.85 * (1 - phase));
      }
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      map.remove();
      mapRef.current = null;
      ready.current = false;
    };
  }, [clock]);

  // load (or switch) the basemap; the overlay is re-added on the next style.load
  useEffect(() => {
    themeRef.current = theme;
    const map = mapRef.current;
    if (!map) return;
    let current = true;
    loadBasemap(theme).then((style) => {
      if (!current || mapRef.current !== map) return;
      // offline: keep the plain placeholder so boats and routes still show
      if (typeof style === "string") return;
      ready.current = false;
      map.setStyle(style, { diff: false });
    });
    return () => {
      current = false;
    };
  }, [theme]);

  // static layers once the network arrives
  useEffect(() => {
    const map = mapRef.current;
    if (!network) return;
    colorsRef.current = routeColors(network);
    dataRef.current = {
      routes: { type: "FeatureCollection", features: network.geojson.features.filter((f) => f.properties.kind === "route") },
      stops: { type: "FeatureCollection", features: network.geojson.features.filter((f) => f.properties.kind === "stop") },
    };
    if (map && ready.current) applyStatic(map);
  }, [network]);

  // highlight the selection
  useEffect(() => {
    selectionRef.current = selection;
    const map = mapRef.current;
    if (map && ready.current) applySelection(map);
  }, [selection]);

  return <div ref={container} className="map" role="application" aria-label="Live map of NYC Ferry boats" />;
}
