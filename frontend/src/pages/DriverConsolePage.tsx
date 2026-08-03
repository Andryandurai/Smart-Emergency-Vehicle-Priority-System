/**
 * Ambulance driver console.
 *
 * Three screens behind one route, because they are three stages of one shift
 * and a driver should never have to navigate between them:
 *
 *   pick     - which ambulance am I taking today
 *   check    - is it fit to go (or: emergency, skip and go now)
 *   navigate - the in-vehicle console for the rest of the shift
 *
 * The stage is derived from server state rather than held as a wizard step,
 * so a driver who closes the app mid-shift and reopens it lands exactly where
 * they left off - which on a night shift is the difference between the tool
 * being usable and being abandoned.
 *
 * Distinct from `DriverPage` (`/driver`), which is the *public road user*
 * alert receiver. Same word, different person: that one warns civilians that
 * an ambulance is coming, this one is inside the ambulance.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Marker } from "react-leaflet";

import { ApiError } from "@/api/client";
import {
  brain,
  dispatch as dispatchApi,
  driverOps,
  shifts as shiftApi,
} from "@/api/endpoints";
import type {
  Breakdown,
  CrewShift,
  EquipmentAnswer,
  EquipmentCheckPayload,
  EquipmentItemSpec,
  FailureReason,
  Preemption,
  ReadinessOutcome,
  RoutePreview,
  SelectableVehicle,
  TransferOffer,
  Trip,
  VehiclePayload,
} from "@/api/types";
import { MapCanvas, ROUTE_BLUE, RouteLine, vehicleIcon } from "@/components/MapCanvas";
import { GisLayer } from "@/components/map/GisLayer";
import { TrafficLayer } from "@/components/map/TrafficLayer";
import { ChaseCamera, HeadingRotation } from "@/components/driver/NavMap";
import { RoutePanel, type RouteOption } from "@/components/driver/RoutePanel";
import { SignalStrip, type UpcomingSignal } from "@/components/driver/SignalStrip";
import { Badge, ConnectionDot, ErrorNote, fmtDistance, fmtEta, levelClass } from "@/components/ui";
import { useGisLayers } from "@/hooks/useGisLayers";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";
import { useAuthStore } from "@/stores/authStore";

/** The four transitions a driver makes. Handover belongs to the paramedic. */
const STAGES: { value: string; label: string }[] = [
  { value: "to_scene", label: "On route" },
  { value: "on_scene", label: "On scene" },
  { value: "to_hospital", label: "Transporting" },
  { value: "arrived", label: "At hospital" },
];

const FAILURE_REASONS: { value: FailureReason; label: string }[] = [
  { value: "engine", label: "Engine" },
  { value: "battery", label: "Battery" },
  { value: "tyres", label: "Tyres" },
  { value: "brakes", label: "Brakes" },
  { value: "gps", label: "GPS" },
  { value: "siren", label: "Siren" },
  { value: "emergency_lights", label: "Emergency lights" },
  { value: "oxygen", label: "Oxygen" },
  { value: "medical_equipment", label: "Medical equipment" },
  { value: "other", label: "Other" },
];

