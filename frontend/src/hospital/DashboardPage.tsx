/**
 * Tab 1 — Hospital Dashboard.
 *
 * The board. Everything on it is read-only: this screen answers "what is true
 * right now", and the one place those figures are changed is the Updates tab,
 * so a nurse reading the board can never be the reason it changed.
 *
 * Ordered by how urgently it is read. The headline strip is the six numbers a
 * charge nurse checks when the phone rings; team readiness and the ward table
 * are what they check when deciding whether to accept the next case.
 */
import { useOutletContext } from "react-router-dom";

import type { HospitalDashboard } from "@/api/types";
import { fmtTime } from "@/components/ui";
import type { HospitalOutletContext } from "@/hospital/HospitalShell";

const STATUS_COPY: Record<string, string> = {
  ready: "Accepting emergency arrivals",
  busy: "Under load — accepting with care",
  full: "Not accepting — full or on diversion",
};

export function DashboardPage() {
  const { board } = useOutletContext<HospitalOutletContext>();

  if (!board) return <div className="hp-loading">Loading the board…</div>;

  return (
    <div className="hp-page">
      <section className={`hp-hero ${board.status}`}>
        <div className="hp-hero-id">
          <h1>{board.hospital.name}</h1>
          <p>
            Hospital ID <b>{board.hospital.code}</b> · {board.hospital.city}
            {board.hospital.is_trauma_designated && " · Trauma centre"}
          </p>
        </div>
        <div className="hp-hero-status">
          <span className={`hp-status big ${board.status}`}>
            <span className="hp-status-dot" aria-hidden />
            {board.status}
          </span>
          <span className="hp-hero-copy">{STATUS_COPY[board.status]}</span>
        </div>
      </section>

      {board.hospital.is_on_diversion && (
        <div className="hp-warn">
          On diversion{board.hospital.diversion_reason && `: ${board.hospital.diversion_reason}`}.
          SEVPS will not route new patients here.
        </div>
      )}

      {board.is_stale && (
        <div className="hp-warn">
          These figures were last reported {fmtTime(board.reported_at)} and are stale. Update
          them so the recommender stops routing on old numbers.
        </div>
      )}

      <div className="hp-figures">
        <Figure
          value={board.active_ambulances_coming}
          label="Active Ambulances Coming"
          tone={board.active_ambulances_coming > 0 ? "live" : ""}
        />
        <Figure value={board.emergency_cases_today} label="Emergency Cases Today" />
        <Figure value={board.available_beds} label="Available Beds" tone={tone(board.available_beds)} />
        <Figure
          value={board.available_icu_beds}
          label="Available ICU Beds"
          tone={tone(board.available_icu_beds)}
        />
        <Figure
          value={board.available_ventilators}
          label="Available Ventilators"
          tone={tone(board.available_ventilators)}
        />
        <Figure
          value={board.available_operation_theatres}
          label="Available Operation Theatres"
          tone={tone(board.available_operation_theatres)}
        />
        <Figure value={board.emergency_staff_on_duty} label="Emergency Staff On Duty" />
      </div>

      <section className="hp-card">
        <h2>
          Hospital Team Ready
          <span className="hp-count">
            {board.teams_ready}/{board.teams_total} ready
          </span>
        </h2>
        <div className="hp-teams">
          {board.teams.map((team) => (
            <div key={team.field} className={`hp-team${team.ready ? " ready" : " not"}`}>
              <span className="hp-team-light" aria-hidden />
              <span className="hp-team-label">{team.label}</span>
              <span className="hp-team-state">{team.ready ? "Ready" : "Not ready"}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="hp-card">
        <h2>Beds / Ventilators Ready</h2>
        <table className="hp-table">
          <thead>
            <tr>
              <th>Resource</th>
              <th>Available</th>
              <th>Total</th>
              <th>In use</th>
            </tr>
          </thead>
          <tbody>
            {board.beds.map((bed) => (
              <tr key={bed.key}>
                <td>{bed.label}</td>
                <td>
                  <b className={tone(bed.available)}>{bed.available}</b>
                </td>
                <td className="hp-dim">{bed.total}</td>
                <td>
                  <Bar available={bed.available} total={bed.total} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="hp-card">
        <h2>Emergency floor</h2>
        <div className="hp-kv-grid">
          <KV label="Patients waiting" value={board.patients_waiting} />
          <KV label="Doctors on duty" value={board.doctors_on_duty} />
          <KV label="Workload index" value={`${Math.round(board.workload_index * 100)}%`} />
          <KV label="Capacity reported" value={fmtTime(board.reported_at)} />
          <KV label="Emergency phone" value={board.hospital.emergency_phone || "—"} />
        </div>
      </section>
    </div>
  );
}

function Figure({
  value,
  label,
  tone: figureTone = "",
}: {
  value: number;
  label: string;
  tone?: string;
}) {
  return (
    <div className={`hp-figure ${figureTone}`}>
      <b>{value}</b>
      <span>{label}</span>
    </div>
  );
}

function KV({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="hp-kv">
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}

/** Occupancy, drawn rather than described - a row of numbers reads slowly. */
function Bar({ available, total }: { available: number; total: number }) {
  const used = Math.max(0, total - available);
  const pct = total ? Math.round((used / total) * 100) : 0;
  return (
    <span className="hp-bar" title={`${used} of ${total} in use`}>
      <span
        className={`hp-bar-fill${pct >= 90 ? " full" : pct >= 70 ? " busy" : ""}`}
        style={{ width: `${pct}%` }}
      />
    </span>
  );
}

/** Zero of anything is the number that changes a decision, so it shouts. */
function tone(value: number): string {
  if (value <= 0) return "bad";
  if (value <= 2) return "warn";
  return "ok";
}

export type { HospitalDashboard };
