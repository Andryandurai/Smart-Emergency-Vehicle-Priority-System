/**
 * The crew boards - Drivers and Paramedics.
 *
 * One component behind both routes, because the two screens answer the same
 * question from opposite seats: who is on duty, who are they crewed with, and
 * what are they on. Building them separately would be two chances to disagree
 * about a single pairing, and a control room that sees a driver on AMB-104
 * while the paramedic board shows the same shift on AMB-107 has no usable
 * board at all.
 *
 * Everything is live. The roster is polled and the ops socket refreshes it on
 * any event that could change an assignment - a shift opening, a paramedic
 * accepting, a trip starting or ending, a vehicle moving.
 */
import { useCallback, useEffect, useState } from "react";

import { shifts as shiftApi } from "@/api/endpoints";
import type { CrewMember, CrewRoster } from "@/api/types";
import { Badge, ErrorNote, fmtDistance, fmtEta, fmtTime, levelClass } from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";

type Seat = "driver" | "paramedic";

/**
 * The tone a live status is drawn in.
 *
 * Three bands, because a supervisor scanning the board is asking one question
 * of each card: can I give this person a job? Green means yes, amber means
 * they are already on one, grey means they are not on shift at all. The
 * labels themselves are the server's - see ``_live_status`` in
 * apps/fleet/crew_api.py - so a status added there gets a sensible tone here
 * rather than a crash.
 */
function statusTone(status: string): "ok" | "warn" | "l4" {
  if (status === "Off Duty" || status === "Shift Ended") return "l4";
  if (status === "Available" || status === "On Duty") return "ok";
  return "warn";
}

export function DriversPage() {
  return <CrewBoard seat="driver" />;
}

export function ParamedicsPage() {
  return <CrewBoard seat="paramedic" />;
}

function CrewBoard({ seat }: { seat: Seat }) {
  const [roster, setRoster] = useState<CrewRoster | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [filter, setFilter] = useState<"all" | "on_duty" | "off_duty">("all");

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      setRoster(await shiftApi.roster(signal));
      setError(null);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError(err instanceof Error ? err.message : "Could not load the crew roster.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // The floor. Assignments change on somebody else's device, so a board left
  // open on a wall must not depend on this tab having been reopened.
  usePolling((signal) => refresh(signal), 5000);

  // And the socket for anything that changes a pairing or a job.
  useSocket("/ws/ops/", {
    handlers: {
      shift_claimed: () => void refresh(),
      shift_requested: () => void refresh(),
      shift_accepted: () => void refresh(),
      shift_ended: () => void refresh(),
      trip_created: () => void refresh(),
      trip_stage: () => void refresh(),
      hospital_assigned: () => void refresh(),
      vehicle_readiness: () => void refresh(),
      vehicle_position: () => void refresh(),
    },
  });

  const people = (seat === "driver" ? roster?.drivers : roster?.paramedics) ?? [];

  /**
   * Off duty is the server's answer, not `!on_duty`.
   *
   * `on_duty` is false for somebody mid-takeover as well - a driver standing
   * at an ambulance running its twenty-one point inspection is signed on and
   * halfway into a shift. Filing them under Off Duty would put a person who
   * is at work on the board of people who are not.
   */
  const onDuty = people.filter((person) => !person.off_duty);
  const offDuty = people.filter((person) => person.off_duty);

  const rows =
    filter === "on_duty" ? onDuty : filter === "off_duty" ? offDuty : people;

  const label = seat === "driver" ? "Driver" : "Paramedic";
  const partnerLabel = seat === "driver" ? "Paramedic" : "Driver";

  return (
    <div className="page scroll">
      <div className="page-head">
        <h1>{label}s</h1>
        <p>
          Every {label.toLowerCase()} on the roster and what they are doing right now —
          the ambulance they are crewing, who with, and the job they are on. Status
          follows the shift and the trip, and updates on its own.
        </p>
      </div>

      <ErrorNote error={error} />

      <div className="crew-toolbar">
        {roster && (
          <div className="crew-summary">
            <Stat value={people.length} label={`${label}s`} />
            <Stat value={onDuty.length} label="On duty" />
            <Stat value={offDuty.length} label="Off duty" />
          </div>
        )}

        <div className="fleet-filters crew-filters">
          {([
            ["all", `All ${label.toLowerCase()}s`],
            ["on_duty", "On duty"],
            ["off_duty", "Off duty"],
          ] as const).map(([value, text]) => (
            <button
              key={value}
              type="button"
              className={`chip${filter === value ? " on" : ""}`}
              onClick={() => setFilter(value)}
            >
              {text}
            </button>
          ))}
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="card">
          <p className="muted">
            {filter === "off_duty"
              ? `Every ${label.toLowerCase()} is signed on. Nobody is off duty.`
              : filter === "on_duty"
                ? `No ${label.toLowerCase()} is signed on right now.`
                : `No ${label.toLowerCase()} is on the roster.`}
          </p>
        </div>
      ) : (
        <div className="crew-grid">
          {rows.map((person) => (
            <CrewCard
              key={person.id}
              person={person}
              partnerLabel={partnerLabel}
              expanded={open === person.id}
              onToggle={() => setOpen(open === person.id ? null : person.id)}
            />
          ))}
        </div>
      )}

      {roster && (
        <p className="muted" style={{ fontSize: 11.5 }}>
          {rows.length} of {people.length} {label.toLowerCase()}
          {people.length === 1 ? "" : "s"} · updated {fmtTime(roster.generated_at)}.
        </p>
      )}
    </div>
  );
}