export function DriverConsolePage() {
  const username = useAuthStore((state) => state.user?.username ?? "");
  const [shift, setShift] = useState<CrewShift | null>(null);
  const [check, setCheck] = useState<(EquipmentCheckPayload & Partial<ReadinessOutcome>) | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * Re-open the checklist after a failed inspection.
   *
   * Readiness is derived from the stored answers, so a grounded vehicle stays
   * grounded on every reload - correct, but it left "Re-inspect after repair"
   * refreshing straight back onto the same screen with no way to record that
   * the tyre had been changed. This flag is what reopens the form.
   */
  const [reinspecting, setReinspecting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const mine = await shiftApi.mine();
      setShift(mine.shift);
      setCheck(mine.shift?.equipment_check ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your shift.");
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Stage is derived, never stored - see the module docstring.
  const amDriver = shift?.driver_detail?.username === username;
  // `vehicle_readiness` is only present on a checklist response; the shift's
  // stored check carries `readiness`. Prefer the fresher of the two.
  const readiness = check?.vehicle_readiness ?? check?.readiness;
  const inspectionSettled =
    Boolean(check?.is_complete) || Boolean(check?.skipped) || readiness === "not_ready";

  if (!loaded) {
    return <div className="nav-boot">Loading shift…</div>;
  }

  if (!shift) {
    return <VehiclePicker onClaimed={refresh} error={error} />;
  }

  // Draft: vehicle taken, inspection outstanding. The paramedic is called
  // only once the vehicle is known to be fit - see ShiftStatus.DRAFT.
  if (shift.status === "draft" && (!inspectionSettled || reinspecting)) {
    return (
      <ReadinessChecklist
        shift={shift}
        onDone={async () => {
          setReinspecting(false);
          await refresh();
        }}
        canSkip={amDriver}
      />
    );
  }

  if (shift.status === "draft" && readiness === "not_ready") {
    return (
      <GroundedScreen
        shift={shift}
        check={check}
        onRecheck={async () => {
          setReinspecting(true);
          await refresh();
        }}
      />
    );
  }

  if (shift.status === "draft") {
    return (
      <RequestParamedic
        shift={shift}
        currentUsername={username}
        check={check}
        onSent={refresh}
      />
    );
  }

  if (shift.status === "pending") {
    return (
      <div className="nav-boot">
        <h2>{shift.vehicle_callsign}</h2>
        <p>
          Waiting for <b>{shift.paramedic_detail?.name}</b> to accept the crew sync
          request.
        </p>
        <p className="muted">The shift starts once they accept on their own device.</p>
      </div>
    );
  }

  if (!inspectionSettled) {
    return (
      <ReadinessChecklist
        shift={shift}
        onDone={refresh}
        canSkip={amDriver}
      />
    );
  }

  if (readiness === "not_ready") {
    return <GroundedScreen shift={shift} check={check} onRecheck={refresh} />;
  }

  return <NavigationConsole shift={shift} check={check} onShiftChange={refresh} />;
}

// ---------------------------------------------------------------------------
// 1. Which ambulance am I taking
// ---------------------------------------------------------------------------
function VehiclePicker({
  onClaimed,
  error,
}: {
  onClaimed: () => Promise<void>;
  error: string | null;
}) {
  const [vehicles, setVehicles] = useState<SelectableVehicle[]>([]);
  const [chosen, setChosen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    shiftApi.selectableVehicles().then((r) => setVehicles(r.vehicles)).catch(() => setVehicles([]));
  }, []);

  const start = async () => {
    if (!chosen) return;
    setBusy(true);
    setLocalError(null);
    try {
      await shiftApi.claim(chosen);
      await onClaimed();
    } catch (err) {
      setLocalError(
        err instanceof ApiError ? err.message : "Could not take that ambulance.",
      );
      // Somebody else may have claimed it in the meantime.
      shiftApi.selectableVehicles().then((r) => setVehicles(r.vehicles)).catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="driver-setup">
      <header>
        <h1>Take over an ambulance</h1>
        <p>
          Step 1 of 3 — choose your vehicle. You will inspect it next, then call your
          paramedic.
        </p>
      </header>

      <ErrorNote error={error ?? localError} />

      {vehicles.length === 0 ? (
        <div className="nav-empty-card">
          No ambulance is free to take over. Every vehicle is either crewed, on a call,
          or grounded for maintenance.
        </div>
      ) : (
        <div className="veh-choice-grid">
          {vehicles.map((vehicle) => (
            <button
              key={vehicle.callsign}
              type="button"
              className={`veh-choice${chosen === vehicle.callsign ? " selected" : ""}`}
              onClick={() => setChosen(vehicle.callsign)}
            >
              <div className="vc-callsign">{vehicle.callsign}</div>
              {vehicle.registration && <div className="veh-reg">{vehicle.registration}</div>}
              <div className="vc-meta">
                {vehicle.ownership_display ?? ""}
                {vehicle.is_als ? " · ALS" : ""}
              </div>
              <div className="vc-meta muted">{vehicle.home_station ?? "No home station"}</div>
              <Badge tone={vehicle.readiness === "ready" ? "ok" : "warn"}>
                {vehicle.readiness_display ?? "Not yet inspected"}
              </Badge>
            </button>
          ))}
        </div>
      )}

      {chosen && (
        <div className="setup-confirm">
          <button
            type="button"
            className="primary-action"
            disabled={busy}
            onClick={() => void start()}
          >
            {busy ? "Taking over…" : `Take over ${chosen} — start inspection`}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * Step 3: call a paramedic to the vehicle.
 *
 * Reached only once the inspection has settled, so the driver is asking a
 * colleague to join an ambulance they already know is fit - or, after an
 * emergency skip, one they have consciously taken out unchecked.
 */
function RequestParamedic({
  shift,
  currentUsername,
  check,
  onSent,
}: {
  shift: CrewShift;
  currentUsername: string;
  check: (EquipmentCheckPayload & Partial<ReadinessOutcome>) | null;
  onSent: () => Promise<void>;
}) {
  const [crew, setCrew] = useState<{ username: string; name: string }[]>([]);
  const [paramedic, setParamedic] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    shiftApi.crew().then((r) => setCrew(r.crew)).catch(() => setCrew([]));
  }, []);

  const send = async () => {
    if (!paramedic) return;
    setBusy(true);
    setError(null);
    try {
      await shiftApi.requestParamedic(shift.id, paramedic);
      await onSent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send the request.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="driver-setup">
      <header>
        <h1>Call your paramedic</h1>
        <p>
          Step 3 of 3 — {shift.vehicle_callsign} is inspected and ready. Send the sync
          request; the shift starts when they accept on their own device.
        </p>
      </header>

      <ErrorNote error={error} />

      <div className="report-card" style={{ borderColor: "#2ecc71" }}>
        <div className="rc-head">
          {shift.vehicle_callsign} · {shift.vehicle_registration}
          <Badge tone={check?.skipped ? "warn" : "ok"}>
            {check?.skipped ? "Temporarily ready" : "Ready for service"}
          </Badge>
        </div>
        {check?.skipped && (
          <div className="rc-reasons">
            Inspection skipped — complete it once this emergency ends.
          </div>
        )}
      </div>

      <div className="setup-confirm">
        <label htmlFor="medic">Paramedic crewing with you</label>
        <select id="medic" value={paramedic} onChange={(e) => setParamedic(e.target.value)}>
          <option value="">Select a paramedic…</option>
          {crew
            .filter((person) => person.username !== currentUsername)
            .map((person) => (
              <option key={person.username} value={person.username}>
                {person.name} ({person.username})
              </option>
            ))}
        </select>
        <button
          type="button"
          className="primary-action"
          disabled={!paramedic || busy}
          onClick={() => void send()}
        >
          {busy ? "Sending…" : "Send sync request"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 2. Vehicle readiness checklist
// ---------------------------------------------------------------------------
function ReadinessChecklist({
  shift,
  onDone,
  canSkip,
}: {
  shift: CrewShift;
  onDone: () => Promise<void>;
  canSkip: boolean;
}) {
  const [catalogue, setCatalogue] = useState<EquipmentItemSpec[]>([]);
  const [draft, setDraft] = useState<Record<string, EquipmentAnswer>>(
    shift.equipment_check?.items ?? {},
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSkip, setShowSkip] = useState(false);
  const [skipReason, setSkipReason] = useState("");

  useEffect(() => {
    shiftApi.equipmentCatalogue().then((r) => setCatalogue(r.items)).catch(() => setCatalogue([]));
  }, []);

  const answered = Object.keys(draft).length;
  const failedCritical = catalogue.filter(
    (item) => item.critical && draft[item.code]?.present === false,
  );

  const setAnswer = (code: string, present: boolean) =>
    setDraft((current) => ({ ...current, [code]: { present, note: current[code]?.note ?? "" } }));

  const setNote = (code: string, note: string) =>
    setDraft((current) => ({
      ...current,
      [code]: { present: current[code]?.present ?? false, note },
    }));

  const submit = async () => {
    setBusy("save");
    setError(null);
    try {
      await shiftApi.saveChecklist(shift.id, draft);
      await onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the inspection.");
    } finally {
      setBusy(null);
    }
  };

  const skip = async () => {
    if (!skipReason.trim()) {
      setError("Say why the inspection is being skipped — it stays on the record.");
      return;
    }
    setBusy("skip");
    setError(null);
    try {
      await shiftApi.skipChecklist(shift.id, skipReason);
      await onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not skip the inspection.");
    } finally {
      setBusy(null);
    }
  };

  const groups = catalogue.reduce<Record<string, EquipmentItemSpec[]>>((acc, item) => {
    (acc[item.group] ??= []).push(item);
    return acc;
  }, {});

  return (
    <div className="driver-setup checklist">
      <header>
        <h1>Vehicle readiness — {shift.vehicle_callsign}</h1>
        <p>
          Step 2 of 3 — {shift.vehicle_registration}. Mark each item ready or not ready.
          A failed critical item grounds the vehicle and no paramedic is called to it.
        </p>
      </header>

      <ErrorNote error={error} />

      {failedCritical.length > 0 && (
        <div className="critical-note">
          <b>{failedCritical.length} critical item(s) not ready.</b> Submitting will mark{" "}
          {shift.vehicle_callsign} NOT READY, raise a maintenance report and prevent
          dispatch.
        </div>
      )}

      {Object.entries(groups).map(([group, items]) => (
        <section key={group} className="check-group">
          <h5>{group}</h5>
          {items.map((item) => {
            const answer = draft[item.code];
            return (
              <div key={item.code} className="check-row driver">
                <span className="check-label">
                  {item.label}
                  {item.critical && <span className="crit-dot" title="Critical" />}
                </span>
                <span className="check-actions">
                  <button
                    type="button"
                    className={`yn yes${answer?.present === true ? " on" : ""}`}
                    onClick={() => setAnswer(item.code, true)}
                  >
                    ✔ Ready
                  </button>
                  <button
                    type="button"
                    className={`yn no${answer?.present === false ? " on" : ""}`}
                    onClick={() => setAnswer(item.code, false)}
                  >
                    ✖ Not ready
                  </button>
                </span>
                {answer?.present === false && (
                  <input
                    className="check-note"
                    placeholder="Remarks (optional)"
                    value={answer.note ?? ""}
                    onChange={(event) => setNote(item.code, event.target.value)}
                  />
                )}
              </div>
            );
          })}
        </section>
      ))}

      <div className="checklist-actions">
        <button
          type="button"
          className="primary-action"
          disabled={busy !== null}
          onClick={() => void submit()}
        >
          {busy === "save"
            ? "Submitting…"
            : `Submit inspection (${answered}/${catalogue.length})`}
        </button>
        {canSkip && (
          <button type="button" className="ghost danger" onClick={() => setShowSkip(true)}>
            Emergency skip
          </button>
        )}
      </div>

      {showSkip && (
        <div className="skip-form">
          <p className="hint">
            The vehicle becomes <b>Temporarily Ready</b> and can be dispatched at once.
            Admin is notified and the inspection must be completed after the emergency.
          </p>
          <label htmlFor="skipWhy">Why is the inspection being skipped?</label>
          <input
            id="skipWhy"
            value={skipReason}
            onChange={(event) => setSkipReason(event.target.value)}
            placeholder="e.g. cardiac call received while boarding"
          />
          <div className="btn-row">
            <button
              type="button"
              className="ghost danger"
              disabled={busy !== null}
              onClick={() => void skip()}
            >
              {busy === "skip" ? "Recording…" : "Skip and go now"}
            </button>
            <button type="button" className="ghost" onClick={() => setShowSkip(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Grounded: the checklist failed
// ---------------------------------------------------------------------------
function GroundedScreen({
  shift,
  check,
  onRecheck,
}: {
  shift: CrewShift;
  check: (EquipmentCheckPayload & Partial<ReadinessOutcome>) | null;
  onRecheck: () => Promise<void>;
}) {
  return (
    <div className="driver-setup grounded">
      <header>
        <h1 className="grounded-title">{shift.vehicle_callsign} — NOT READY</h1>
        <p>
          This ambulance cannot be dispatched. Fleet management and the control room have
          been notified and a maintenance report has been raised.
        </p>
      </header>

      <div className="critical-note">
        <b>Failed:</b>{" "}
        {(check?.missing_critical ?? []).join(", ") || "critical items"}
      </div>

      {check?.maintenance_report && (
        <div className="report-card">
          <div className="rc-head">
            Maintenance report #{check.maintenance_report.id}
            <Badge tone="bad">{check.maintenance_report.state_display}</Badge>
          </div>
          <div className="rc-reasons">
            {check.maintenance_report.reasons.join(" · ")}
          </div>
          <pre className="rc-remarks">{check.maintenance_report.remarks}</pre>
        </div>
      )}

      <button type="button" className="primary-action" onClick={() => void onRecheck()}>
        Re-inspect after repair
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 3. In-vehicle navigation console
// ---------------------------------------------------------------------------
function NavigationConsole({
  shift,
  check,
  onShiftChange,
}: {
  shift: CrewShift;
  check: (EquipmentCheckPayload & Partial<ReadinessOutcome>) | null;
  onShiftChange: () => Promise<void>;
}) {
  const callsign = shift.vehicle_callsign;
  const [vehicle, setVehicle] = useState<VehiclePayload | null>(null);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [corridor, setCorridor] = useState<Preemption[]>([]);
  const [routeGeometry, setRouteGeometry] = useState<[number, number][]>([]);
  const [offers, setOffers] = useState<TransferOffer[]>([]);
  const [breakdown, setBreakdown] = useState<Breakdown | null>(null);
  const [alternatives, setAlternatives] = useState<RouteOption[]>([]);
  const [routesLoading, setRoutesLoading] = useState(false);
  const [selectedRoute, setSelectedRoute] = useState<string | null>("ai");
  /**
   * Heading-up rotation is opt-in, not the default.
   *
   * It is implemented and it looks right, but Leaflet has no knowledge of the
   * CSS transform, so while it is on, pointer coordinates and hit-testing are
   * offset by the rotation angle - tapping a hospital pin selects whatever is
   * at the unrotated position instead. Shipping that as the default would
   * make the map actively misleading to touch, so the driver opts in with the
   * ◈ button and gets a correct north-up map otherwise.
   */
  const [northUp, setNorthUp] = useState(true);
  const [showBreakdown, setShowBreakdown] = useState(false);
  const [faultReasons, setFaultReasons] = useState<FailureReason[]>([]);
  const [faultRemarks, setFaultRemarks] = useState("");
  const [busyStage, setBusyStage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /**
   * Report where the journey has got to.
   *
   * The hospital board renders `stage_display` straight from the trip, so
   * this is the driver telling a charge nurse - not a status field nobody
   * reads. Kept to the four transitions a driver actually makes; handover
   * stays with the paramedic, who is the one completing it.
   */
  const setStage = async (stage: string) => {
    if (!trip) return;
    setBusyStage(stage);
    setError(null);
    try {
      setTrip(await dispatchApi.setStage(trip.id, stage));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the status.");
    } finally {
      setBusyStage(null);
    }
  };

  const gis = useGisLayers();

  const { status } = useSocket(`/ws/vehicle/${encodeURIComponent(callsign)}/`, {
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
        if (snap.route?.geometry) setRouteGeometry(snap.route.geometry);
      },
      vehicle_position: (data) => setVehicle(data as VehiclePayload),
      route_updated: (data) =>
        setRouteGeometry((data as { geometry: [number, number][] }).geometry),
      trip_stage: () => void onShiftChange(),
      // Reusing the vehicle socket rather than opening a driver-specific one:
      // the crew already hold it, and a transfer offer is addressed to a
      // vehicle, which is exactly what this group is keyed on.
      transfer_offer: (data) => {
        const offer = data as TransferOffer;
        setOffers((current) =>
          current.some((o) => o.id === offer.id) ? current : [...current, offer],
        );
      },
      transfer_accepted: () => {
        setOffers([]);
        void onShiftChange();
      },
    },
  });

  usePolling((signal) => driverOps.myOffers(signal).then((r) => setOffers(r.offers)), 8000);

  const position: [number, number] | null = vehicle
    ? [vehicle.latitude, vehicle.longitude]
    : null;

  // --- alternative routes -------------------------------------------------
  const destination: [number, number] | null =
    trip?.destination_latitude != null && trip?.destination_longitude != null
      ? [trip.destination_latitude, trip.destination_longitude]
      : null;

  const lastComputed = useRef<string>("");
  useEffect(() => {
    if (!position || !destination) {
      setAlternatives([]);
      return;
    }
    // Recompute only when the destination changes, not on every GPS fix -
    // three route searches a second would achieve nothing except load.
    const key = `${destination[0].toFixed(4)},${destination[1].toFixed(4)}`;
    if (lastComputed.current === key) return;
    lastComputed.current = key;

    setRoutesLoading(true);
    void (async () => {
      try {
        const level = trip?.priority_level ?? 1;
        const [recommended, cautious] = await Promise.all([
          brain.route(position, destination, level),
          // A lower-priority plan weights signals the way ordinary traffic
          // experiences them, which surfaces a genuinely different road
          // rather than the same one costed twice.
          brain.route(position, destination, 4),
        ]);
        setAlternatives(buildOptions(recommended, cautious));
      } catch {
        setAlternatives([]);
      } finally {
        setRoutesLoading(false);
      }
    })();
  }, [position, destination, trip?.priority_level]);

  // --- upcoming signals ---------------------------------------------------
  const upcoming: UpcomingSignal[] = useMemo(() => {
    if (!position) return [];
    const now = Date.now();
    return corridor
      .filter((row) => ["planned", "armed", "active", "failed"].includes(row.state))
      .map((row) => ({
        preemption: row,
        distanceM: haversine(position, [row.latitude, row.longitude]),
        secondsToGreen: (new Date(row.planned_green_at).getTime() - now) / 1000,
      }))
      .sort((a, b) => a.distanceM - b.distanceM)
      .slice(0, 4);
  }, [corridor, position]);

  const declareBreakdown = async () => {
    if (faultReasons.length === 0) {
      setError("Select at least one fault so the workshop knows what failed.");
      return;
    }
    setError(null);
    try {
      const result = await driverOps.declareBreakdown(
        callsign, faultReasons, faultRemarks, position ?? undefined,
      );
      setBreakdown(result);
      setShowBreakdown(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not declare a breakdown.");
    }
  };

  const respond = async (offer: TransferOffer, accept: boolean) => {
    try {
      if (accept) await driverOps.acceptTransfer(offer.breakdown_id, callsign);
      else await driverOps.rejectTransfer(offer.breakdown_id, callsign, "Declined");
      setOffers((current) => current.filter((o) => o.id !== offer.id));
      await onShiftChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not respond to the transfer.");
      setOffers((current) => current.filter((o) => o.id !== offer.id));
    }
  };

  const heading = vehicle?.heading_deg ?? 0;
  const speed = Math.round(vehicle?.speed_kmh ?? 0);

  return (
    <div className="nav-console">
      <div className="nav-map-wrap">
        <MapCanvas centre={position ?? undefined} zoom={16} className="map nav-map">
          <TrafficLayer collection={gis.data.road_network ?? null} />
          <GisLayer layer="traffic_signals" collection={gis.data.traffic_signals ?? null} />
          <GisLayer layer="road_closures" collection={gis.data.road_closures ?? null} />
          <GisLayer layer="hospitals" collection={gis.data.hospitals ?? null} />
          <RouteLine geometry={routeGeometry} colour={ROUTE_BLUE} />
          {vehicle && <OwnVehicle vehicle={vehicle} />}
          <HeadingRotation heading={heading} enabled={!northUp} />
          {/* Following and rotating are separate concerns. North-up means
              "do not rotate the map", never "stop tracking the ambulance" -
              tying them to one flag left the driver looking at a fixed view
              of the city centre while their vehicle drove off it. */}
          <ChaseCamera
            position={position}
            heading={northUp ? 0 : heading}
            lookAheadPx={northUp ? 0 : 150}
          />
        </MapCanvas>

        {/* Head-up figures, over the map rather than beside it: a driver
            glancing down for an ETA should not have to find a panel. */}
        <div className="hud">
          <div className="hud-speed">
            <b>{speed}</b>
            <span>km/h</span>
          </div>
          <div className="hud-mid">
            {trip ? (
              <>
                <div className="hud-road">
                  {trip.stage_display} · {trip.category_display}
                </div>
                <div className="hud-dest">
                  → {trip.hospital_name ?? "destination not yet assigned"}
                </div>
              </>
            ) : (
              <>
                <div className="hud-road">Standing by</div>
                <div className="hud-dest">No active call — awaiting dispatch</div>
              </>
            )}
          </div>
          {/* An ETA panel with nothing to count down to is noise on a screen
              that has to be readable at a glance, so it simply is not there. */}
          {trip && (
            <div className="hud-eta">
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

        {trip && (
          <div className={`corridor-banner ${corridorTone(corridor)}`}>
            <span className="cb-label">Green corridor</span>
            <span className="cb-state">{corridorSummary(corridor)}</span>
            <Badge tone={levelClass(trip.priority_level) as "l1"}>
              L{trip.priority_level}
            </Badge>
          </div>
        )}
      </div>

      <aside className="nav-side">
        <div className="nav-head">
          <div>
            <div className="nav-callsign">{callsign}</div>
            <div className="muted small">{shift.vehicle_registration}</div>
          </div>
          <ConnectionDot status={status} />
        </div>

        <ErrorNote error={error} />

        {check?.skipped && check.is_outstanding && (
          <div className="skip-note">
            Inspection skipped — <b>Temporarily Ready</b>. Complete it once this
            emergency ends.
          </div>
        )}

        {/* A transfer request outranks everything else on this screen. */}
        {offers.map((offer) => (
          <div key={offer.id} className="transfer-offer">
            <div className="to-title">Emergency transfer required</div>
            <div className="to-body">
              <b>{offer.breakdown?.vehicle}</b> has broken down{" "}
              <b>{fmtDistance(offer.distance_m)}</b> away with a{" "}
              {offer.breakdown?.emergency_category_display ?? "patient"} on board
              {offer.breakdown?.destination_hospital
                ? ` bound for ${offer.breakdown.destination_hospital}`
                : ""}
              .
            </div>
            <div className="btn-row">
              <button
                type="button"
                className="primary-action"
                onClick={() => void respond(offer, true)}
              >
                Accept transfer
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => void respond(offer, false)}
              >
                Reject
              </button>
            </div>
          </div>
        ))}

        {breakdown && (
          <div className="breakdown-live">
            Breakdown reported. {breakdown.replacement
              ? `${breakdown.replacement} is taking over.`
              : "Seeking a replacement ambulance…"}
          </div>
        )}

        <SignalStrip signals={upcoming} />

        <RoutePanel
          options={alternatives}
          selectedId={selectedRoute}
          onSelect={(option) => setSelectedRoute(option.id)}
          loading={routesLoading}
        />

        {/* Journey status. The hospital's board reads the stage directly, so
            a driver marking "On scene" is what tells a charge nurse the ETA
            they are looking at has stopped counting down. */}
        {trip && (
          <div className="nav-panel">
            <h4>Journey status</h4>
            <div className="stage-now">{trip.stage_display}</div>
            <div className="stage-row">
              {STAGES.map((stage) => (
                <button
                  key={stage.value}
                  type="button"
                  className={`stage-btn${trip.stage === stage.value ? " on" : ""}`}
                  disabled={busyStage !== null}
                  onClick={() => void setStage(stage.value)}
                >
                  {busyStage === stage.value ? "…" : stage.label}
                </button>
              ))}
            </div>
            <p className="nav-empty">Shared live with the receiving hospital.</p>
          </div>
        )}

        <div className="nav-panel">
          <h4>Vehicle</h4>
          {!showBreakdown ? (
            <button
              type="button"
              className="ghost danger wide"
              onClick={() => setShowBreakdown(true)}
              disabled={!trip}
              title={trip ? "" : "A breakdown is a failure with a patient on board"}
            >
              Emergency breakdown
            </button>
          ) : (
            <div className="skip-form">
              <p className="hint">
                Notifies admin, dispatch, the receiving hospital and the nearest crews,
                and shares your position and the patient's destination.
              </p>
              <div className="fault-grid">
                {FAILURE_REASONS.map((reason) => (
                  <button
                    key={reason.value}
                    type="button"
                    className={`fault${faultReasons.includes(reason.value) ? " on" : ""}`}
                    onClick={() =>
                      setFaultReasons((current) =>
                        current.includes(reason.value)
                          ? current.filter((r) => r !== reason.value)
                          : [...current, reason.value],
                      )
                    }
                  >
                    {reason.label}
                  </button>
                ))}
              </div>
              <input
                value={faultRemarks}
                onChange={(event) => setFaultRemarks(event.target.value)}
                placeholder="What happened? (optional)"
              />
              <div className="btn-row">
                <button
                  type="button"
                  className="ghost danger"
                  onClick={() => void declareBreakdown()}
                >
                  Declare breakdown
                </button>
                <button type="button" className="ghost" onClick={() => setShowBreakdown(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

/**
 * Own vehicle.
 *
 * Drawn with a zero heading rather than the real one: on a heading-up map the
 * ambulance is by definition pointing at the top of the screen, and rotating
 * the glyph as well would make it spin while the vehicle drives straight.
 */
function OwnVehicle({ vehicle }: { vehicle: VehiclePayload }) {
  return (
    <Marker
      position={[vehicle.latitude, vehicle.longitude]}
      icon={vehicleIcon(vehicle.priority_level, vehicle.vehicle_type, {
        heading: 0,
        focused: true,
      })}
      zIndexOffset={3000}
    />
  );
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function haversine(a: [number, number], b: [number, number]): number {
  const R = 6_371_000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b[0] - a[0]);
  const dLon = toRad(b[1] - a[1]);
  const lat1 = toRad(a[0]);
  const lat2 = toRad(b[0]);
  const h =
    Math.sin(dLat / 2) ** 2 + Math.sin(dLon / 2) ** 2 * Math.cos(lat1) * Math.cos(lat2);
  return 2 * R * Math.asin(Math.sqrt(h));
}

function corridorSummary(corridor: Preemption[]): string {
  const active = corridor.filter((row) => row.state === "active").length;
  const planned = corridor.filter((row) => ["planned", "armed"].includes(row.state)).length;
  const failed = corridor.filter((row) => row.state === "failed").length;
  if (failed) return `${failed} hold failed — expect red`;
  if (active) return `${active} junction${active === 1 ? "" : "s"} held green`;
  if (planned) return `${planned} scheduled ahead`;
  return "No signals under priority control";
}

function corridorTone(corridor: Preemption[]): string {
  if (corridor.some((row) => row.state === "failed")) return "fail";
  if (corridor.some((row) => row.state === "active")) return "go";
  if (corridor.some((row) => ["planned", "armed"].includes(row.state))) return "arming";
  return "off";
}

function buildOptions(recommended: RoutePreview, alternative: RoutePreview): RouteOption[] {
  const gain = alternative.total_duration_s - recommended.total_duration_s;
  const options: RouteOption[] = [
    {
      id: "ai",
      label: "AI recommended",
      route: recommended,
      reason: "Fastest in current traffic with signal priority applied.",
      gainS: 0,
      trafficLevel: trafficFrom(recommended),
      isRecommended: true,
    },
  ];

  // Only offer the alternative if it is a materially different road - two
  // labels for the same route is worse than one.
  const differs =
    Math.abs(alternative.total_distance_m - recommended.total_distance_m) > 150;
  if (differs) {
    options.push({
      id: "alt-1",
      label: "Alternative 1",
      route: alternative,
      reason:
        gain > 0
          ? `Longer by ${Math.round(gain / 60)} min but avoids the priority corridor.`
          : `Saves ${Math.abs(Math.round(gain / 60))} min without signal priority.`,
      gainS: -gain,
      trafficLevel: trafficFrom(alternative),
      isRecommended: false,
    });
  }
  return options;
}

/** Average speed against a nominal free-flow, as a traffic band. */
function trafficFrom(route: RoutePreview): RouteOption["trafficLevel"] {
  const kmh = route.total_duration_s
    ? (route.total_distance_m / route.total_duration_s) * 3.6
    : 0;
  if (kmh >= 34) return "clear";
  if (kmh >= 20) return "moderate";
  return "heavy";
}
