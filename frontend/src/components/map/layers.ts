/**
 * GIS layer types and styling.
 *
 * The backend serves every layer as GeoJSON with a documented property set
 * (see apps/network/gis.py). This module holds the client's half of that
 * contract: the types, and how each layer is drawn.
 *
 * Styling lives here rather than in the components so that a layer's
 * appearance is decided in one place and cannot drift between the operations
 * map, the analytics map and anything added later.
 */
import type { LatLngExpression } from "leaflet";

export interface LayerSpec {
  name: string;
  title: string;
  description: string;
  geometry: "Point" | "LineString";
  public: boolean;
  is_heatmap: boolean;
  url: string;
}

export interface BasemapProvider {
  id: string;
  name: string;
  url: string;
  attribution: string;
  subdomains?: string;
  max_zoom: number;
  default: boolean;
  requires_key: boolean;
  is_traffic?: boolean;
}

export interface BasemapCatalogue {
  providers: BasemapProvider[];
  default: string;
  mapbox_available: boolean;
  google_maps_available: boolean;
  google_maps_note: string;
}

export interface GeoFeature<P = Record<string, unknown>> {
  type: "Feature";
  id?: number | string;
  geometry:
    | { type: "Point"; coordinates: [number, number] }
    | { type: "LineString"; coordinates: [number, number][] };
  properties: P;
}

export interface GeoCollection<P = Record<string, unknown>> {
  type: "FeatureCollection";
  features: GeoFeature<P>[];
  metadata: { generated_at: string; count: number; layer?: string; truncated?: boolean };
}

/** GeoJSON is [lon, lat]; Leaflet wants [lat, lon]. Converted here, once. */
export function toLatLng(coordinates: [number, number]): LatLngExpression {
  return [coordinates[1], coordinates[0]];
}

export function lineToLatLngs(coordinates: [number, number][]): LatLngExpression[] {
  return coordinates.map(toLatLng);
}

// ---------------------------------------------------------------------------
// Styling
// ---------------------------------------------------------------------------
export function congestionColour(index: number): string {
  if (index >= 0.8) return "#e74c3c";
  if (index >= 0.55) return "#e67e22";
  if (index >= 0.35) return "#f1c40f";
  if (index >= 0.15) return "#9acd32";
  return "#2ecc71";
}

const PRIORITY_COLOUR: Record<number, string> = {
  1: "#ff4d4f",
  2: "#ff9f43",
  3: "#ffd166",
  4: "#7f8c9b",
};

export function priorityColour(level: number | undefined): string {
  return PRIORITY_COLOUR[level ?? 4] ?? "#7f8c9b";
}

export interface PointStyle {
  colour: string;
  radius: number;
}

/** How a point feature is drawn, per layer. */
export function pointStyle(layer: string, properties: Record<string, unknown>): PointStyle {
  switch (layer) {
    case "hospitals":
      return {
        colour: properties.is_on_diversion ? "#e74c3c" : "#2ecc71",
        radius: properties.is_trauma_designated ? 9 : 7,
      };
    case "traffic_signals":
      // A held signal is the single most important thing on this map: it means
      // cross traffic is stopped right now.
      if (properties.is_preempted) return { colour: "#2ecc71", radius: 8 };
      return { colour: properties.is_online ? "#8b98a9" : "#e74c3c", radius: 4 };
    case "road_closures":
      return { colour: properties.blocks_road ? "#e74c3c" : "#ff9f43", radius: 7 };
    case "emergency_vehicles":
      return { colour: priorityColour(properties.priority_level as number), radius: 8 };
    case "display_boards":
      return { colour: properties.is_displaying_alert ? "#ffb300" : "#5a6472", radius: 5 };
    case "cameras":
      return { colour: "#4da3ff", radius: 4 };
    default:
      return { colour: "#4da3ff", radius: 5 };
  }
}

export function lineStyle(
  layer: string,
  properties: Record<string, unknown>,
): { color: string; weight: number; opacity: number; dashArray?: string } {
  if (layer === "emergency_routes") {
    return {
      color: priorityColour(properties.priority_level as number),
      weight: 5,
      opacity: 0.85,
      dashArray: "1 8",
    };
  }
  // road_network
  const open = properties.is_open !== false;
  return {
    color: open ? congestionColour((properties.congestion_index as number) ?? 0) : "#e74c3c",
    weight: open ? 2 : 4,
    opacity: 0.55,
  };
}

/** Popup text per layer. Kept beside the styling so both stay in step. */
export function describeFeature(layer: string, properties: Record<string, unknown>): string {
  const p = properties as Record<string, string | number | boolean | null>;
  switch (layer) {
    case "hospitals":
      return `<b>${p.name}</b><br>${p.code}${
        p.is_on_diversion ? "<br><b>ON DIVERSION</b>" : ""
      }<br>ED beds ${p.emergency_beds_available} · ICU ${p.icu_beds_available}`;
    case "traffic_signals":
      return `<b>${p.controller_id}</b><br>${p.intersection}<br>phase: ${p.current_phase}${
        p.is_preempted ? " (priority hold)" : ""
      }`;
    case "road_closures":
      return `<b>${p.event_type_display}</b><br>${p.description || "reported"}<br>severity ${Math.round(
        Number(p.severity) * 100,
      )}% · source ${p.source}`;
    case "emergency_vehicles":
      return `<b>${p.callsign}</b><br>${p.status} · ${Math.round(
        Number(p.speed_kmh),
      )} km/h<br>siren: ${p.siren_mode}`;
    case "emergency_routes":
      return `<b>${p.reference}</b><br>${p.vehicle} → ${p.hospital ?? "unassigned"}<br>${(
        Number(p.distance_m) / 1000
      ).toFixed(1)} km`;
    case "display_boards":
      return `<b>${p.code}</b><br>${p.message || "(blank)"}`;
    case "cameras":
      return `<b>${p.name}</b><br>last analysed ${p.last_analysed_at ?? "never"}`;
    case "road_network":
      return `<b>${p.name || "Road segment"}</b><br>${p.congestion_level} · ${
        p.speed_kmh ? `${Math.round(Number(p.speed_kmh))} km/h` : "no live speed"
      }`;
    default:
      return `<b>${p.name ?? layer}</b>`;
  }
}

/** Which layers a fresh operations map opens with. */
export const DEFAULT_ACTIVE_LAYERS = [
  "road_network",
  "hospitals",
  "traffic_signals",
  "road_closures",
  "emergency_routes",
  "emergency_vehicles",
] as const;