function CrewCard({
  person,
  partnerLabel,
  expanded,
  onToggle,
}: {
  person: CrewMember;
  partnerLabel: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const initials = person.name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0] ?? "")
    .join("")
    .toUpperCase();

  return (
    <article
      className={`crew-card${person.off_duty ? " off-duty" : person.on_duty ? " on-duty" : " signing-on"}`}
    >
      <button type="button" className="crew-card-head" onClick={onToggle} aria-expanded={expanded}>
        {person.avatar_url ? (
          <img className="crew-avatar" src={person.avatar_url} alt={person.name} />
        ) : (
          <span className="crew-avatar fallback">{initials}</span>
        )}
        <span className="crew-id">
          <span className="crew-name">{person.name}</span>
          <span className="muted small">
            {[person.staff_id, person.qualification].filter(Boolean).join(" · ") ||
              person.username}
          </span>
        </span>
        <span className="crew-flags">
          {/* The live state, not the shift record. `status` moves as the trip
              moves - Available, On Route, On Scene, Transporting - which is
              what somebody looking at this board is actually asking. The shift
              row below still carries the formal shift state. */}
          <Badge tone={statusTone(person.status)}>{person.status}</Badge>
          {person.mission_priority && (
            <Badge tone={levelClass(person.mission_priority) as "l1"}>
              L{person.mission_priority}
            </Badge>
          )}
        </span>
      </button>

      <div className="crew-assign">
        <Field label="Assigned ambulance" value={person.vehicle ?? "—"} strong />
        <Field label={`Assigned ${partnerLabel.toLowerCase()}`} value={person.partner ?? "—"} />
        <Field label="Current mission" value={person.mission ?? "None"} />
        <Field label="Shift" value={person.shift_status} />
      </div>

      {expanded && (
        <div className="crew-detail">
          <section>
            <h4>Profile</h4>
            <Field label="Username" value={person.username} />
            <Field label="Staff ID" value={person.staff_id || "—"} />
            <Field label="Qualification" value={person.qualification || "—"} />
            <Field label="Base station" value={person.base_station || "—"} />
            <Field label="Phone" value={person.phone || "—"} />
            <Field label="Blood group" value={person.blood_group || "—"} />
            <Field label="Email" value={person.email || "—"} />
          </section>

          <section>
            <h4>Mission</h4>
            <Field label="Reference" value={person.mission ?? "—"} />
            <Field label="Category" value={person.mission_category ?? "—"} />
            <Field label="Stage" value={person.mission_stage ?? "—"} />
            <Field label="Destination" value={person.mission_hospital ?? "—"} />
            <Field label="ETA" value={person.mission_eta ? fmtEta(person.mission_eta) : "—"} />
            <Field
              label="Distance remaining"
              value={
                person.monitoring.distance_remaining_m != null
                  ? fmtDistance(person.monitoring.distance_remaining_m)
                  : "—"
              }
            />
            <Field
              label="On duty since"
              value={person.on_duty_since ? fmtTime(person.on_duty_since) : "—"}
            />
          </section>

          <section>
            <h4>Monitoring</h4>
            <Field
              label="Position"
              value={
                person.monitoring.latitude != null
                  ? `${person.monitoring.latitude.toFixed(4)}, ${person.monitoring.longitude?.toFixed(4)}`
                  : "—"
              }
            />
            <Field
              label="Speed"
              value={
                person.monitoring.speed_kmh != null
                  ? `${Math.round(person.monitoring.speed_kmh)} km/h`
                  : "—"
              }
            />
            <Field label="Vehicle readiness" value={person.monitoring.vehicle_readiness ?? "—"} />
            <Field label="Inspection" value={person.monitoring.inspection ?? "—"} />
            <Field
              label="Last fix"
              value={
                person.monitoring.last_seen_at
                  ? `${fmtTime(person.monitoring.last_seen_at)}${person.monitoring.is_stale ? " (stale)" : ""}`
                  : "—"
              }
            />
          </section>
        </div>
      )}
    </article>
  );
}

function Field({
  label,
  value,
  strong = false,
}: {
  label: string;
  value: string;
  strong?: boolean;
}) {
  return (
    <div className="crew-field">
      <span>{label}</span>
      {strong ? <b className="strong">{value}</b> : <b>{value}</b>}
    </div>
  );
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div className="crew-stat">
      <b>{value}</b>
      <span>{label}</span>
    </div>
  );
}
