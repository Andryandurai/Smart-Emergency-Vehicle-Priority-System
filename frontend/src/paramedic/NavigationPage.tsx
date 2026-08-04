/**
 * Paramedic navigation.
 *
 * The same first-person view the driver has, not the control room's city
 * plan: heading-up optional, the ambulance low in the frame, the route
 * running ahead of it. A paramedic in the back needs to know how long they
 * have with this patient before the doors open, which junctions are being
 * held, and whether the road ahead has gone bad - not where every other
 * ambulance in Chennai is.
 *
 * Reuses the driver console's map primitives rather than re-implementing
 * them, so the two screens cannot drift apart.
 */
import { useEffect, useState } from "react";
import { Marker } from "react-leaflet";

import { dispatch as dispatchApi, shifts as shiftApi } from "@/api/endpoints";
import type { CrewShift, Preemption, Trip, VehiclePayload } from "@/api/types";
import { MapCanvas, ROUTE_BLUE, RouteLine, vehicleIcon } from "@/components/MapCanvas";
import { GisLayer } from "@/components/map/GisLayer";
import { TrafficLayer } from "@/components/map/TrafficLayer";
import { ChaseCamera, HeadingRotation } from "@/components/driver/NavMap";
import { SignalStrip, type UpcomingSignal } from "@/components/driver/SignalStrip";
import { fmtDistance, fmtEta } from "@/components/ui";
import { useGisLayers } from "@/hooks/useGisLayers";
import { useJourneyTick } from "@/hooks/useJourneyTick";
import { useSocket } from "@/hooks/useSocket";

