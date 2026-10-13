import type { Map as MapLibreMap } from "maplibre-gl";

/**
 * Basemaps. Both come from CARTO's free vector tiles (OpenStreetMap data); Track My Ferry fetches the
 * hosted style once and recolors it by layer before handing it to MapLibre, so the city look
 * needs no tile server or API key of its own.
 */
export type MapTheme = "city" | "night";
export type StyleSpec = Exclude<Parameters<MapLibreMap["setStyle"]>[0], string | null>;

type Paint = Record<string, unknown>;
type Rule = [RegExp, Paint];

const CARTO = "https://basemaps.cartocdn.com/gl";
export const GLYPHS = "https://tiles.basemaps.cartocdn.com/fonts/{fontstack}/{range}.pbf";

/** City palette: warm paper land, light blue water, green parks, building footprints, pale cream highways. */
const CITY = {
  land: "#f4f1ea",
  urban: "#ede9e1",
  park: "#c8e6bb",
  water: "#a6d3f5",
  waterText: "#2e6c9f",
  waterHalo: "rgba(214, 236, 251, 0.85)",
  buildingBase: "#d9d1c4",
  building: "#e8e2d7",
  buildingLine: "#d3cabb",
  // highways stay pale so the saturated route colors are the only strong lines on the map
  motorway: "#fff6dc",
  motorwayCase: "#e2d1a6",
  trunk: "#fffaf0",
  trunkCase: "#e0d6c2",
  major: "#ffffff",
  majorCase: "#d5cdbf",
  street: "#ffffff",
  streetCase: "#ddd6c9",
  tunnel: "#f6f3ee",
  tunnelMajor: "#f8f2e4",
  path: "#d9d1c3",
  rail: "#bfb9af",
  railDash: "#f4f1ea",
  aeroway: "#e2e0ea",
  text: "#36404a",
  textSoft: "#6c7680",
  roadText: "#5b636b",
  halo: "rgba(255, 255, 255, 0.92)",
  parkText: "#3b7748",
};

const NIGHT = {
  land: "#0e1013",
  water: "#132c42",
  waterText: "#6f98bd",
  waterHalo: "rgba(10, 22, 34, 0.8)",
};

const CITY_RULES: Rule[] = [
  [/^background$/, { "background-color": CITY.land }],
  [/^landuse_residential$/, { "fill-color": CITY.urban }],
  [/^(landcover|park_national_park|park_nature_reserve|landuse)$/,
    { "fill-color": CITY.park, "fill-opacity": ["interpolate", ["linear"], ["zoom"], 8, 0.55, 12, 1] }],
  [/^water$/, { "fill-color": CITY.water }],
  [/^water_shadow$/, { "fill-color": "rgba(0, 0, 0, 0)" }],
  [/^waterway$/, { "line-color": CITY.water }],
  [/^aeroway-/, { "line-color": CITY.aeroway }],
  // roads: casing then fill, minor → major so the more specific rule wins
  [/_case/, { "line-color": CITY.streetCase }],
  [/_fill/, { "line-color": CITY.street }],
  [/_(pri|sec)_case/, { "line-color": CITY.majorCase }],
  [/_(pri|sec)_fill/, { "line-color": CITY.major }],
  [/_trunk_case/, { "line-color": CITY.trunkCase }],
  [/_trunk_fill/, { "line-color": CITY.trunk }],
  [/_mot_case/, { "line-color": CITY.motorwayCase }],
  [/_mot_fill/, { "line-color": CITY.motorway }],
  [/^tunnel_.*_fill/, { "line-color": CITY.tunnel }],
  [/^tunnel_(mot|trunk)_fill/, { "line-color": CITY.tunnelMajor }],
  [/^(road|tunnel|bridge)_path$/, { "line-color": CITY.path }],
  [/^(tunnel_)?rail$/, { "line-color": CITY.rail }],
  [/^(tunnel_)?rail_dash$/, { "line-color": CITY.railDash }],
  [/^building$/, { "fill-color": CITY.buildingBase }],
  [/^building-top$/, { "fill-color": CITY.building, "fill-outline-color": CITY.buildingLine }],
  // labels
  [/^place_/, { "text-color": CITY.text, "text-halo-color": CITY.halo }],
  [/^place_(hamlet|suburbs|villages)$/, { "text-color": CITY.textSoft }],
  [/^roadname_/, { "text-color": CITY.roadText, "text-halo-color": CITY.halo }],
  [/^(watername_|waterway_label)/, { "text-color": CITY.waterText, "text-halo-color": CITY.waterHalo }],
  [/^poi_park$/, { "text-color": CITY.parkText, "text-halo-color": "rgba(238, 246, 234, 0.9)" }],
  [/^poi_stadium$/, { "text-color": CITY.textSoft, "text-halo-color": CITY.halo }],
];

