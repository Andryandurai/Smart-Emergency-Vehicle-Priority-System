/**
 * Tab 1 — Take Over Ambulance.
 *
 * Four stages of one job, behind one tab, because they are one job: choose
 * the vehicle, inspect it, call a paramedic to it, and then hold the shift
 * until it ends.
 *
 * The stage is derived from server state on every load rather than held as a
 * wizard step. A driver who closes the app between claiming the ambulance and
 * finishing the inspection reopens it exactly where they were, and a driver
 * whose paramedic accepts while the screen is shut sees an active shift when
 * they look again — neither of which is true of a client-side step counter.
 *
 * The order is load-bearing. The paramedic is called *after* the inspection,
 * never before: a colleague summoned to an ambulance that turns out to have
 * failed brakes is the one person the driver most needs still available.
 */
import { useCallback, useEffect, useState } from "react";
import { useOutletContext } from "react-router-dom";

import { ApiError } from "@/api/client";
import { shifts as shiftApi } from "@/api/endpoints";
import type {
  CrewPerson,
  CrewShift,
  EquipmentAnswer,
  EquipmentCheckPayload,
  EquipmentItemSpec,
  ReadinessOutcome,
  SelectableVehicle,
} from "@/api/types";
import type { DriverOutletContext } from "@/driver/DriverShell";
import { useAuthStore } from "@/stores/authStore";

type Check = (EquipmentCheckPayload & Partial<ReadinessOutcome>) | null;

export function TakeoverPage() {
  const { shift, refreshShift } = useOutletContext<DriverOutletContext>();
  const username = useAuthStore((state) => state.user?.username ?? "");
  const [check, setCheck] = useState<Check>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * Re-open the checklist after a failed inspection.
   *
   * Readiness is derived from the stored answers, so a grounded vehicle stays
   * grounded on every reload. Correct — but without this flag "Re-inspect
   * after repair" refreshed straight back onto the grounded screen, with no
   * way to record that the tyre had been changed.
   */
  const [reinspecting, setReinspecting] = useState(false);

  const reload = useCallback(async () => {
    try {
      const mine = await shiftApi.mine();
      setCheck(mine.shift?.equipment_check ?? null);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your shift.");
    } finally {
      setLoaded(true);
    }
    await refreshShift();
  }, [refreshShift]);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (!loaded) return <div className="dp-loading">Loading your shift…</div>;

  // `vehicle_readiness` is only present on a checklist response; the shift's
  // stored check carries `readiness`. Prefer the fresher of the two.
  const readiness = check?.vehicle_readiness ?? check?.readiness;
  const settled =
    Boolean(check?.is_complete) || Boolean(check?.skipped) || readiness === "not_ready";

  if (!shift) return <VehiclePicker onClaimed={reload} outerError={error} />;

  if (shift.status === "draft" && readiness === "not_ready" && !reinspecting) {
    return <Grounded shift={shift} check={check} onRecheck={() => setReinspecting(true)} />;
  }

  if (shift.status === "draft" && (!settled || reinspecting)) {
    return (
      <Checklist
        shift={shift}
        onDone={async () => {
          setReinspecting(false);
          await reload();
        }}
      />
    );
  }

  if (shift.status === "draft") {
    return (
      <CallParamedic
        shift={shift}
        check={check}
        currentUsername={username}
        onSent={reload}
      />
    );
  }

  if (shift.status === "pending") return <AwaitingParamedic shift={shift} onCancel={reload} />;

  if (!settled) return <Checklist shift={shift} onDone={reload} />;
  if (readiness === "not_ready") {
    return <Grounded shift={shift} check={check} onRecheck={() => setReinspecting(true)} />;
  }

  return <OnDuty shift={shift} check={check} onEnded={reload} />;
}

