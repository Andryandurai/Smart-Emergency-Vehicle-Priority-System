/**
 * Leaflet primitives shared by every map on the console.
 *
 * Coordinate convention worth stating once: SEVPS APIs return `[lat, lon]`
 * everywhere except the GeoJSON endpoints, which follow the spec and return
 * `[lon, lat]`. Leaflet wants `[lat, lon]`. `SegmentsLayer` is the only place
 * that swaps, and it is the only place that should.
 */
import L from "leaflet";
import type { ReactNode } from "react";
import { useEffect, useMemo, useRef } from "react";
import {
  CircleMarker,
  MapContainer,
  Marker,
  Polyline,
  Popup,
  TileLayer,
  Tooltip,
  useMap,
} from "react-leaflet";

import type { SegmentCollection, Trip, VehiclePayload } from "@/api/types";
import { congestionColour, levelClass } from "@/components/ui";
import {
  TRAFFIC_BLOCKED,
  TRAFFIC_FREE,
  TRAFFIC_HEAVY,
  TRAFFIC_SLIGHT,
  signalPhaseColour,
} from "@/components/map/layers";

import "leaflet/dist/leaflet.css";

export const DEFAULT_CENTRE: [number, number] = [13.0604, 80.2496];
export const DEFAULT_ZOOM = 13;

const VEHICLE_GLYPH: Record<string, string> = {
  ambulance: "A",
  disaster: "D",
};

export function vehicleIcon(
  level: number,
  type: string,
  { heading = 0, focused = false }: { heading?: number; focused?: boolean } = {},
): L.DivIcon {
  // The heading arrow is a sibling of the badge rather than a rotation of it,
  // so the glyph stays upright and readable while the arrow points where the
  // vehicle is actually going.
  return L.divIcon({
    className: "",
    html:
      `<div class="veh-pin${focused ? " focused" : ""}">` +
      `<span class="veh-heading" style="transform: rotate(${heading}deg)"></span>` +
      `<span class="veh-marker ${levelClass(level)}">${VEHICLE_GLYPH[type] ?? "E"}</span>` +
      `</div>`,
    iconSize: [34, 34],
    iconAnchor: [17, 17],
  });
}

/**
 * Hospital pin.
 *
 * A teardrop pin rather than a dot: hospitals are destinations an operator
 * picks, and a pin reads as "a place you go" where a dot reads as "a reading
 * taken here". It also survives being drawn on top of the road network, which
 * a 7px circle does not.
 */
export function hospitalIcon(
  { onDiversion = false, isTrauma = false }: { onDiversion?: boolean; isTrauma?: boolean } = {},
): L.DivIcon {
  const tone = onDiversion ? "diverted" : "open";
  return L.divIcon({
    className: "",
    html:
      `<div class="hosp-pin ${tone}${isTrauma ? " trauma" : ""}">` +
      `<span class="hosp-glyph">${onDiversion ? "⊘" : "⚕"}</span>` +
      `</div>`,
    iconSize: [26, 34],
    iconAnchor: [13, 34],
    popupAnchor: [0, -30],
  });
}

/**
 * Traffic signal head, drawn as the three-lamp housing with the live aspect
 * lit. Colour alone would be ambiguous against the congestion layer, which
 * also uses red and amber; the housing shape is what makes a signal a signal.
 */
export function signalIcon(
  phase: string,
  { preempted = false, online = true }: { preempted?: boolean; online?: boolean } = {},
): L.DivIcon {
  const lamps = (["red", "amber", "green"] as const)
    .map((lamp) => {
      const lit =
        online &&
        (lamp === phase || (phase === "flashing_amber" && lamp === "amber"));
      return `<i class="lamp ${lamp}${lit ? " lit" : ""}"></i>`;
    })
    .join("");
  return L.divIcon({
    className: "",
    html:
      `<div class="sig-head${preempted ? " preempted" : ""}${online ? "" : " offline"}"` +
      ` style="--aspect:${signalPhaseColour(online ? phase : "off")}">${lamps}</div>`,
    iconSize: [12, 28],
    iconAnchor: [6, 14],
    popupAnchor: [0, -14],
  });
}

/**
 * Road disruption: a hazard triangle, the sign every road user already knows.
 *
 * ``blocking`` distinguishes "the road is shut" from "something is slowing it
 * down" - the two demand different decisions from a dispatcher and used to be
 * two shades of the same orange dot.
 */
