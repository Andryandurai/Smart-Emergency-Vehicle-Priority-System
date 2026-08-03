/**
 * Start-of-shift: who is crewing this ambulance, and is it fit to go out.
 *
 * The takeover is a two-sided handshake on purpose. The driver opens it and
 * names their paramedic; the shift only becomes live once that paramedic
 * accepts. That is what makes "who was on AMB-101 at 09:40" answerable later,
 * rather than being one person's claim about who else was on board.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/api/client";
import { shifts } from "@/api/endpoints";
import type {
  CrewPerson,
  CrewShift,
  EquipmentAnswer,
  EquipmentCheckPayload,
  EquipmentItemSpec,
  MyShift,
} from "@/api/types";
import { Badge, Card, Empty, ErrorNote, fmtTime } from "@/components/ui";

interface ShiftTakeoverProps {
  /** The vehicle this screen is showing, pre-filled into the takeover form. */
  callsign: string;
  /** Username of the signed-in user, to work out which side of the handshake
   *  they are on without a second API call. */
  currentUsername: string;
  onShiftChange?: (shift: CrewShift | null) => void;
}

export function ShiftTakeover({
  callsign,
  currentUsername,
  onShiftChange,
}: ShiftTakeoverProps) {
  const [state, setState] = useState<MyShift | null>(null);
  const [crew, setCrew] = useState<CrewPerson[]>([]);
  const [catalogue, setCatalogue] = useState<EquipmentItemSpec[]>([]);
  const [chosenParamedic, setChosenParamedic] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const mine = await shifts.mine();
      setState(mine);
      onShiftChange?.(mine.shift);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your shift.");
    }
  }, [onShiftChange]);

  useEffect(() => {
    void refresh();
    shifts.crew().then((r) => setCrew(r.crew)).catch(() => setCrew([]));
    shifts
      .equipmentCatalogue()
      .then((r) => setCatalogue(r.items))
      .catch(() => setCatalogue([]));
  }, [refresh]);

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "That did not work.",
      );
    } finally {
      setBusy(null);
    }
  };

  const shift = state?.shift ?? null;
  const awaiting = state?.awaiting_my_acceptance ?? [];

  // --- a takeover is waiting on me to accept -------------------------------
  if (awaiting.length > 0) {
    return (
      <Card title="Crew sync request">
        <ErrorNote error={error} />
        {awaiting.map((request) => (
          <div key={request.id} className="sync-request">
            <div className="sync-head">
              <b>{request.driver_detail?.name ?? "A driver"}</b> wants to crew{" "}
              <b>{request.vehicle_callsign}</b> with you.
            </div>
            <div className="muted small">
              {request.vehicle_registration} · requested {fmtTime(request.requested_at)}
            </div>
            <div className="btn-row">
              <button
                type="button"
                className="primary-action"
                disabled={busy !== null}
                onClick={() => void run("accept", () => shifts.accept(request.id))}
              >
                {busy === "accept" ? "Accepting…" : "Accept & start shift"}
              </button>
              <button
                type="button"
                className="ghost"
                disabled={busy !== null}
                onClick={() =>
                  void run("decline", () => shifts.decline(request.id, "Declined by paramedic"))
                }
              >
                Decline
              </button>
            </div>
          </div>
        ))}
      </Card>
    );
  }

  // --- no shift: the driver opens one --------------------------------------
  if (!shift) {
    return (
      <Card title="Take over this ambulance">
        <ErrorNote error={error} />
        <p className="hint">
          As the driver, name the paramedic crewing with you. The shift starts once they
          accept on their own device.
        </p>
        <label htmlFor="paramedic">Paramedic on board</label>
        <select
          id="paramedic"
          value={chosenParamedic}
          onChange={(event) => setChosenParamedic(event.target.value)}
        >
          <option value="">Select a paramedic…</option>
          {crew
            .filter((person) => person.username !== currentUsername)
            .map((person) => (
              <option key={person.id} value={person.username}>
                {person.name} ({person.username})
              </option>
            ))}
        </select>
        <div className="btn-row">
          <button
            type="button"
            className="primary-action"
            disabled={!chosenParamedic || busy !== null}
            onClick={() =>
              void run("open", () => shifts.open(callsign, chosenParamedic))
            }
          >
            {busy === "open" ? "Sending…" : `Send sync request for ${callsign}`}
          </button>
        </div>
      </Card>
    );
  }

  // --- I opened a takeover, waiting on the paramedic -----------------------
  const iAmDriver = shift.driver_detail?.username === currentUsername;
  if (shift.status === "pending") {
    return (
      <Card title="Waiting for the paramedic">
        <ErrorNote error={error} />
        <div className="crew-pair">
          <CrewSeat label="Driver" person={shift.driver_detail} confirmed />
          <CrewSeat label="Paramedic" person={shift.paramedic_detail} confirmed={false} />
        </div>
        <p className="hint">
          {shift.paramedic_detail?.name} has not accepted yet. The shift is not live and the
          vehicle check cannot be recorded against it.
        </p>
        {iAmDriver && (
          <div className="btn-row">
            <button
              type="button"
              className="ghost"
              disabled={busy !== null}
              onClick={() => void run("end", () => shifts.end(shift.id))}
            >
              Cancel request
            </button>
          </div>
        )}
      </Card>
    );
  }

  // --- live shift ----------------------------------------------------------
  return (
    <>
      <Card
        title="Crew on duty"
        actions={<Badge tone="ok">{shift.vehicle_callsign}</Badge>}
      >
        <ErrorNote error={error} />
        <div className="crew-pair">
          <CrewSeat label="Driver" person={shift.driver_detail} confirmed />
          <CrewSeat label="Paramedic" person={shift.paramedic_detail} confirmed />
        </div>
        <div className="muted small" style={{ marginTop: 8 }}>
          On duty since {fmtTime(shift.accepted_at)} · {shift.vehicle_registration}
        </div>
        <div className="btn-row">
          <button
            type="button"
            className="ghost"
            disabled={busy !== null}
            onClick={() => void run("end", () => shifts.end(shift.id))}
          >
            End shift
          </button>
        </div>
      </Card>

      <DailyCheck
        shift={shift}
        catalogue={catalogue}
        onChanged={refresh}
      />
    </>
  );
}

