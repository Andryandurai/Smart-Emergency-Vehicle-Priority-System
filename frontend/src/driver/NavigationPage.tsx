/**
 * Tab 2 — Navigation.
 *
 * A driving view, not a city view. The operations console draws the whole
 * road network, every signal, every closure and every hospital, because a
 * controller is watching a city. A driver at 60 km/h is reading the next four
 * hundred metres, so this map carries exactly four things: the route, the
 * ambulance, the junctions being held ahead, and the destination. None of the
 * admin GIS layers are mounted here at all — not hidden behind a toggle,
 * simply absent.
 *
 * The camera is first-person the way a phone satnav is: heading-up by
 * default, the vehicle low in the frame with the road running up the screen,
 * zoomed to street level and following.
 *
 * Rerouting is the platform's, not this screen's. `apps.brain.rerouting`
 * already decides when a closure or a jam justifies a new road; what was
 * missing was any way for the driver to *see* it happen, so this polls the
 * decision and both applies it and says why.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Marker } from "react-leaflet";
import { Link, useOutletContext } from "react-router-dom";

import { ApiError } from "@/api/client";
import { dispatch as dispatchApi, shifts as shiftApi } from "@/api/endpoints";
import type { ChecklistDue, Preemption, RerouteCheck, Trip, VehiclePayload } from "@/api/types";
import { MapCanvas, ROUTE_BLUE, RouteLine, vehicleIcon } from "@/components/MapCanvas";
import { ChaseCamera, HeadingRotation } from "@/components/driver/NavMap";
import { fmtDistance, fmtEta } from "@/components/ui";
import { DpError } from "@/driver/TakeoverPage";
import type { DriverOutletContext } from "@/driver/DriverShell";
import { useJourneyTick } from "@/hooks/useJourneyTick";
import { useSocket } from "@/hooks/useSocket";

/**
 * The journey states a driver reports.
 *
 * Handover is absent on purpose: it belongs to the paramedic, who is the one
 * completing it. These four are what the receiving hospital reads off the
 * board, which is why "On scene" matters — it is what tells a charge nurse
 * the ETA they are watching has stopped counting down.
 */
const STAGES: { value: string; label: string; hint: string }[] = [
  { value: "to_scene", label: "On route", hint: "Driving to the incident" },
  { value: "on_scene", label: "On scene", hint: "Arrived at the patient" },
  { value: "to_hospital", label: "Transporting", hint: "Patient aboard, hospital bound" },
  { value: "arrived", label: "At hospital", hint: "Arrived at the receiving hospital" },
];