export function NavigationPage() {
  const [shift, setShift] = useState<CrewShift | null>(null);
  const [vehicle, setVehicle] = useState<VehiclePayload | null>(null);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [corridor, setCorridor] = useState<Preemption[]>([]);
  const [route, setRoute] = useState<[number, number][]>([]);
  const [northUp, setNorthUp] = useState(true);
  const gis = useGisLayers();

  useEffect(() => {
    shiftApi
      .mine()
      .then((mine) => setShift(mine.shift))
      .catch(() => setShift(null));
  }, []);

  const callsign = shift?.vehicle_callsign ?? "";

  useSocket(callsign ? `/ws/vehicle/${encodeURIComponent(callsign)}/` : "", {
    handlers: {
      snapshot: (data) => {
        const snap = data as {
          vehicle: VehiclePayload;
          trip: Trip | null;
          corridor: Preemption[];
          route: { geometry: [number, number][] } | null;
        };
        setVehicle(snap.vehicle);
        setTrip(snap.trip);
        setCorridor(snap.corridor ?? []);
        if (snap.route?.geometry) setRoute(snap.route.geometry);
      },
      vehicle_position: (data) => setVehicle(data as VehiclePayload),
      route_updated: (data) =>
        setRoute((data as { geometry: [number, number][] }).geometry),
      trip_stage: () => {
        if (callsign) {
          void dispatchApi.tripsForVehicle(callsign).then((t) => setTrip(t[0] ?? null));
        }
      },
    },
  });

  // The same sweep the driver's console runs. Both crew are in one ambulance
  // and must watch one journey, so both drive it through the server rather
  // than each interpolating a position of their own.
  useJourneyTick(shift?.status === "active" && Boolean(trip));

  if (!shift || shift.status !== "active") {
    return (
      <div className="pm-page">
        <div className="pm-hero idle">
          <span className="pm-hero-tag">No shift</span>
          <h1>Navigation</h1>
          <p>Start your shift to see the route.</p>
        </div>
      </div>
    );
  }

  const position: [number, number] | null = vehicle
    ? [vehicle.latitude, vehicle.longitude]
    : null;
  const heading = vehicle?.heading_deg ?? 0;

  const upcoming: UpcomingSignal[] = position
    ? corridor
        .filter((row) => ["planned", "armed", "active", "failed"].includes(row.state))
        .map((row) => ({
          preemption: row,
          distanceM: haversine(position, [row.latitude, row.longitude]),
          secondsToGreen: (new Date(row.planned_green_at).getTime() - Date.now()) / 1000,
        }))
        .sort((a, b) => a.distanceM - b.distanceM)
        .slice(0, 4)
    : [];

  return (
    <div className="pm-nav">
      <div className="pm-nav-map">
        <MapCanvas centre={position ?? undefined} zoom={16} className="map nav-map">
          <TrafficLayer collection={gis.data.road_network ?? null} />
          <GisLayer layer="traffic_signals" collection={gis.data.traffic_signals ?? null} />
          <GisLayer layer="road_closures" collection={gis.data.road_closures ?? null} />
          <GisLayer layer="hospitals" collection={gis.data.hospitals ?? null} />
          <RouteLine geometry={route} colour={ROUTE_BLUE} />
          {vehicle && (
            <Marker
              position={[vehicle.latitude, vehicle.longitude]}
              icon={vehicleIcon(vehicle.priority_level, vehicle.vehicle_type, {
                heading: 0,
                focused: true,
              })}
              zIndexOffset={3000}
            />
          )}
          <HeadingRotation heading={heading} enabled={!northUp} />
          <ChaseCamera
            position={position}
            heading={northUp ? 0 : heading}
            lookAheadPx={northUp ? 0 : 150}
          />
        </MapCanvas>

        <div className="pm-nav-hud">
          <div className="pm-nav-speed">
            <b>{Math.round(vehicle?.speed_kmh ?? 0)}</b>
            <span>km/h</span>
          </div>
          <div className="pm-nav-mid">
            <div className="pm-nav-dest">
              {trip?.hospital_name ?? "No destination set"}
            </div>
            <div className="pm-nav-stage">{trip?.stage_display ?? "Standing by"}</div>
          </div>
          {trip && (
            <div className="pm-nav-eta">
              <b>{fmtEta(trip.eta)}</b>
              <span>{fmtDistance(trip.distance_remaining_m)}</span>
            </div>
          )}
        </div>

        <button
          type="button"
          className="nav-orient"
          onClick={() => setNorthUp((v) => !v)}
          title={northUp ? "Rotate with heading" : "Lock north up"}
        >
          {northUp ? "N↑" : "◈"}
        </button>
      </div>

      <div className="pm-nav-side">
        <div className={`pm-corridor ${corridorTone(corridor)}`}>
          <span className="k">Green corridor</span>
          <b>{corridorSummary(corridor)}</b>
        </div>
        <SignalStrip signals={upcoming} />
        <div className="nav-panel">
          <h4>Road ahead</h4>
          <p className="nav-empty">
            {(gis.data.road_closures?.metadata.count ?? 0) > 0
              ? `${gis.data.road_closures?.metadata.count} disruption(s) on the network. ` +
                `The route re-plans automatically when one blocks the way.`
              : "Network clear."}
          </p>
        </div>
      </div>
    </div>
  );
}

function haversine(a: [number, number], b: [number, number]): number {
  const R = 6_371_000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b[0] - a[0]);
  const dLon = toRad(b[1] - a[1]);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.sin(dLon / 2) ** 2 * Math.cos(toRad(a[0])) * Math.cos(toRad(b[0]));
  return 2 * R * Math.asin(Math.sqrt(h));
}

function corridorSummary(corridor: Preemption[]): string {
  const active = corridor.filter((r) => r.state === "active").length;
  const planned = corridor.filter((r) => ["planned", "armed"].includes(r.state)).length;
  const failed = corridor.filter((r) => r.state === "failed").length;
  if (failed) return `${failed} hold failed — expect red`;
  if (active) return `${active} junction${active === 1 ? "" : "s"} held green`;
  if (planned) return `${planned} scheduled ahead`;
  return "No signals under priority control";
}

function corridorTone(corridor: Preemption[]): string {
  if (corridor.some((r) => r.state === "failed")) return "fail";
  if (corridor.some((r) => r.state === "active")) return "go";
  if (corridor.some((r) => ["planned", "armed"].includes(r.state))) return "arming";
  return "off";
}