const NIGHT_RULES: Rule[] = [
  [/^water$/, { "fill-color": NIGHT.water }],
  [/^water_shadow$/, { "fill-color": "rgba(0, 0, 0, 0)" }],
  [/^waterway$/, { "line-color": NIGHT.water }],
  [/^(watername_|waterway_label)/, { "text-color": NIGHT.waterText, "text-halo-color": NIGHT.waterHalo }],
];

/** Fallbacks for layers a future CARTO style might add: color them by what they draw. */
const SOURCE_LAYER_RULES: Record<MapTheme, Record<string, Paint>> = {
  city: {
    "water/fill": { "fill-color": CITY.water },
    "waterway/line": { "line-color": CITY.water },
    "park/fill": { "fill-color": CITY.park },
  },
  night: {
    "water/fill": { "fill-color": NIGHT.water },
    "waterway/line": { "line-color": NIGHT.water },
  },
};

export const THEMES: Record<MapTheme, { label: string; url: string; land: string; water: string; rules: Rule[] }> = {
  city: {
    label: "City",
    url: import.meta.env.VITE_MAP_STYLE ?? `${CARTO}/voyager-gl-style/style.json`,
    land: CITY.land,
    water: CITY.water,
    rules: CITY_RULES,
  },
  night: {
    label: "Night",
    url: `${CARTO}/dark-matter-gl-style/style.json`,
    land: NIGHT.land,
    water: NIGHT.water,
    rules: NIGHT_RULES,
  },
};

const PREFIX: Record<string, string[]> = {
  background: ["background-"],
  fill: ["fill-"],
  line: ["line-"],
  symbol: ["text-", "icon-"],
  circle: ["circle-"],
};

/** Keep only paint keys that belong to this layer type (a `fill-*` key on a line layer would be invalid). */
function forType(type: string, paint: Paint): Paint {
  const prefixes = PREFIX[type];
  if (!prefixes) return {};
  const out: Paint = {};
  for (const [k, v] of Object.entries(paint)) {
    if (prefixes.some((p) => k.startsWith(p)) && !k.startsWith("fill-extrusion-")) out[k] = v;
  }
  return out;
}

/** Recolor a CARTO style. Exported for tests; unknown layers are left as they are. */
export function restyle(style: StyleSpec, theme: MapTheme): StyleSpec {
  const rules = THEMES[theme].rules;
  const bySource = SOURCE_LAYER_RULES[theme];
  const layers = style.layers.map((layer) => {
    const l = layer as unknown as { id: string; type: string; "source-layer"?: string; paint?: Paint };
    let paint: Paint = {};
    let matched = false;
    for (const [re, p] of rules) {
      if (re.test(l.id)) {
        paint = { ...paint, ...p };
        matched = true;
      }
    }
    if (!matched) {
      const fallback = bySource[`${l["source-layer"] ?? ""}/${l.type}`];
      if (fallback) paint = fallback;
    }
    const valid = forType(l.type, paint);
    if (!Object.keys(valid).length) return layer;
    return { ...l, paint: { ...(l.paint ?? {}), ...valid } } as unknown as typeof layer;
  });
  return { ...style, layers };
}

/** A bare style with the theme's land color, shown for the moment before the basemap arrives. */
export function placeholderStyle(theme: MapTheme): StyleSpec {
  return {
    version: 8,
    glyphs: GLYPHS,
    sources: {},
    layers: [{ id: "background", type: "background", paint: { "background-color": THEMES[theme].land } }],
  };
}

const cache = new Map<MapTheme, Promise<StyleSpec | string>>();

/** Fetch and recolor the theme's basemap; falls back to the untouched URL if the fetch fails. */
export function loadBasemap(theme: MapTheme): Promise<StyleSpec | string> {
  let p = cache.get(theme);
  if (!p) {
    const url = THEMES[theme].url;
    p = fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`basemap ${r.status}`);
        return r.json() as Promise<StyleSpec>;
      })
      .then((s) => restyle(s, theme))
      .catch(() => {
        cache.delete(theme);
        return url;
      });
    cache.set(theme, p);
  }
  return p;
}

const STORAGE_KEY = "trackmyferry.mapTheme";

export function savedTheme(): MapTheme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "city" || v === "night") return v;
  } catch {
    /* storage unavailable */
  }
  return "city";
}

export function saveTheme(theme: MapTheme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* storage unavailable */
  }
}
