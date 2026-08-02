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
import { useEffect, useMemo } from "react";
import { CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, useMap } from "react-leaflet";

import type { SegmentCollection, VehiclePayload } from "@/api/types";
import { congestionColour, levelClass } from "@/components/ui";

import "leaflet/dist/leaflet.css";

export const DEFAULT_CENTRE: [number, number] = [13.0604, 80.2496];
export const DEFAULT_ZOOM = 13;

const VEHICLE_GLYPH: Record<string, string> = {
  ambulance: "A",
  fire_engine: "F",
  police: "P",
  disaster: "D",
};

export function vehicleIcon(level: number, type: string): L.DivIcon {
  return L.divIcon({
    className: "",
    html: `<div class="veh-marker ${levelClass(level)}">${VEHICLE_GLYPH[type] ?? "E"}</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

export function MapCanvas({
  centre = DEFAULT_CENTRE,
  zoom = DEFAULT_ZOOM,
  children,
  className = "map",
}: {
  centre?: [number, number];
  zoom?: number;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <MapContainer center={centre} zoom={zoom} className={className} preferCanvas>
      <TileLayer
        url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        attribution="&copy; OpenStreetMap contributors &copy; CARTO"
        subdomains="abcd"
        maxZoom={20}
      />
      {children}
    </MapContainer>
  );
}

/** Pans the map when a vehicle is being followed. */
export function FollowVehicle({ position }: { position: [number, number] | null }) {
  const map = useMap();
  useEffect(() => {
    if (position) map.panTo(position, { animate: true });
  }, [map, position]);
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
}: {
  vehicles: VehiclePayload[];
  onSelect?: (callsign: string) => void;
}) {
  return (
    <>
      {vehicles.map((vehicle) => (
        <Marker
          key={vehicle.callsign}
          position={[vehicle.latitude, vehicle.longitude]}
          icon={vehicleIcon(vehicle.priority_level, vehicle.vehicle_type)}
          zIndexOffset={1000}
          eventHandlers={onSelect ? { click: () => onSelect(vehicle.callsign) } : undefined}
        >
          <Popup>
            <b>{vehicle.callsign}</b>
            <br />
            {vehicle.status} · {Math.round(vehicle.speed_kmh)} km/h
            <br />
            siren: {vehicle.siren_mode}
          </Popup>
        </Marker>
      ))}
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

export function RouteLine({ geometry }: { geometry: [number, number][] }) {
  if (geometry.length < 2) return null;
  return (
    <Polyline
      positions={geometry}
      pathOptions={{ color: "#4da3ff", weight: 5, opacity: 0.8, dashArray: "1 8" }}
    />
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

export function MapLegend() {
  return (
    <div className="legend map-legend">
      <div><span className="k" style={{ background: "#ff4d4f" }} />Level 1 critical</div>
      <div><span className="k" style={{ background: "#ff9f43" }} />Level 2 high</div>
      <div><span className="k" style={{ background: "#ffd166" }} />Level 3 moderate</div>
      <div><span className="k" style={{ background: "#7f8c9b" }} />Level 4 transport</div>
      <div><span className="k" style={{ background: "#2ecc71" }} />Signal held green</div>
      <div><span className="k" style={{ background: "#e74c3c" }} />Congested / blocked</div>
    </div>
  );
}
