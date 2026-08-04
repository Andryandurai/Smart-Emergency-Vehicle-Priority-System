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
  const [filter, setFilter] = useState<"all" | "on_duty" | "on_call">("all");

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
  const rows = people.filter((person) =>
    filter === "on_duty" ? person.on_duty : filter === "on_call" ? Boolean(person.mission) : true,
  );

  const label = seat === "driver" ? "Driver" : "Paramedic";
  const partnerLabel = seat === "driver" ? "Paramedic" : "Driver";

  return (
    <div className="page scroll">
      <div className="page-head">
        <h1>{label}s</h1>
        <p>
          Who is on duty, the ambulance they are crewing, who with, and the job they are
          on. Updates live.
        </p>
      </div>

      <ErrorNote error={error} />

      {roster && (
        <div className="crew-summary">
          <Stat value={people.length} label={`${label}s`} />
          <Stat value={people.filter((p) => p.on_duty).length} label="On duty" />
          <Stat value={people.filter((p) => p.mission).length} label="On a call" />
          <Stat
            value={people.filter((p) => !p.on_duty).length}
            label="Off duty"
          />
        </div>
      )}

      <div className="fleet-filters">
        {([
          ["all", "All"],
          ["on_duty", "On duty"],
          ["on_call", "On a call"],
        ] as const).map(([value, text]) => (
          <button
            key={value}
            type="button"
            className={filter === value ? "active" : ""}
            onClick={() => setFilter(value)}
          >
            {text}
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <div className="card">
          <p className="muted">No {label.toLowerCase()} matches that filter.</p>
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
          Updated {fmtTime(roster.generated_at)}.
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
    <article className={`crew-card${person.on_duty ? " on-duty" : ""}${person.mission ? " on-call" : ""}`}>
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
          <Badge tone={person.on_duty ? "ok" : "warn"}>{person.shift_status}</Badge>
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
        <Field label="Current status" value={person.status} />
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