export function NavigationPage() {
  const { shift, checklistDue } = useOutletContext<DriverOutletContext>();
  const [vehicle, setVehicle] = useState<VehiclePayload | null>(null);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [corridor, setCorridor] = useState<Preemption[]>([]);
  const [route, setRoute] = useState<[number, number][]>([]);
  /**
   * Heading-up is the default here, unlike the operations map.
   *
   * Leaflet has no knowledge of the CSS rotation, so while it is on, pointer
   * hit-testing is offset by the rotation angle. On a control-room map full
   * of clickable pins that would be disqualifying; on this one there is
   * nothing to tap but the orientation button itself, and "the road ahead is
   * at the top of the screen" is worth far more to someone driving.
   */
  const [headingUp, setHeadingUp] = useState(true);
  const [reroute, setReroute] = useState<RerouteCheck | null>(null);
  const [rerouting, setRerouting] = useState(false);
  const [busyStage, setBusyStage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const callsign = shift?.vehicle_callsign ?? "";
  const active = shift?.status === "active";

  const reloadTrip = useCallback(async () => {
    if (!callsign) return;
    try {
      const trips = await dispatchApi.tripsForVehicle(callsign);
      setTrip(trips[0] ?? null);
    } catch {
      /* the socket snapshot is the primary source; a failed poll is not fatal */
    }
  }, [callsign]);

  const { status } = useSocket(active && callsign ? `/ws/vehicle/${encodeURIComponent(callsign)}/` : "", {
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
      route_updated: (data) => setRoute((data as { geometry: [number, number][] }).geometry),
      trip_stage: () => void reloadTrip(),
    },
  });

  // The signal countdown is a clock, so it has to tick even when nothing
  // arrives from the server - otherwise "green in 12 s" stays on screen long
  // after the junction has been passed.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  // Drive the journey. The positions this produces arrive back through the
  // same `vehicle_position` socket event a real device would send, so the
  // marker below moves without this screen knowing where the fix came from.
  useJourneyTick(active && Boolean(trip));

  /**
   * Watch the road ahead.
   *
   * Polled rather than driven by the socket because the trigger is a
   * *condition* rather than an event: congestion builds without anything
   * being published, and the check is what turns "the graph knows this road
   * is now jammed" into a route the driver is actually on. Applying it here
   * as well as in the worker means the driver's screen reroutes even when
   * `sevps_worker` is not running.
   */
  useEffect(() => {
    if (!trip?.id || !active) {
      setReroute(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const decision = await dispatchApi.rerouteCheck(trip.id);
        if (cancelled) return;
        setReroute(decision);
        if (decision.should_reroute) {
          setRerouting(true);
          await dispatchApi.reroute(trip.id, decision.reason);
          if (!cancelled) await reloadTrip();
        }
      } catch {
        /* a failed check must not take the map down */
      } finally {
        if (!cancelled) setRerouting(false);
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [trip?.id, active, reloadTrip]);

  const setStage = async (stage: string) => {
    if (!trip) return;
    setBusyStage(stage);
    setError(null);
    try {
      setTrip(await dispatchApi.setStage(trip.id, stage));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not update your status.");
    } finally {
      setBusyStage(null);
    }
  };

  const position: [number, number] | null = vehicle
    ? [vehicle.latitude, vehicle.longitude]
    : null;
  const heading = vehicle?.heading_deg ?? 0;

  /** Junctions ahead, nearest first. Distance leads: it is what a driver judges against. */
  const signals = useMemo(() => {
    if (!position) return [];
    return corridor
      .filter((row) => ["planned", "armed", "active", "failed"].includes(row.state))
      .map((row) => ({
        row,
        distanceM: haversine(position, [row.latitude, row.longitude]),
        secondsToGreen: (new Date(row.planned_green_at).getTime() - now) / 1000,
      }))
      .sort((a, b) => a.distanceM - b.distanceM)
      .slice(0, 4);
  }, [corridor, position, now]);

  if (!active) {
    return (
      <div className="dp-page">
        <div className="dp-hero idle">
          <span className="dp-hero-tag">No shift</span>
          <h1>Navigation</h1>
          <p>Take over an ambulance and start your shift to get a route.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="dp-nav">
      <div className="dp-nav-map">
        <MapCanvas centre={position ?? undefined} zoom={17} className="map dp-map">
          {/* Route, vehicle and destination only. No road network, no signal
              layer, no closure layer, no hospital pins - that is the control
              room's map and it belongs on the control room's screen. */}
          <RouteLine geometry={route} colour={ROUTE_BLUE} />
          {vehicle && (
            <Marker
              position={[vehicle.latitude, vehicle.longitude]}
              icon={vehicleIcon(vehicle.priority_level, vehicle.vehicle_type, {
                // Zero, not the real heading: on a heading-up map the ambulance
                // points at the top of the screen by definition, and rotating
                // the glyph as well makes it spin while driving straight.
                heading: 0,
                focused: true,
              })}
              zIndexOffset={3000}
            />
          )}
          <HeadingRotation heading={heading} enabled={headingUp} />
          <ChaseCamera
            position={position}
            heading={headingUp ? heading : 0}
            lookAheadPx={headingUp ? 160 : 0}
          />
        </MapCanvas>

        {/* Head-up figures over the map: a driver glancing down for an ETA
            should not have to find a panel first. */}
        <div className="dp-hud">
          <div className="dp-hud-speed">
            <b>{Math.round(vehicle?.speed_kmh ?? 0)}</b>
            <span>km/h</span>
          </div>
          <div className="dp-hud-mid">
            <div className="dp-hud-dest">
              {trip?.hospital_name ?? (trip ? "No hospital assigned yet" : "Standing by")}
            </div>
            <div className="dp-hud-stage">
              {trip ? `${trip.stage_display} · ${trip.category_display}` : "No active emergency"}
            </div>
          </div>
          {trip && (
            <div className="dp-hud-eta">
              <b>{fmtEta(trip.eta)}</b>
              <span>{fmtDistance(trip.distance_remaining_m)}</span>
            </div>
          )}
        </div>

        <button
          type="button"
          className="dp-orient"
          onClick={() => setHeadingUp((value) => !value)}
          title={headingUp ? "Lock north up" : "Rotate with heading"}
        >
          {headingUp ? "◈" : "N↑"}
        </button>

        {/* The next junction, as large as the speed. This is the one thing a
            driver on a green corridor cannot read off the road itself. */}
        {signals.length > 0 && <NextSignal signal={signals[0]!} />}

        {(rerouting || reroute?.should_reroute || reroute?.blocked || reroute?.congested) && (
          <div className={`dp-reroute ${reroute?.blocked ? "blocked" : "congested"}`}>
            <span className="dp-reroute-icon" aria-hidden>
              {reroute?.blocked ? "⛔" : "🚦"}
            </span>
            <span>
              <b>
                {rerouting
                  ? "Rerouting…"
                  : reroute?.blocked
                    ? "Road closed ahead"
                    : "Heavy traffic ahead"}
              </b>
              <span className="dp-reroute-why">{reroute?.reason ?? "Finding a faster road"}</span>
            </span>
          </div>
        )}
      </div>

      <aside className="dp-nav-side">
        <div className="dp-nav-head">
          <div>
            <div className="dp-nav-call">{callsign}</div>
            <div className="dp-note">{shift?.vehicle_registration}</div>
          </div>
          <span className={`dp-link ${status}`} title={`Live link: ${status}`} />
        </div>

        <DpError error={error} />

        {checklistDue && <ChecklistReminder due={checklistDue} />}

        <EmergencyControl
          trip={trip}
          onChanged={reloadTrip}
          shiftId={shift?.id ?? null}
          checklistDue={checklistDue}
        />

        {trip && (
          <section className="dp-card">
            <h4>Your status</h4>
            <p className="dp-note">
              Shared live with the receiving hospital so they can prepare.
            </p>
            <div className="dp-stages">
              {STAGES.map((stage) => (
                <button
                  key={stage.value}
                  type="button"
                  className={`dp-stage${trip.stage === stage.value ? " on" : ""}`}
                  disabled={busyStage !== null}
                  onClick={() => void setStage(stage.value)}
                  title={stage.hint}
                >
                  {busyStage === stage.value ? "…" : stage.label}
                </button>
              ))}
            </div>
          </section>
        )}

        <section className="dp-card">
          <h4>Signals ahead</h4>
          {signals.length === 0 ? (
            <p className="dp-note">No junction is under priority control on this route.</p>
          ) : (
            signals.map(({ row, distanceM, secondsToGreen }, index) => (
              <div key={row.id} className={`dp-sig ${signalTone(row.state)}${index === 0 ? " next" : ""}`}>
                <div className="dp-sig-dist">{fmtDistance(distanceM)}</div>
                <div className="dp-sig-body">
                  <div className="dp-sig-name">{row.intersection || row.controller_id}</div>
                  <div className="dp-sig-state">{SIGNAL_COPY[row.state] ?? "Scheduled"}</div>
                </div>
                <div className="dp-sig-eta">
                  {row.state === "active" ? (
                    <span className="go">GREEN</span>
                  ) : secondsToGreen > 0 ? (
                    <>
                      <b>{Math.round(secondsToGreen)}</b>
                      <span>s</span>
                    </>
                  ) : (
                    <span className="dim">—</span>
                  )}
                </div>
              </div>
            ))
          )}
        </section>

        <section className="dp-card">
          <h4>Road ahead</h4>
          <p className="dp-note">
            {reroute
              ? reroute.reason
              : trip
                ? "Watching for closures and congestion on your route."
                : "No route to watch."}
          </p>
        </section>
      </aside>
    </div>
  );
}

/**
 * The emergency, and the rule about starting another one.
 *
 * A driver cannot open a second response while one is running. The server
 * enforces it too — this is the half that makes it comprehensible, by naming
 * the two ways out rather than only refusing.
 */
/**
 * The skipped inspection, come due.
 *
 * Shown on arrival and on every visit afterwards until the 21 items are
 * answered. It is not dismissible: the whole failure mode of an emergency skip
 * is that "later" never arrives, and a reminder with an X on it is a reminder
 * that gets an X pressed on it. The way out is the checklist, so that is the
 * only button.
 */
function ChecklistReminder({ due }: { due: ChecklistDue }) {
  return (
    <section className="dp-card checklist-due">
      <h4>
        Vehicle check outstanding
        <span className="dp-chip bad">
          {due.answered}/{due.total}
        </span>
      </h4>
      <p className="dp-lead">
        You reached your destination. The readiness check skipped earlier —
        “{due.skip_reason}” — must be completed before you take another patient.
      </p>
      <Link className="dp-btn primary wide" to="/d">
        Complete the 21-point check now
      </Link>
    </section>
  );
}

function EmergencyControl({
  trip,
  shiftId,
  onChanged,
  checklistDue,
}: {
  trip: Trip | null;
  shiftId: number | null;
  onChanged: () => Promise<void>;
  checklistDue: ChecklistDue | null;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    try {
      await action();
      await onChanged();
      setConfirming(false);
      setReason("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(null);
    }
  };

  if (!trip) {
    return (
      <section className="dp-card">
        <h4>Emergency</h4>
        <p className="dp-note">
          {checklistDue
            ? "Blocked until the outstanding vehicle check is completed."
            : "No emergency is running. You are available for dispatch."}
        </p>
        <DpError error={error} />
        <button
          type="button"
          className="dp-btn primary wide"
          // The server refuses this too - see `_outstanding_skip` in
          // crew_api.py. Disabling it here is so the driver is told why
          // before they press it, not instead of the server saying no.
          disabled={busy !== null || shiftId === null || checklistDue !== null}
          onClick={() =>
            void run("new", () => shiftApi.newEmergency(shiftId as number))
          }
        >
          {busy === "new"
            ? "Opening…"
            : checklistDue
              ? "Vehicle check required first"
              : "Start emergency"}
        </button>
      </section>
    );
  }

  const finished = ["handover", "cancelled"].includes(trip.stage);

  return (
    <section className="dp-card emergency">
      <h4>
        Emergency in progress
        <span className={`dp-chip l${trip.priority_level}`}>L{trip.priority_level}</span>
      </h4>
      <div className="dp-kv">
        <span>Reference</span>
        <b>{trip.reference}</b>
      </div>
      <div className="dp-kv">
        <span>Destination</span>
        <b>{trip.hospital_name ?? "not yet assigned"}</b>
      </div>

      <DpError error={error} />

      <div className="dp-locked">
        A new emergency cannot be started until this one is completed or cancelled.
      </div>

      <div className="dp-actions">
        <button
          type="button"
          className="dp-btn primary"
          disabled={busy !== null || finished}
          onClick={() => void run("complete", () => dispatchApi.handover(trip.id))}
        >
          {busy === "complete" ? "Completing…" : "Complete emergency"}
        </button>
        <button
          type="button"
          className="dp-btn danger-ghost"
          disabled={busy !== null || finished}
          onClick={() => setConfirming(true)}
        >
          Cancel emergency
        </button>
      </div>

      {confirming && (
        <div className="dp-skip">
          <label htmlFor="dpCancelWhy">Why is this emergency being cancelled?</label>
          <input
            id="dpCancelWhy"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="e.g. stood down by control, patient refused transport"
          />
          <div className="dp-actions">
            <button
              type="button"
              className="dp-btn danger"
              disabled={busy !== null || !reason.trim()}
              onClick={() => void run("cancel", () => dispatchApi.cancel(trip.id, reason))}
            >
              {busy === "cancel" ? "Cancelling…" : "Confirm cancellation"}
            </button>
            <button type="button" className="dp-btn ghost" onClick={() => setConfirming(false)}>
              Keep going
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

/** The single most important number on the screen, sized accordingly. */
function NextSignal({
  signal,
}: {
  signal: { row: Preemption; distanceM: number; secondsToGreen: number };
}) {
  const { row, distanceM, secondsToGreen } = signal;
  return (
    <div className={`dp-next-signal ${signalTone(row.state)}`}>
      <div className="dp-next-dist">{fmtDistance(distanceM)}</div>
      <div className="dp-next-copy">
        {row.state === "active"
          ? `Signal in ${fmtDistance(distanceM)} — green corridor ACTIVE`
          : row.state === "failed"
            ? `Signal in ${fmtDistance(distanceM)} — hold failed, expect red`
            : `Signal in ${fmtDistance(distanceM)} preparing for green corridor`}
        <span className="dp-next-sub">
          {row.intersection || row.controller_id}
          {row.state !== "active" && secondsToGreen > 0
            ? ` · green in ${Math.round(secondsToGreen)}s`
            : ""}
        </span>
      </div>
    </div>
  );
}

const SIGNAL_COPY: Record<string, string> = {
  active: "Green corridor active",
  armed: "Clearing cross traffic",
  planned: "Green corridor scheduled",
  released: "Released — normal timing",
  failed: "Hold failed — expect red",
  cancelled: "Cancelled",
};

function signalTone(state: string): string {
  if (state === "active") return "go";
  if (state === "armed") return "arming";
  if (state === "failed") return "fail";
  return "planned";
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
