/** Emergency Operations Dashboard - feature 4.11. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useShallow } from "zustand/react/shallow";

import type { DriverAlert, Trip, VehiclePayload } from "@/api/types";
import {
  AlertCircle,
  FollowVehicle,
  MapCanvas,
  MapLegend,
  ROUTE_BLUE,
  RouteEndpoints,
  RouteLine,
  VehicleMarkers,
} from "@/components/MapCanvas";
import { GisLayer } from "@/components/map/GisLayer";
import { TrafficLayer } from "@/components/map/TrafficLayer";
import { LayerControl } from "@/components/map/LayerControl";
import { useGisLayers } from "@/hooks/useGisLayers";
import { useAuthStore } from "@/stores/authStore";
import { useNotifyStore } from "@/stores/notifyStore";
import {
  Badge,
  Card,
  ConnectionDot,
  Empty,
  ErrorNote,
  RedactionNote,
  Stat,
  fmtDistance,
  fmtEta,
  fmtTime,
  levelClass,
} from "@/components/ui";
import { useJourneyTick } from "@/hooks/useJourneyTick";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";
import type { EtaUpdate } from "@/stores/opsStore";
import {
  selectActiveHolds,
  selectOpenPreemptions,
  selectTripList,
  selectVehicleList,
  useOpsStore,
} from "@/stores/opsStore";

const ALERT_ZONES_KEY = "sevps.showAlertZones";

export function OperationsPage() {
  const store = useOpsStore();
  // useShallow is required, not stylistic: these selectors build a new array
  // per call and would otherwise re-render forever. See the note in opsStore.
  const trips = useOpsStore(useShallow(selectTripList));
  const vehicles = useOpsStore(useShallow(selectVehicleList));
  const openPreemptions = useOpsStore(useShallow(selectOpenPreemptions));
  const activeHolds = useOpsStore(selectActiveHolds);   // a number - safe

  // Layers, their data and the operator's on/off choices all live in one hook
  // so a layer added server-side needs no change here.
  const gis = useGisLayers();
  const authenticated = useAuthStore((state) => state.status === "authenticated");
  const [basemapId, setBasemapId] = useState<string>("");

  // Which trip card was clicked - see the focusedTrip note below.
  const [selectedTripId, setSelectedTripId] = useState<number | null>(null);

  /**
   * Driver alert zones - the large translucent circles.
   *
   * Off by default now. They are the Layer 4 broadcast radius around each
   * warned vehicle, and with several responses running they covered most of
   * the city centre in overlapping orange, hiding the traffic and signals
   * underneath. Still one toggle away for anyone checking alert coverage.
   */
  const [showAlertZones, setShowAlertZones] = useState<boolean>(
    () => window.localStorage.getItem(ALERT_ZONES_KEY) === "1",
  );

  const toggleAlertZones = useCallback(() => {
    setShowAlertZones((on) => {
      const next = !on;
      try {
        window.localStorage.setItem(ALERT_ZONES_KEY, next ? "1" : "0");
      } catch {
        // Private browsing; the toggle still works for this session.
      }
      return next;
    });
  }, []);

  // Adopt the server's default basemap once the catalogue arrives, unless the
  // operator has already picked one.
  useEffect(() => {
    if (gis.basemaps && !basemapId) setBasemapId(gis.basemaps.default);
  }, [gis.basemaps, basemapId]);

  const basemap =
    gis.basemaps?.providers.find((provider) => provider.id === basemapId) ?? null;

  /**
   * Drive the fleet.
   *
   * The control room was showing vehicles that were *active* but parked: a
   * trip had a route and an ETA, and nothing advanced the ambulance along it
   * unless a real device was reporting or `simulate` was running elsewhere.
   * The same sweep the crew consoles use fixes it here, and because the
   * positions come back as ordinary `vehicle_position` events, every marker,
   * corridor and ETA on this screen updates through the path it already had.
   */
  useJourneyTick(true);

  // Polling is the correctness floor; the socket below is the latency win.
  usePolling((signal) => store.refreshVehicles(signal), 2000);
  usePolling((signal) => store.refreshTrips(signal), 3000);
  usePolling((signal) => store.refreshPreemptions(signal), 4000);
  usePolling((signal) => store.refreshEvents(signal), 10000);

  // A sequence gap means this client missed frames; refetch rather than
  // render a board that quietly stopped updating.
  const { status, viewer, missedFrames } = useSocket("/ws/ops/", {
    onGap: () => {
      store.addLog("missed live frames - resyncing", "warn");
      void store.refreshAll();
    },
    handlers: {
      snapshot: (data) => store.applySnapshot(data as never),
      vehicle_position: (data) => store.upsertVehicle(data as VehiclePayload),
      trip_created: (data) => {
        const trip = data as { reference: string; vehicle: string };
        store.addLog(`${trip.reference}: dispatched ${trip.vehicle}`, "warn");
        void store.refreshTrips();
      },
      trip_stage: (data) => {
        const trip = data as { reference: string; previous_stage: string; stage: string };
        store.addLog(`${trip.reference}: ${trip.previous_stage} → ${trip.stage}`);
        void store.refreshTrips();
      },
      hospital_assigned: (data) => {
        const payload = data as { reference: string; hospital: { name: string } };
        store.addLog(`${payload.reference}: routed to ${payload.hospital.name}`, "ok");
        void store.refreshTrips();
      },
      route_updated: (data) => {
        const route = data as { reference: string; reason: string };
        store.addLog(`${route.reference}: route updated — ${route.reason}`);
        void store.refreshTrips();
      },
      priority_directive: (data) => {
        const directive = data as { reference: string; label: string; trigger: string; is_upgrade: boolean };
        store.addLog(
          `${directive.reference}: ${directive.label} — ${directive.trigger}`,
          directive.is_upgrade ? "bad" : "ok",
        );
      },
      signal_preempted: (data) => {
        const signal = data as { controller_id: string; duration_s: number };
        store.addLog(
          `signal ${signal.controller_id} held green ${Math.round(signal.duration_s)}s`,
          "ok",
        );
        void store.refreshPreemptions();
      },
      signal_released: (data) => {
        store.addLog(`signal ${(data as { controller_id: string }).controller_id} released`);
        void store.refreshPreemptions();
      },
      /**
       * A hospital's figures moved.
       *
       * Raised by the ward's own Updates tab and, more importantly, by an
       * admission - which decrements beds without anybody typing a number. The
       * control room's hospital panel reads the same capacity rows the
       * recommender does, so it has to hear about both or it will show beds
       * that were committed to a patient minutes ago.
       */
      hospital_capacity: (data) => {
        const event = data as { hospital: string; status?: string };
        store.addLog(
          `${event.hospital}: capacity updated${event.status ? ` — ${event.status}` : ""}`,
        );
        gis.reload("hospitals");
      },
      patient_admitted: (data) => {
        const event = data as {
          hospital: string;
          trip: string;
          applied: { label: string; units: number }[];
        };
        store.addLog(
          `${event.hospital}: admitted ${event.trip} — ` +
            `${(event.applied ?? []).map((item) => `${item.label} −${item.units}`).join(", ")}`,
          "warn",
        );
        gis.reload("hospitals");
      },
      patient_received: (data) => {
        const event = data as { reference?: string; hospital_code?: string };
        store.addLog(
          `${event.reference ?? "patient"}: received at ${event.hospital_code ?? "hospital"}`,
          "ok",
        );
        void store.refreshTrips();
      },
      driver_alerts: (data) => store.pushAlerts((data as { alerts: DriverAlert[] }).alerts ?? []),
      eta_update: (data) => {
        const update = data as EtaUpdate;
        store.applyEta(update);
        if (update.is_stalled) {
          store.addLog(`trip ${update.trip_id} is stationary`, "warn");
        }
      },
      traffic_update: () => { /* segments repaint on the next network poll */ },
      fleet_health: (data) => {
        const stale = (data as { stale: { callsign: string }[] }).stale ?? [];
        if (stale.length) {
          store.addLog(`${stale.map((v) => v.callsign).join(", ")} silent`, "bad");
        }
      },
      notification: (data) => {
        const note = data as { title: string; severity: string };
        store.addLog(note.title, note.severity === "critical" ? "bad" : "warn");
        // Also feed the notification centre, so a notification that arrived
        // while this tab was open is not missing from the bell.
        useNotifyStore.getState().ingest(data as never);
      },
      road_event_created: () => void store.refreshEvents(),
      road_event_cleared: () => void store.refreshEvents(),
    },
  });

  const focusedCallsign = store.followedCallsign;

  const followed = useMemo(
    () => vehicles.find((vehicle) => vehicle.callsign === focusedCallsign) ?? null,
    [vehicles, focusedCallsign],
  );

  /**
   * The trip being tracked.
   *
   * Keyed on trip id, not callsign: one ambulance can legitimately hold more
   * than one active trip (a handover still open while the next dispatch is
   * created), and resolving by callsign alone highlighted both cards and drew
   * whichever route happened to sort first. Falls back to the newest trip for
   * the callsign when focus arrived from the map marker, which knows only the
   * vehicle.
   */
  const focusedTrip = useMemo(() => {
    if (!focusedCallsign) return null;
    const byId = trips.find((trip) => trip.id === selectedTripId);
    if (byId && byId.vehicle_callsign === focusedCallsign) return byId;
    const forVehicle = trips.filter((trip) => trip.vehicle_callsign === focusedCallsign);
    return forVehicle.length
      ? forVehicle.reduce((newest, trip) => (trip.id > newest.id ? trip : newest))
      : null;
  }, [trips, focusedCallsign, selectedTripId]);

  const selectTrip = useCallback(
    (trip: Trip) => {
      setSelectedTripId(trip.id);
      store.follow(trip.vehicle_callsign);
    },
    [store],
  );

  const clearFocus = useCallback(() => {
    setSelectedTripId(null);
    store.follow(null);
  }, [store]);

  // Focus mode: one vehicle on the map, everything else hidden. Requested so
  // an operator tracking a single response is not reading nine markers to find
  // it. Cleared by "Show all emergency vehicles".
  const shownVehicles = useMemo(
    () => (focusedCallsign ? vehicles.filter((v) => v.callsign === focusedCallsign) : vehicles),
    [vehicles, focusedCallsign],
  );

  // Keyed lookup so each marker's hover card can name its emergency and
  // destination without scanning the trip list per marker.
  const tripsByCallsign = useMemo(() => {
    const index: Record<string, Trip> = {};
    for (const trip of trips) index[trip.vehicle_callsign] = trip;
    return index;
  }, [trips]);

  const focusedRoute = focusedTrip?.active_route?.geometry ?? [];

  return (
    <div className="split wide">
      <aside className="sidebar">
        <div className="sidebar-head">
          <ConnectionDot status={status} missedFrames={missedFrames} />
          {viewer && !viewer.authenticated && (
            <Badge tone="warn">socket anonymous</Badge>
          )}
        </div>

        <ErrorNote error={store.lastError} />

        <div className="stat-row">
          <Stat value={trips.length} label="Active trips" />
          <Stat value={vehicles.length} label="Vehicles online" />
          <Stat value={activeHolds} label="Signals held" />
          <Stat value={store.alerts.length} label="Live alerts" />
        </div>

        <Card title="Active emergency vehicles">
          {focusedCallsign ? (
            <button type="button" className="ghost show-all" onClick={clearFocus}>
              ← Show all emergency vehicles
            </button>
          ) : (
            trips.length > 0 && <p className="hint">Tap one to track it on the map.</p>
          )}
          {trips.length === 0 ? (
            <Empty>No active trips. Start one from the Paramedic screen or run the simulator.</Empty>
          ) : (
            trips.map((trip) => (
              <TripCard
                key={trip.id}
                trip={trip}
                selected={trip.id === focusedTrip?.id}
                dimmed={focusedTrip !== null && trip.id !== focusedTrip.id}
                onSelect={() => selectTrip(trip)}
              />
            ))
          )}
        </Card>

        <Card title="Green corridor — signal status">
          {openPreemptions.length === 0 ? (
            <Empty>No signals under priority control.</Empty>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>Junction</th>
                  <th>State</th>
                  <th>Green at</th>
                  <th>Hold</th>
                </tr>
              </thead>
              <tbody>
                {openPreemptions.map((preemption) => (
                  <tr key={preemption.id}>
                    <td>{preemption.intersection || preemption.controller_id}</td>
                    <td>
                      <Badge tone={preemption.state === "active" ? "ok" : "warn"}>
                        {preemption.state}
                      </Badge>
                    </td>
                    <td className="mono">{fmtTime(preemption.planned_green_at)}</td>
                    <td className="mono">{Math.round(preemption.hold_duration_s)}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Live event log">
          <div className="log">
            {store.log.map((entry) => (
              <div className="entry" key={entry.id}>
                <span className="t">{fmtTime(entry.at)}</span>
                <span className={entry.tone === "info" ? "" : `badge ${entry.tone}`}>
                  {entry.message}
                </span>
              </div>
            ))}
          </div>
        </Card>
      </aside>

      <MapCanvas basemap={basemap}>
        {/* The road network is drawn imperatively on its own canvas - see the
            note in TrafficLayer for why it is not a GisLayer. */}
        {gis.isActive("road_network") && (
          <TrafficLayer
            collection={gis.data.road_network ?? null}
            dimmed={focusedCallsign !== null}
          />
        )}

        {/* Declarative GIS layers: hospitals, signals, closures, routes,
            boards, cameras and the heat surfaces. */}
        {gis.catalogue.map((layer) => {
          if (layer.name === "road_network") return null;
          // In focus mode the platform-wide route and vehicle layers would
          // re-draw the vehicles this mode exists to hide.
          if (focusedCallsign && (layer.name === "emergency_routes" || layer.name === "emergency_vehicles")) {
            return null;
          }
          return gis.isActive(layer.name) ? (
            <GisLayer
              key={layer.name}
              layer={layer.name}
              collection={gis.data[layer.name] ?? null}
              heatmap={layer.is_heatmap}
            />
          ) : null;
        })}

        {/* The focused vehicle's own route, in vibrant blue. */}
        {focusedCallsign && (
          <>
            <RouteLine geometry={focusedRoute} colour={ROUTE_BLUE} />
            <RouteEndpoints
              geometry={focusedRoute}
              destinationLabel={focusedTrip?.hospital_name ?? null}
            />
          </>
        )}

        {/* Vehicles stay socket-driven rather than polled - they move every
            second and carry the priority styling the corridor depends on. */}
        <VehicleMarkers
          vehicles={shownVehicles}
          onSelect={(callsign) => {
            // From the map we only know the vehicle; let focusedTrip resolve
            // which of its trips to draw.
            setSelectedTripId(null);
            store.follow(callsign);
          }}
          focusedCallsign={focusedCallsign}
          tripsByCallsign={tripsByCallsign}
        />

        {showAlertZones &&
          store.alerts.map((alert) => (
            <AlertCircle
              key={alert.uuid}
              position={[alert.latitude, alert.longitude]}
              radiusM={alert.radius_m}
              message={alert.message}
            />
          ))}
        <FollowVehicle
          position={followed ? [followed.latitude, followed.longitude] : null}
          enabled={focusedCallsign !== null}
        />
      </MapCanvas>

      {focusedCallsign && (
        <div className="focus-banner">
          <span className="focus-dot" />
          Tracking <b>{focusedCallsign}</b>
          {focusedTrip?.hospital_name ? ` → ${focusedTrip.hospital_name}` : ""}
          <button type="button" onClick={clearFocus}>
            Show all emergency vehicles
          </button>
        </div>
      )}

      <LayerControl
        catalogue={gis.catalogue}
        basemaps={gis.basemaps}
        data={gis.data}
        isActive={gis.isActive}
        onToggle={gis.toggle}
        basemapId={basemapId}
        onBasemap={setBasemapId}
        authenticated={authenticated}
        clientLayers={[
          {
            name: "alert_zones",
            title: "Driver alert zones",
            description:
              "Broadcast radius around each warned vehicle. Off by default — with several responses running they overlap into one orange mass.",
            count: store.alerts.length,
            active: showAlertZones,
            onToggle: toggleAlertZones,
          },
        ]}
      />
      <MapLegend />
    </div>
  );
}

/**
 * One-second clock, scoped to the card that needs it.
 *
 * This used to live on the page, which meant the whole console - map, layers,
 * every marker - re-rendered once a second purely so an ETA countdown stayed
 * honest. Keeping the interval here confines that cost to the text it exists
 * for.
 */
function useSecondTick(): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
}

function TripCard({
  trip,
  selected = false,
  dimmed = false,
  onSelect,
}: {
  trip: Trip;
  selected?: boolean;
  dimmed?: boolean;
  onSelect: () => void;
}) {
  useSecondTick();
  return (
    <div
      className={`trip clickable ${levelClass(trip.priority_level)}${selected ? " selected" : ""}${
        dimmed ? " dimmed" : ""
      }`}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter") onSelect();
      }}
    >
      <div className="head">
        <span className="ref">
          {trip.reference} · {trip.vehicle_callsign}
        </span>
        <Badge tone={levelClass(trip.priority_level) as "l1"}>L{trip.priority_level}</Badge>
      </div>
      <div className="meta">
        {trip.category_display || trip.emergency_category} · {trip.stage_display || trip.stage}
      </div>
      <div className="meta">
        {trip.hospital_name ? `→ ${trip.hospital_name}` : <em>hospital not yet assigned</em>}
      </div>
      <div className="meta">
        <span className="eta">ETA {fmtEta(trip.eta)}</span> ·{" "}
        {fmtDistance(trip.distance_remaining_m)} remaining · siren: {trip.siren_mode}
      </div>
      {trip.clinical_data_redacted && <RedactionNote />}
    </div>
  );
}