// ---------------------------------------------------------------------------
// Step 1 — which ambulance
// ---------------------------------------------------------------------------
function VehiclePicker({
  onClaimed,
  outerError,
}: {
  onClaimed: () => Promise<void>;
  outerError: string | null;
}) {
  const [vehicles, setVehicles] = useState<SelectableVehicle[]>([]);
  const [chosen, setChosen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    shiftApi
      .selectableVehicles()
      .then((r) => setVehicles(r.vehicles))
      .catch(() => setVehicles([]));
  }, []);

  useEffect(load, [load]);

  const claim = async () => {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      await shiftApi.claim(chosen);
      await onClaimed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not take that ambulance.");
      // Somebody else may have claimed it while this screen was open.
      load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dp-page">
      <Stepper step={1} />
      <h2 className="dp-h2">Choose your ambulance</h2>
      <p className="dp-lead">
        Only vehicles that are free, uncrewed and not grounded appear here. You will
        inspect the one you take before anyone is called to it.
      </p>

      <DpError error={outerError ?? error} />

      {vehicles.length === 0 ? (
        <div className="dp-empty">
          No ambulance is free to take over. Every vehicle is either crewed, on a call,
          or grounded for maintenance.
        </div>
      ) : (
        <div className="dp-veh-grid">
          {vehicles.map((vehicle) => (
            <button
              key={vehicle.callsign}
              type="button"
              className={`dp-veh${chosen === vehicle.callsign ? " chosen" : ""}`}
              onClick={() => setChosen(vehicle.callsign)}
            >
              <div className="dp-veh-call">{vehicle.callsign}</div>
              <div className="dp-veh-plate">{vehicle.registration || "no plate"}</div>
              <div className="dp-veh-meta">
                {vehicle.ownership_display ?? "—"}
                {vehicle.is_als ? " · ALS" : " · BLS"}
              </div>
              <div className="dp-veh-meta dim">{vehicle.home_station ?? "No home station"}</div>
              <span className={`dp-chip ${vehicle.readiness === "ready" ? "ok" : "warn"}`}>
                {vehicle.readiness_display ?? "Not yet inspected"}
              </span>
            </button>
          ))}
        </div>
      )}

      {chosen && (
        <div className="dp-actions">
          <button type="button" className="dp-btn primary" disabled={busy} onClick={() => void claim()}>
            {busy ? "Taking over…" : `Take over ${chosen} → inspect`}
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — vehicle readiness, all 21 items
// ---------------------------------------------------------------------------
function Checklist({ shift, onDone }: { shift: CrewShift; onDone: () => Promise<void> }) {
  const [catalogue, setCatalogue] = useState<EquipmentItemSpec[]>([]);
  const [draft, setDraft] = useState<Record<string, EquipmentAnswer>>(
    shift.equipment_check?.items ?? {},
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSkip, setShowSkip] = useState(false);
  const [skipReason, setSkipReason] = useState("");

  useEffect(() => {
    shiftApi
      .equipmentCatalogue()
      .then((r) => setCatalogue(r.items))
      .catch(() => setCatalogue([]));
  }, []);

  const answered = Object.keys(draft).length;
  const failedCritical = catalogue.filter(
    (item) => item.critical && draft[item.code]?.present === false,
  );

  const answer = (code: string, present: boolean) =>
    setDraft((current) => ({ ...current, [code]: { present, note: current[code]?.note ?? "" } }));

  const note = (code: string, text: string) =>
    setDraft((current) => ({
      ...current,
      [code]: { present: current[code]?.present ?? false, note: text },
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

  /**
   * Emergency skip.
   *
   * The load-bearing part of this screen. A crew handed a cardiac call while
   * still walking to the vehicle cannot stop to tick twenty-one boxes, and a
   * system that insists will simply be lied to — so skipping is a first-class,
   * attributed action, and the check stays outstanding until it is done.
   */
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
    <div className="dp-page">
      <Stepper step={2} />
      <h2 className="dp-h2">
        Vehicle readiness
        <span className="dp-count">
          {answered}/{catalogue.length || 21}
        </span>
      </h2>
      <p className="dp-lead">
        {shift.vehicle_callsign} · {shift.vehicle_registration || "no plate"}. Mark every
        item. A failed critical item grounds the ambulance and no paramedic is called
        to it.
      </p>

      <DpError error={error} />

      {failedCritical.length > 0 && (
        <div className="dp-critical">
          <b>{failedCritical.length} critical item(s) not ready.</b> Submitting will mark{" "}
          {shift.vehicle_callsign} NOT READY, raise a maintenance report and prevent
          dispatch.
        </div>
      )}

      {Object.entries(groups).map(([group, items]) => (
        <section key={group} className="dp-check-group">
          <h4>{group}</h4>
          {items.map((item) => {
            const current = draft[item.code];
            return (
              <div key={item.code} className="dp-check-row">
                <span className="dp-check-label">
                  {item.label}
                  {item.critical && <span className="dp-crit" title="Critical" />}
                </span>
                <span className="dp-yn">
                  <button
                    type="button"
                    className={`dp-y${current?.present === true ? " on" : ""}`}
                    onClick={() => answer(item.code, true)}
                  >
                    Ready
                  </button>
                  <button
                    type="button"
                    className={`dp-n${current?.present === false ? " on" : ""}`}
                    onClick={() => answer(item.code, false)}
                  >
                    Not ready
                  </button>
                </span>
                {current?.present === false && (
                  <input
                    className="dp-check-note"
                    placeholder="Remarks (optional)"
                    value={current.note ?? ""}
                    onChange={(event) => note(item.code, event.target.value)}
                  />
                )}
              </div>
            );
          })}
        </section>
      ))}

      <div className="dp-actions">
        <button type="button" className="dp-btn primary" disabled={busy !== null} onClick={() => void submit()}>
          {busy === "save" ? "Submitting…" : `Submit inspection (${answered}/${catalogue.length || 21})`}
        </button>
        <button type="button" className="dp-btn danger-ghost" onClick={() => setShowSkip(true)}>
          Emergency skip
        </button>
      </div>

      {showSkip && (
        <div className="dp-skip">
          <p>
            The ambulance becomes <b>Temporarily Ready</b> and can be dispatched at once.
            Administrators are notified and the inspection must still be completed once
            the emergency ends.
          </p>
          <label htmlFor="dpSkipWhy">Why is the inspection being skipped?</label>
          <input
            id="dpSkipWhy"
            value={skipReason}
            onChange={(event) => setSkipReason(event.target.value)}
            placeholder="e.g. cardiac call received while boarding"
          />
          <div className="dp-actions">
            <button type="button" className="dp-btn danger" disabled={busy !== null} onClick={() => void skip()}>
              {busy === "skip" ? "Recording…" : "Skip and go now"}
            </button>
            <button type="button" className="dp-btn ghost" onClick={() => setShowSkip(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Grounded
// ---------------------------------------------------------------------------
function Grounded({
  shift,
  check,
  onRecheck,
}: {
  shift: CrewShift;
  check: Check;
  onRecheck: () => void;
}) {
  return (
    <div className="dp-page">
      <div className="dp-hero bad">
        <span className="dp-hero-tag">Not ready</span>
        <h1>{shift.vehicle_callsign}</h1>
        <p>
          This ambulance cannot be dispatched. Fleet management and the control room have
          been notified and a maintenance report has been raised.
        </p>
      </div>

      <div className="dp-critical">
        <b>Failed:</b> {(check?.missing_critical ?? []).join(", ") || "critical items"}
      </div>

      {check?.maintenance_report && (
        <section className="dp-card">
          <h4>Maintenance report #{check.maintenance_report.id}</h4>
          <div className="dp-kv">
            <span>State</span>
            <b>{check.maintenance_report.state_display}</b>
          </div>
          <div className="dp-kv">
            <span>Reasons</span>
            <b>{check.maintenance_report.reasons.join(" · ")}</b>
          </div>
          <pre className="dp-pre">{check.maintenance_report.remarks}</pre>
        </section>
      )}

      <div className="dp-actions">
        <button type="button" className="dp-btn primary" onClick={onRecheck}>
          Re-inspect after repair
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3 — call the paramedic
// ---------------------------------------------------------------------------
function CallParamedic({
  shift,
  check,
  currentUsername,
  onSent,
}: {
  shift: CrewShift;
  check: Check;
  currentUsername: string;
  onSent: () => Promise<void>;
}) {
  const [crew, setCrew] = useState<CrewPerson[]>([]);
  const [chosen, setChosen] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    shiftApi.crew().then((r) => setCrew(r.crew)).catch(() => setCrew([]));
  }, []);

  const send = async () => {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      await shiftApi.requestParamedic(shift.id, chosen);
      await onSent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send the request.");
    } finally {
      setBusy(false);
    }
  };

  const selectable = crew.filter((person) => person.username !== currentUsername);

  return (
    <div className="dp-page">
      <Stepper step={3} />
      <h2 className="dp-h2">Choose your paramedic</h2>
      <p className="dp-lead">
        {shift.vehicle_callsign} is inspected. Send the sync request — the shift starts
        the moment they accept on their own device.
      </p>

      <DpError error={error} />

      <div className={`dp-ready-banner${check?.skipped ? " warn" : ""}`}>
        <span className="dp-chip ok">{check?.skipped ? "Temporarily ready" : "Ready for service"}</span>
        <span>
          {shift.vehicle_callsign} · {shift.vehicle_registration || "no plate"}
          {check?.skipped && " — inspection skipped, still outstanding"}
        </span>
      </div>

      {selectable.length === 0 ? (
        <div className="dp-empty">
          No paramedic is available to crew with. Ask your control room to roster one.
        </div>
      ) : (
        <div className="dp-crew-grid">
          {selectable.map((person) => (
            <button
              key={person.username}
              type="button"
              className={`dp-crew${chosen === person.username ? " chosen" : ""}`}
              onClick={() => setChosen(person.username)}
            >
              <span className="dp-crew-name">{person.name}</span>
              <span className="dp-crew-user">{person.username}</span>
            </button>
          ))}
        </div>
      )}

      <div className="dp-actions">
        <button
          type="button"
          className="dp-btn primary"
          disabled={!chosen || busy}
          onClick={() => void send()}
        >
          {busy ? "Sending…" : "Send sync request"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Waiting on the paramedic
// ---------------------------------------------------------------------------
function AwaitingParamedic({
  shift,
  onCancel,
}: {
  shift: CrewShift;
  onCancel: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cancel = async () => {
    setBusy(true);
    setError(null);
    try {
      await shiftApi.end(shift.id);
      await onCancel();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not cancel the request.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dp-page">
      <div className="dp-hero wait">
        <span className="dp-hero-tag">Sync sent</span>
        <h1>{shift.vehicle_callsign}</h1>
        <p>
          Waiting for <b>{shift.paramedic_detail?.name ?? "your paramedic"}</b> to accept.
        </p>
      </div>

      <DpError error={error} />

      <section className="dp-card">
        <div className="dp-spinner" aria-hidden />
        <p className="dp-lead">
          Their portal is showing your request now. This screen updates on its own — the
          shift goes live the moment they accept, and no emergency can be started until
          it does.
        </p>
      </section>

      <div className="dp-actions">
        <button type="button" className="dp-btn ghost" disabled={busy} onClick={() => void cancel()}>
          {busy ? "Cancelling…" : "Cancel request"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// On duty
// ---------------------------------------------------------------------------
function OnDuty({
  shift,
  check,
  onEnded,
}: {
  shift: CrewShift;
  check: Check;
  onEnded: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const end = async () => {
    setBusy(true);
    setError(null);
    try {
      await shiftApi.end(shift.id);
      await onEnded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not end the shift.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dp-page">
      <div className="dp-hero go">
        <span className="dp-hero-tag">On duty</span>
        <h1>{shift.vehicle_callsign}</h1>
        <p>{shift.vehicle_registration || "no plate"}</p>
      </div>

      <DpError error={error} />

      {check?.skipped && check.is_outstanding && (
        <div className="dp-critical warn">
          Inspection was skipped — <b>Temporarily Ready</b>. Complete the 21-point check
          once this emergency ends.
        </div>
      )}

      <section className="dp-card">
        <h4>Crew</h4>
        <div className="dp-kv">
          <span>Driver</span>
          <b>{shift.driver_detail?.name ?? "—"} (you)</b>
        </div>
        <div className="dp-kv">
          <span>Paramedic</span>
          <b>{shift.paramedic_detail?.name ?? "—"}</b>
        </div>
        <p className="dp-note">
          Both seats are confirmed. Your paramedic sees the same shift on their portal.
        </p>
      </section>

      <div className="dp-actions">
        <button type="button" className="dp-btn ghost" disabled={busy} onClick={() => void end()}>
          {busy ? "Ending…" : "End shift"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// shared bits
// ---------------------------------------------------------------------------
function Stepper({ step }: { step: 1 | 2 | 3 }) {
  const labels = ["Ambulance", "Readiness", "Paramedic"];
  return (
    <ol className="dp-stepper">
      {labels.map((label, index) => (
        <li
          key={label}
          className={index + 1 === step ? "now" : index + 1 < step ? "done" : ""}
        >
          <span className="dp-step-n">{index + 1}</span>
          {label}
        </li>
      ))}
    </ol>
  );
}

export function DpError({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="dp-error">{error}</div>;
}
