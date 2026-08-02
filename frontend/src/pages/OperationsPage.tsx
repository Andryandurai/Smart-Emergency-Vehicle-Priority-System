/** Emergency Operations Dashboard - feature 4.11. */
import { useEffect, useMemo, useState } from "react";
import { useShallow } from "zustand/react/shallow";

import type { DriverAlert, Trip, VehiclePayload } from "@/api/types";
import {
  AlertCircle,
  FollowVehicle,
  MapCanvas,
  MapLegend,
  VehicleMarkers,
} from "@/components/MapCanvas";
import { GisLayer } from "@/components/map/GisLayer";
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

  // ETAs are relative, so the list re-renders once a second even when no data
  // has changed. Cheap, and it keeps the countdown honest.
  const [, setTick] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  // Adopt the server's default basemap once the catalogue arrives, unless the
  // operator has already picked one.
  useEffect(() => {
    if (gis.basemaps && !basemapId) setBasemapId(gis.basemaps.default);
  }, [gis.basemaps, basemapId]);

  const basemap =
    gis.basemaps?.providers.find((provider) => provider.id === basemapId) ?? null;

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

  const followed = useMemo(
    () => vehicles.find((vehicle) => vehicle.callsign === store.followedCallsign) ?? null,
    [vehicles, store.followedCallsign],
  );

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
          {trips.length === 0 ? (
            <Empty>No active trips. Start one from the Paramedic screen or run the simulator.</Empty>
          ) : (
            trips.map((trip) => <TripCard key={trip.id} trip={trip} onSelect={store.follow} />)
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

        <Card title="Network disruptions">
          {store.events.length === 0 ? (
            <Empty>Network clear.</Empty>
          ) : (
            store.events.slice(0, 12).map((event) => (
              <div key={event.id} className={`trip ${event.blocks_road ? "l1" : "l3"}`}>
                <div className="head">
                  <span className="ref">{event.event_type_display || event.event_type}</span>
                  <Badge tone={event.blocks_road ? "bad" : "warn"}>
                    {Math.round(event.severity * 100)}%
                  </Badge>
                </div>
                <div className="meta">
                  {event.description || "reported"} · source: {event.source}
                </div>
              </div>
            ))
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
        {/* Declarative GIS layers: roads, hospitals, signals, closures,
            routes, boards, cameras and the three heat surfaces. */}
        {gis.catalogue.map((layer) =>
          gis.isActive(layer.name) ? (
            <GisLayer
              key={layer.name}
              layer={layer.name}
              collection={gis.data[layer.name] ?? null}
              heatmap={layer.is_heatmap}
            />
          ) : null,
        )}

        {/* Vehicles stay socket-driven rather than polled - they move every
            second and carry the priority styling the corridor depends on. */}
        <VehicleMarkers vehicles={vehicles} onSelect={store.follow} />

        {store.alerts.map((alert) => (
          <AlertCircle
            key={alert.uuid}
            position={[alert.latitude, alert.longitude]}
            radiusM={alert.radius_m}
            message={alert.message}
          />
        ))}
        <FollowVehicle
          position={followed ? [followed.latitude, followed.longitude] : null}
        />
      </MapCanvas>

      <LayerControl
        catalogue={gis.catalogue}
        basemaps={gis.basemaps}
        data={gis.data}
        isActive={gis.isActive}
        onToggle={gis.toggle}
        basemapId={basemapId}
        onBasemap={setBasemapId}
        authenticated={authenticated}
      />
      <MapLegend />
    </div>
  );
}

function TripCard({ trip, onSelect }: { trip: Trip; onSelect: (callsign: string) => void }) {
  return (
    <div
      className={`trip ${levelClass(trip.priority_level)}`}
      onClick={() => onSelect(trip.vehicle_callsign)}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter") onSelect(trip.vehicle_callsign);
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