function CrewSeat({
  label,
  person,
  confirmed,
}: {
  label: string;
  person: CrewPerson | null;
  confirmed: boolean;
}) {
  return (
    <div className={`crew-seat${confirmed ? " confirmed" : " pending"}`}>
      <span className="seat-label">{label}</span>
      <span className="seat-name">{person?.name ?? "—"}</span>
      <span className="seat-state">{confirmed ? "✓ confirmed" : "awaiting accept"}</span>
    </div>
  );
}

/**
 * The start-of-day vehicle check.
 *
 * The emergency skip is the load-bearing part. A crew handed a cardiac call
 * while still walking to the vehicle cannot stop to tick twenty boxes, and a
 * system that insists will simply be lied to - so skipping is a first-class,
 * attributed action, and the check stays outstanding until it is done.
 */
function DailyCheck({
  shift,
  catalogue,
  onChanged,
}: {
  shift: CrewShift;
  catalogue: EquipmentItemSpec[];
  onChanged: () => Promise<void>;
}) {
  const [check, setCheck] = useState<EquipmentCheckPayload | null>(shift.equipment_check);
  const [draft, setDraft] = useState<Record<string, EquipmentAnswer>>(
    shift.equipment_check?.items ?? {},
  );
  const [skipReason, setSkipReason] = useState("");
  const [showSkip, setShowSkip] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setCheck(shift.equipment_check);
    setDraft(shift.equipment_check?.items ?? {});
  }, [shift.equipment_check]);

  const answered = Object.keys(draft).length;
  const total = catalogue.length || check?.total || 0;
  const outstanding = check?.is_outstanding ?? true;

  const setAnswer = (code: string, present: boolean) =>
    setDraft((current) => ({ ...current, [code]: { present, note: current[code]?.note ?? "" } }));

  const save = async () => {
    setBusy("save");
    setError(null);
    try {
      setCheck(await shifts.saveChecklist(shift.id, draft));
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the check.");
    } finally {
      setBusy(null);
    }
  };

  const skip = async () => {
    if (!skipReason.trim()) {
      setError("Say why the check is being skipped — it stays on the record.");
      return;
    }
    setBusy("skip");
    setError(null);
    try {
      setCheck(await shifts.skipChecklist(shift.id, skipReason));
      setShowSkip(false);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not skip the check.");
    } finally {
      setBusy(null);
    }
  };

  const groups = catalogue.reduce<Record<string, EquipmentItemSpec[]>>((acc, item) => {
    (acc[item.group] ??= []).push(item);
    return acc;
  }, {});

  return (
    <Card
      title="Daily vehicle check"
      actions={
        check?.is_complete ? (
          <Badge tone="ok">complete</Badge>
        ) : check?.skipped ? (
          <Badge tone="bad">skipped — still owed</Badge>
        ) : (
          <Badge tone="warn">
            {answered}/{total}
          </Badge>
        )
      }
    >
      <ErrorNote error={error} />

      {check?.skipped && outstanding && (
        <div className="skip-note">
          Check skipped at {fmtTime(check.skipped_at)} — “{check.skip_reason}”. Still to be
          completed.
        </div>
      )}

      {check && check.missing_critical.length > 0 && (
        <div className="critical-note">
          <b>Critical equipment missing:</b>{" "}
          {check.missing_critical
            .map((code) => catalogue.find((i) => i.code === code)?.label ?? code)
            .join(", ")}
          . This vehicle is not fit for a Level 1 response.
        </div>
      )}

      {check?.is_complete && !open ? (
        <p className="muted small">
          Completed {fmtTime(check.completed_at)} by {check.completed_by}.{" "}
          <button type="button" className="linklike" onClick={() => setOpen(true)}>
            Review
          </button>
        </p>
      ) : (
        <>
          {!open ? (
            <div className="btn-row">
              <button type="button" className="primary-action" onClick={() => setOpen(true)}>
                Start vehicle check ({answered}/{total})
              </button>
              <button type="button" className="ghost danger" onClick={() => setShowSkip(true)}>
                Emergency skip
              </button>
            </div>
          ) : (
            <>
              {catalogue.length === 0 ? (
                <Empty>Equipment catalogue unavailable.</Empty>
              ) : (
                Object.entries(groups).map(([group, items]) => (
                  <div key={group} className="check-group">
                    <h5>{group}</h5>
                    {items.map((item) => {
                      const answer = draft[item.code];
                      return (
                        <div key={item.code} className="check-row">
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
                              Present
                            </button>
                            <button
                              type="button"
                              className={`yn no${answer?.present === false ? " on" : ""}`}
                              onClick={() => setAnswer(item.code, false)}
                            >
                              Missing
                            </button>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ))
              )}
              <div className="btn-row">
                <button
                  type="button"
                  className="primary-action"
                  disabled={busy !== null}
                  onClick={() => void save()}
                >
                  {busy === "save" ? "Saving…" : `Save check (${answered}/${total})`}
                </button>
                <button type="button" className="ghost" onClick={() => setOpen(false)}>
                  Close
                </button>
              </div>
            </>
          )}
        </>
      )}

      {showSkip && (
        <div className="skip-form">
          <label htmlFor="skipReason">
            Why is the check being skipped? (stays on the record)
          </label>
          <input
            id="skipReason"
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
    </Card>
  );
}
