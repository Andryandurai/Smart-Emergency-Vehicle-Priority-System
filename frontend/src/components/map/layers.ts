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
/**
 * Traffic colouring, Google-Maps convention.
 *
 * Three bands, not five. The point of a traffic layer is a decision - "can I
 * take this road" - and five shades of green-to-red makes an operator read the
 * legend to answer it. Blue is deliberately the free-flow colour rather than
 * green: green on this map already means "signal held for a corridor", and two
 * meanings for one colour on a map an operator scans under pressure is the
 * kind of ambiguity that costs seconds.
 *
 * ``congestion_index`` is 0 (free flow) to 1 (standstill), derived from the
 * current/free-flow speed ratio - the thresholds mirror CongestionLevel in
 * apps/core/enums.py so the colour and the word always agree.
 */
export const TRAFFIC_FREE = "#4285f4";     // blue      - clear run
export const TRAFFIC_SLIGHT = "#fbbc04";   // yellow    - slowing
export const TRAFFIC_HEAVY = "#8c0d1c";    // dark red  - heavy / standstill
// Purple, not a darker red. A closure is a different kind of fact from slow
// traffic - you cannot drive it at all - and the earlier dark-red-on-red was
// unreadable against a heavy segment at speed.
export const TRAFFIC_BLOCKED = "#a259ff";  // purple    - impassable

export type TrafficBand = "free" | "slight" | "heavy";

/**
 * Collapse the backend's five congestion levels into the three bands the map
 * draws. Derived from the *level* rather than the raw index wherever the
 * server sent one, so the colour on the map and the word in the popup can
 * never disagree - they did, before this: the index thresholds here had
 * drifted from CongestionLevel.from_ratio in apps/core/enums.py, and a
 * segment labelled "heavy" was being drawn amber.
 */
const BAND_BY_LEVEL: Record<string, TrafficBand> = {
  free: "free",
  light: "free",
  moderate: "slight",
  heavy: "heavy",
  jam: "heavy",
};

/** Index boundaries, mirroring `CongestionLevel.from_ratio` (index = 1 - ratio). */
export function trafficBand(index: number, level?: unknown): TrafficBand {
  const named = BAND_BY_LEVEL[String(level ?? "")];
  if (named) return named;
  if (index >= 0.55) return "heavy";   // ratio < 0.45 -> heavy or jam
  if (index >= 0.35) return "slight";  // ratio < 0.65 -> moderate
  return "free";                       // free or light
}

export function congestionColour(index: number, level?: unknown): string {
  const band = trafficBand(index, level);
  if (band === "heavy") return TRAFFIC_HEAVY;
  if (band === "slight") return TRAFFIC_SLIGHT;
  return TRAFFIC_FREE;
}

/** Heavier stroke for worse traffic, so the layer reads without colour. */
export function congestionWeight(index: number, level?: unknown): number {
  const band = trafficBand(index, level);
  if (band === "heavy") return 4.5;
  if (band === "slight") return 3.5;
  return 2.5;
}

/** Live aspect colour for a signal head. Mirrors SignalPhase in enums.py. */
export const SIGNAL_PHASE_COLOUR: Record<string, string> = {
  red: "#ff3b30",
  amber: "#ffab00",
  green: "#34c759",
  flashing_amber: "#ffab00",
  off: "#6b7683",
};

export function signalPhaseColour(phase: unknown): string {
  return SIGNAL_PHASE_COLOUR[String(phase ?? "off")] ?? SIGNAL_PHASE_COLOUR.off!;
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
      // Drawn as the aspect it is actually showing. A preempted signal is
      // green by definition, and gets a larger radius plus a halo in the
      // marker so "held for a corridor" is distinguishable from "green in
      // its normal cycle" - both matter, and they are not the same fact.
      if (!properties.is_online) return { colour: "#6b7683", radius: 4 };
      return {
        colour: signalPhaseColour(properties.current_phase),
        radius: properties.is_preempted ? 8 : 5,
      };
    case "road_closures":
      return { colour: properties.blocks_road ? TRAFFIC_BLOCKED : "#ff9f43", radius: 7 };
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
  if (!open) {
    return { color: TRAFFIC_BLOCKED, weight: 5, opacity: 0.9, dashArray: "6 5" };
  }
  const index = (properties.congestion_index as number) ?? 0;
  const level = properties.congestion_level;
  return {
    color: congestionColour(index, level),
    weight: congestionWeight(index, level),
    // Congested roads are the ones worth seeing, so opacity rises with the
    // index instead of being flat - a clear network recedes into the basemap.
    opacity: trafficBand(index, level) === "free" ? 0.5 : 0.85,
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
      return `<b>${p.intersection}</b><br>${p.controller_id}<br>Aspect: <b>${String(
        p.current_phase,
      ).replaceAll("_", " ")}</b>${
        p.is_preempted ? "<br><b>HELD GREEN — emergency corridor</b>" : ""
      }${p.is_online ? "" : "<br><b>OFFLINE</b>"}`;
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