export function disruptionIcon(
  { blocking = false, highlighted = false }: { blocking?: boolean; highlighted?: boolean } = {},
): L.DivIcon {
  return L.divIcon({
    className: "",
    html:
      `<div class="warn-pin${blocking ? " blocking" : ""}${highlighted ? " lit" : ""}">` +
      `<span class="warn-glyph">!</span></div>`,
    iconSize: [26, 24],
    iconAnchor: [13, 20],
    popupAnchor: [0, -18],
  });
}

/** Variable message sign, drawn as the screen it is. */
export function boardIcon({ alerting = false }: { alerting?: boolean } = {}): L.DivIcon {
  return L.divIcon({
    className: "",
    html:
      `<div class="board-pin${alerting ? " alerting" : ""}">` +
      `<span class="board-screen"></span><span class="board-stand"></span></div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 22],
    popupAnchor: [0, -20],
  });
}

/** CCTV camera: body, lens and mount bracket. */
export function cameraIcon({ active = true }: { active?: boolean } = {}): L.DivIcon {
  return L.divIcon({
    className: "",
    html:
      `<div class="cam-pin${active ? "" : " idle"}">` +
      `<span class="cam-body"><i class="cam-lens"></i></span><span class="cam-mount"></span></div>`,
    iconSize: [24, 20],
    iconAnchor: [12, 16],
    popupAnchor: [0, -14],
  });
}

/** Fallback used before the basemap catalogue has loaded, and if it fails.
 *  OpenStreetMap through CARTO needs no key: an emergency platform should not
 *  depend on a commercial tile contract to draw a map. */
const FALLBACK_BASEMAP = {
  url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
  subdomains: "abcd",
  max_zoom: 20,
};

export function MapCanvas({
  centre = DEFAULT_CENTRE,
  zoom = DEFAULT_ZOOM,
  children,
  className = "map",
  basemap,
}: {
  centre?: [number, number];
  zoom?: number;
  children?: ReactNode;
  className?: string;
  basemap?: { url: string; attribution: string; subdomains?: string; max_zoom: number } | null;
}) {
  const tiles = basemap ?? FALLBACK_BASEMAP;
  return (
    <MapContainer
      center={centre}
      zoom={zoom}
      className={className}
      // Canvas rather than SVG: the seeded network is ~4,000 polylines, and an
      // SVG path per segment makes the browser re-layout the whole overlay on
      // every pan.
      preferCanvas
      // Pan/zoom feel. Leaflet's defaults are tuned for a document with a map
      // in it; this is a console where the map IS the document, so inertia is
      // shortened (a flick should stop where you let go, not coast past the
      // junction you were aiming at) and wheel zoom is made continuous rather
      // than one-step-per-notch.
      zoomControl
      inertia
      inertiaDeceleration={2600}
      inertiaMaxSpeed={2400}
      easeLinearity={0.28}
      wheelDebounceTime={24}
      wheelPxPerZoomLevel={110}
      zoomSnap={0.5}
      zoomDelta={0.5}
      zoomAnimation
      markerZoomAnimation={false}
      maxZoom={19}
    >
      <MapResizeGuard />
      <TileLayer
        // Keyed on the URL so switching provider replaces the layer rather
        // than mutating it - Leaflet caches tiles per layer instance.
        key={tiles.url}
        url={tiles.url}
        attribution={tiles.attribution}
        {...(tiles.subdomains ? { subdomains: tiles.subdomains } : {})}
        maxZoom={tiles.max_zoom}
      />
      {children}
    </MapContainer>
  );
}

/**
 * Keeps Leaflet's cached container size honest.
 *
 * Leaflet measures its container once on init. The console mounts the map
 * inside a CSS grid that settles a frame later, and the sidebar can change
 * width - after which every pointer event is offset from where it looks like
 * it landed, so dragging "sticks" and zoom recentres on the wrong point. This
 * was the actual cause of the map feeling unresponsive to drags.
 */
function MapResizeGuard() {
  const map = useMap();
  useEffect(() => {
    const container = map.getContainer();
    const invalidate = () => map.invalidateSize({ animate: false });

    // Settle after the first paint, when the grid has resolved.
    const raf = window.requestAnimationFrame(invalidate);
    const observer = new ResizeObserver(invalidate);
    observer.observe(container);
    window.addEventListener("resize", invalidate);

    return () => {
      window.cancelAnimationFrame(raf);
      observer.disconnect();
      window.removeEventListener("resize", invalidate);
    };
  }, [map]);
  return null;
}

/**
 * Pans the map when a vehicle is being followed.
 *
 * Deliberately yields to the operator: while a drag or zoom is in progress,
 * and for a moment afterwards, following is suspended. Otherwise every
 * telemetry fix yanks the viewport back and the map cannot be explored at all
 * — which is what "the map doesn't move where I drag it" actually was.
 */
export function FollowVehicle({
  position,
  enabled = true,
}: {
  position: [number, number] | null;
  enabled?: boolean;
}) {
  const map = useMap();
  const interacting = useRef(0);

  useEffect(() => {
    const touch = () => {
      interacting.current = Date.now();
    };
    map.on("dragstart", touch);
    map.on("drag", touch);
    map.on("zoomstart", touch);
    return () => {
      map.off("dragstart", touch);
      map.off("drag", touch);
      map.off("zoomstart", touch);
    };
  }, [map]);

  useEffect(() => {
    if (!enabled || !position) return;
    if (Date.now() - interacting.current < 6000) return;
    // Only recentre once the vehicle is genuinely drifting out of view;
    // panning on every fix makes the whole map twitch.
    if (map.getBounds().pad(-0.25).contains(position)) return;
    map.panTo(position, { animate: true, duration: 0.6 });
  }, [map, position, enabled]);

  return null;
}

export function FitBounds({ points }: { points: [number, number][] }) {
  const map = useMap();
  useEffect(() => {
    if (points.length > 1) {
      map.fitBounds(L.latLngBounds(points), { padding: [40, 40] });
    }
  }, [map, points]);
  return null;
}

export function VehicleMarkers({
  vehicles,
  onSelect,
  focusedCallsign = null,
  /** Trip keyed by callsign, so the hover card can name the emergency and the
   *  receiving hospital - neither of which the fleet payload knows about. */
  tripsByCallsign,
}: {
  vehicles: VehiclePayload[];
  onSelect?: (callsign: string) => void;
  focusedCallsign?: string | null;
  tripsByCallsign?: Record<string, Trip>;
}) {
  return (
    <>
      {vehicles.map((vehicle) => {
        const trip = tripsByCallsign?.[vehicle.callsign];
        const focused = focusedCallsign === vehicle.callsign;
        return (
          <Marker
            key={vehicle.callsign}
            position={[vehicle.latitude, vehicle.longitude]}
            icon={vehicleIcon(vehicle.priority_level, vehicle.vehicle_type, {
              heading: vehicle.heading_deg,
              focused,
            })}
            zIndexOffset={focused ? 2000 : 1000}
            eventHandlers={onSelect ? { click: () => onSelect(vehicle.callsign) } : undefined}
          >
            {/* Hover card. `sticky` follows the pointer so it never covers the
                vehicle it describes, which matters when several are close. */}
            <Tooltip direction="top" offset={[0, -16]} opacity={1} sticky className="veh-tip">
              <div className="veh-tip-body">
                <div className="veh-tip-head">
                  <b>{vehicle.callsign}</b>
                  <span className={`veh-tip-level ${levelClass(vehicle.priority_level)}`}>
                    L{vehicle.priority_level}
                  </span>
                </div>
                {vehicle.registration && (
                  <div className="veh-tip-row mono">{vehicle.registration}</div>
                )}
                <div className="veh-tip-row">
                  <span className="k">Speed</span>
                  <b>{Math.round(vehicle.speed_kmh)} km/h</b>
                </div>
                <div className="veh-tip-row">
                  <span className="k">Emergency</span>
                  <b>{trip?.category_display || trip?.emergency_category || "—"}</b>
                </div>
                <div className="veh-tip-row">
                  <span className="k">Destination</span>
                  <b>{trip?.hospital_name ?? "not yet assigned"}</b>
                </div>
                <div className="veh-tip-row">
                  <span className="k">Status</span>
                  <b>{trip?.stage_display || vehicle.status_display || vehicle.status}</b>
                </div>
              </div>
            </Tooltip>
          </Marker>
        );
      })}
    </>
  );
}

/**
 * Road network coloured by congestion.
 *
 * Memoised on the feature count: the seeded network is ~4,000 polylines and
 * re-projecting them on every vehicle tick would dominate the frame budget.
 */
export function SegmentsLayer({ collection }: { collection: SegmentCollection | null }) {
  const lines = useMemo(() => {
    if (!collection) return [];
    return collection.features.map((feature) => ({
      id: feature.properties.id,
      // GeoJSON is [lon, lat]; Leaflet wants [lat, lon].
      positions: feature.geometry.coordinates.map(
        ([lon, lat]) => [lat, lon] as [number, number],
      ),
      colour: feature.properties.is_open
        ? congestionColour(feature.properties.congestion_index)
        : "#e74c3c",
      weight: feature.properties.is_open ? 2 : 4,
      name: feature.properties.name,
      level: feature.properties.congestion_level,
      speed: feature.properties.speed_kmh,
    }));
  }, [collection]);

  return (
    <>
      {lines.map((line) => (
        <Polyline
          key={line.id}
          positions={line.positions}
          pathOptions={{ color: line.colour, weight: line.weight, opacity: 0.55 }}
        >
          <Popup>
            <b>{line.name || "Road segment"}</b>
            <br />
            {line.level} · {line.speed ? `${Math.round(line.speed)} km/h` : "no live speed"}
          </Popup>
        </Polyline>
      ))}
    </>
  );
}

/** Vibrant blue for a selected vehicle's route - reads over dark red and purple. */
export const ROUTE_BLUE = "#00b0ff";

/**
 * The active route, drawn Google-style: a solid casing under a blue dotted
 * line. The casing is what makes the dots legible over both the dark basemap
 * and a red congested road - a bare dashed line disappears over either.
 */
export function RouteLine({
  geometry,
  colour = ROUTE_BLUE,
}: {
  geometry: [number, number][];
  colour?: string;
}) {
  if (geometry.length < 2) return null;
  return (
    <>
      <Polyline
        positions={geometry}
        pathOptions={{ color: "#0b1a2b", weight: 11, opacity: 0.75, lineCap: "round" }}
      />
      <Polyline
        positions={geometry}
        pathOptions={{
          color: colour,
          weight: 6,
          opacity: 0.95,
          dashArray: "1 11",
          lineCap: "round",
        }}
      />
    </>
  );
}

/** Start/end caps for a focused route, so its extent is unambiguous. */
export function RouteEndpoints({
  geometry,
  destinationLabel,
}: {
  geometry: [number, number][];
  destinationLabel?: string | null;
}) {
  if (geometry.length < 2) return null;
  const end = geometry[geometry.length - 1]!;
  return (
    <CircleMarker
      center={end}
      radius={6}
      pathOptions={{ color: "#ffffff", fillColor: "#4da3ff", fillOpacity: 1, weight: 2 }}
    >
      {destinationLabel && <Popup>{destinationLabel}</Popup>}
    </CircleMarker>
  );
}

/** A hospital drawn as a pin. See {@link hospitalIcon}. */
export function HospitalPin({
  position,
  name,
  detail,
  onDiversion = false,
  isTrauma = false,
  onClick,
}: {
  position: [number, number];
  name: string;
  detail?: ReactNode;
  onDiversion?: boolean;
  isTrauma?: boolean;
  onClick?: () => void;
}) {
  return (
    <Marker
      position={position}
      icon={hospitalIcon({ onDiversion, isTrauma })}
      zIndexOffset={500}
      eventHandlers={onClick ? { click: onClick } : undefined}
    >
      <Tooltip direction="top" offset={[0, -34]} opacity={1} className="veh-tip">
        <div className="veh-tip-body">
          <div className="veh-tip-head">
            <b>{name}</b>
          </div>
          {detail}
        </div>
      </Tooltip>
    </Marker>
  );
}

export function Dot({
  position,
  colour,
  radius = 6,
  children,
}: {
  position: [number, number];
  colour: string;
  radius?: number;
  children?: ReactNode;
}) {
  return (
    <CircleMarker
      center={position}
      radius={radius}
      pathOptions={{ color: colour, fillColor: colour, fillOpacity: 0.9, weight: 1 }}
    >
      {children && <Popup>{children}</Popup>}
    </CircleMarker>
  );
}

/**
 * Flies to a disruption and rings it, for "click it in the list, find it on
 * the map". The ring is drawn imperatively so it sits above every layer
 * regardless of which are switched on.
 */
export function DisruptionSpotlight({
  event,
  onClear,
}: {
  event: { id: number; latitude: number; longitude: number; event_type_display?: string; description?: string; blocks_road?: boolean };
  onClear?: () => void;
}) {
  const map = useMap();

  useEffect(() => {
    const position: [number, number] = [event.latitude, event.longitude];
    // Only zoom in if the operator is currently further out - yanking a
    // close-in view back to z16 loses the detail they were looking at.
    map.flyTo(position, Math.max(map.getZoom(), 16), { duration: 0.8 });

    // An explicit SVG renderer, because the map runs with `preferCanvas` for
    // the 4,000-segment road layer - and a canvas-rendered circle has no DOM
    // node, so `className` never lands anywhere and the pulse cannot animate.
    const ring = L.circleMarker(position, {
      renderer: L.svg(),
      radius: 26,
      color: event.blocks_road ? "#a259ff" : "#ff9f43",
      weight: 3,
      fill: false,
      className: "spotlight-ring",
      interactive: false,
    }).addTo(map);

    return () => {
      ring.remove();
    };
  }, [map, event.id, event.latitude, event.longitude, event.blocks_road]);

  useEffect(() => {
    if (!onClear) return;
    // Clicking bare map dismisses the spotlight, the way any selection should.
    map.on("click", onClear);
    return () => {
      map.off("click", onClear);
    };
  }, [map, onClear]);

  return null;
}

export function AlertCircle({
  position,
  radiusM,
  message,
}: {
  position: [number, number];
  radiusM: number;
  message: string;
}) {
  const map = useMap();
  useEffect(() => {
    const circle = L.circle(position, {
      radius: radiusM,
      color: "#ff9f43",
      weight: 1,
      fillOpacity: 0.1,
    })
      .bindPopup(message)
      .addTo(map);
    return () => {
      circle.remove();
    };
  }, [map, position, radiusM, message]);
  return null;
}

/**
 * Map key.
 *
 * Grouped by what the symbol *is* rather than by colour, because the same
 * colour legitimately means different things in different groups - red is a
 * jammed road, a stopped signal and a level-1 vehicle, and an operator needs
 * the shape to disambiguate. Each group states its own shape.
 */
export function MapLegend() {
  return (
    <div className="legend map-legend">
      <div className="legend-group">
        <h5>Traffic flow</h5>
        <div><span className="k bar" style={{ background: TRAFFIC_FREE }} />Clear</div>
        <div><span className="k bar" style={{ background: TRAFFIC_SLIGHT }} />Slow</div>
        <div><span className="k bar" style={{ background: TRAFFIC_HEAVY }} />Heavy</div>
        <div><span className="k bar" style={{ background: TRAFFIC_BLOCKED }} />Road closed</div>
      </div>

      <div className="legend-group">
        <h5>Signals</h5>
        <div><span className="k lamp" style={{ background: "#ff3b30" }} />Red</div>
        <div><span className="k lamp" style={{ background: "#ffab00" }} />Amber</div>
        <div><span className="k lamp" style={{ background: "#34c759" }} />Green</div>
        <div><span className="k lamp haloed" style={{ background: "#34c759" }} />Held for corridor</div>
      </div>

      <div className="legend-group">
        <h5>Ambulances</h5>
        <div><span className="k round" style={{ background: "#ff4d4f" }} />L1 critical</div>
        <div><span className="k round" style={{ background: "#ff9f43" }} />L2 high</div>
        <div><span className="k round" style={{ background: "#ffd166" }} />L3 moderate</div>
        <div><span className="k round" style={{ background: "#7f8c9b" }} />L4 transport</div>
      </div>

      <div className="legend-group">
        <h5>Places &amp; markers</h5>
        <div><span className="k pin" style={{ background: "#2ecc71" }} />Hospital</div>
        <div><span className="k pin" style={{ background: "#e74c3c" }} />On diversion</div>
        <div><span className="k warn" />Disruption</div>
        <div><span className="k route" />Selected route</div>
      </div>
    </div>
  );
}
