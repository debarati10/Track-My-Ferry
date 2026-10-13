export type VesselStatus = "underway" | "docked" | "unassigned";

export interface VesselState {
  mmsi: number;
  ts: number;
  lat: number;
  lon: number;
  name: string | null;
  sog_kn: number | null;
  cog: number | null;
  heading: number | null;
  status: VesselStatus;
  route_id: string | null;
  direction_id: number | null;
  trip_id: string | null;
  pattern_id: string | null;
  along_m: number | null;
  next_stop_id: string | null;
  current_stop_id: string | null;
  delay_s: number | null;
  match_confidence: number | null;
}

export interface Prediction {
  made_at: number;
  mmsi: number;
  trip_id: string;
  route_id: string;
  direction_id: number;
  stop_id: string;
  stops_ahead: number;
  eta_p10: number;
  eta_p50: number;
  eta_p90: number;
  official_eta: number | null;
  scheduled_eta: number | null;
  vessel_name: string | null;
  p_full: number | null;
  model_version: string;
}

export interface Arrival extends Prediction {
  route_name: string;
  route_color: string | null;
  headsign: string | null;
  origin: string | null;
  terminates: boolean;
  minutes: number;
  minutes_range: [number, number];
}

export interface ScheduledDeparture {
  trip_id: string;
  route_id: string;
  route_name: string;
  route_color: string | null;
  headsign: string | null;
  departure_ts: number;
}

export interface StopInfo {
  stop_id: string;
  name: string;
  lat: number;
  lon: number;
}

export interface StopArrivals {
  stop: StopInfo;
  ts: number;
  arrivals: Arrival[];
  scheduled_departures: ScheduledDeparture[];
}

export interface RouteInfo {
  route_id: string;
  short_name: string;
  name: string;
  color: string;
}

export interface Feature {
  type: "Feature";
  geometry: { type: "LineString"; coordinates: number[][] } | { type: "Point"; coordinates: number[] };
  properties: Record<string, unknown>;
}

export interface FeatureCollection {
  type: "FeatureCollection";
  features: Feature[];
}

export interface Network {
  routes: RouteInfo[];
  geojson: FeatureCollection;
}

export interface Health {
  status: string;
  mode: "sim" | "live";
  vessels: number;
  data_ts: number | null;
  model: string;
  crowding: boolean;
}

export interface ModelInfo {
  version: string;
  evaluation: {
    mae_s: Record<string, number>;
    interval: { coverage_p10_p90: number; mean_width_s: number };
    improvement_vs_official_pct: number | null;
    source: string;
  } | null;
}

export type LiveMessage =
  | { type: "snapshot"; ts: number; vessels: VesselState[]; predictions: Record<string, Prediction[]> }
  | { type: "vessels"; vessels: VesselState[] }
  | { type: "predictions"; predictions: Record<string, Prediction[]> };

export type Selection = { kind: "stop"; id: string } | { kind: "vessel"; id: number } | null;
