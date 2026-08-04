/**
 * Ambulances — one card per vehicle, with the crew, the check and the job.
 *
 * Distinct from the Fleet tab on purpose. Fleet is a triage table: one row per
 * ambulance, scanned down a readiness column to find the ones that need
 * attention. This is the per-vehicle view - who is on it, what it is carrying,
 * where it is - which a table of ten columns cannot show without becoming
 * unreadable.
 *
 * Built from `/fleet/board/`, which already carries every field this needs, so
 * the two screens cannot disagree about a vehicle's state.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { driverOps } from "@/api/endpoints";
import type { FleetBoard, FleetRow } from "@/api/types";
import { Badge, ErrorNote, fmtEta, fmtTime, levelClass } from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";

/** Fitness for dispatch, as a tone for the readiness chip. */
const READINESS_TONE: Record<string, "ok" | "warn" | "bad"> = {
  ready: "ok",
  temporarily_ready: "warn",
  unchecked: "warn",
  not_ready: "bad",
  maintenance: "bad",
};

export function AmbulancesPage() {
  const [board, setBoard] = useState<FleetBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      setBoard(await driverOps.board(signal));
      setError(null);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError(err instanceof Error ? err.message : "Could not load the ambulances.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  usePolling((signal) => refresh(signal), 4000);

  /**
   * Every event that can change what a card says.
   *
   * The three portals all write here: a driver claiming a vehicle or finishing
   * an inspection, a paramedic accepting a shift or assigning a hospital, a
   * hospital receiving a patient. Each ends in one of these events, so the
   * board follows all three without any of them knowing it exists.
   */
  useSocket("/ws/ops/", {
    handlers: {
      vehicle_position: () => void refresh(),
      vehicle_readiness: () => void refresh(),
      shift_claimed: () => void refresh(),
      shift_requested: () => void refresh(),
      shift_accepted: () => void refresh(),
      shift_ended: () => void refresh(),
      trip_created: () => void refresh(),
      trip_stage: () => void refresh(),
      hospital_assigned: () => void refresh(),
      maintenance_report: () => void refresh(),
      patient_received: () => void refresh(),
      patient_admitted: () => void refresh(),
    },
  });

  const rows = useMemo(() => {
    const list = board?.vehicles ?? [];
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter(
      (row) =>
        row.callsign.toLowerCase().includes(q) ||
        (row.registration ?? "").toLowerCase().includes(q) ||
        (row.driver_name ?? "").toLowerCase().includes(q) ||
        (row.paramedic_name ?? "").toLowerCase().includes(q) ||
        (row.current_emergency ?? "").toLowerCase().includes(q),
    );
  }, [board, query]);

  return (
    <div className="page scroll">
      <div className="page-head">
        <h1>Ambulances</h1>
        <p>
          Every vehicle in the fleet, its crew, its last inspection and the job it is on.
          Updates live from the driver, paramedic and hospital portals.
        </p>
      </div>

      <ErrorNote error={error} />

      <input
        className="amb-search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search callsign, plate, crew or emergency"
      />

      {rows.length === 0 ? (
        <div className="card">
          <p className="muted">No ambulance matches that search.</p>
        </div>
      ) : (
        <div className="amb-grid">
          {rows.map((row) => (
            <AmbulanceCard
              key={row.callsign}
              row={row}
              expanded={open === row.callsign}
              onToggle={() => setOpen(open === row.callsign ? null : row.callsign)}
            />
          ))}
        </div>
      )}

      {board && (
        <p className="muted" style={{ fontSize: 11.5 }}>
          {board.count} ambulance{board.count === 1 ? "" : "s"} · updated{" "}
          {fmtTime(board.generated_at)}.
        </p>
      )}
    </div>
  );
}

function AmbulanceCard({
  row,
  expanded,
  onToggle,
}: {
  row: FleetRow;
  expanded: boolean;
  onToggle: () => void;
}) {
  const onCall = Boolean(row.current_trip_reference);
  return (
    <article className={`amb-card${onCall ? " on-call" : ""}${row.readiness === "not_ready" ? " grounded" : ""}`}>
      <button type="button" className="amb-card-head" onClick={onToggle} aria-expanded={expanded}>
        <span className="amb-id">
          <span className="amb-callsign">{row.callsign}</span>
          <span className="amb-plate">{row.registration || "no plate"}</span>
        </span>
        <span className="amb-flags">
          <Badge tone={READINESS_TONE[row.readiness] ?? "warn"}>{row.readiness_display}</Badge>
          <Badge tone={onCall ? "bad" : "ok"}>{row.status_display ?? row.status}</Badge>
          {row.current_priority_level && (
            <Badge tone={levelClass(row.current_priority_level) as "l1"}>
              L{row.current_priority_level}
            </Badge>
          )}
        </span>
      </button>

      <div className="amb-crew">
        <Row label="Driver" value={row.driver_name ?? "Uncrewed"} />
        <Row label="Paramedic" value={row.paramedic_name ?? "Uncrewed"} />
        <Row label="Mission" value={row.current_trip_reference ?? "Standing by"} />
        <Row label="Category" value={row.current_emergency ?? "—"} />
      </div>

      {expanded && (
        <div className="amb-detail">
          <section>
            <h4>Ambulance details</h4>
            <Row label="Type" value={row.vehicle_type_display ?? row.vehicle_type} />
            <Row label="Operator" value={row.operator || "—"} />
            <Row label="Ownership" value={row.ownership_display ?? "—"} />
            <Row label="Capability" value={row.is_als ? "Advanced Life Support" : "Basic Life Support"} />
            <Row label="Registration" value={row.registration || "—"} />
          </section>

          <section>
            <h4>Quality check</h4>
            <Row label="Readiness" value={row.readiness_display} />
            <Row label="Inspection" value={row.inspection_status || "—"} />
            <Row label="Shift" value={row.shift_status_display || "—"} />
            <Row
              label="On duty since"
              value={row.on_duty_since ? fmtTime(row.on_duty_since) : "—"}
            />
          </section>

          <section>
            <h4>Current location</h4>
            <Row
              label="Position"
              value={`${row.latitude.toFixed(4)}, ${row.longitude.toFixed(4)}`}
            />
            <Row label="Speed" value={`${Math.round(row.speed_kmh)} km/h`} />
            <Row label="Heading" value={`${Math.round(row.heading_deg)}°`} />
            <Row
              label="Last fix"
              value={
                row.last_seen_at
                  ? `${fmtTime(row.last_seen_at)}${row.is_stale ? " (stale)" : ""}`
                  : "—"
              }
            />
          </section>

          <section>
            <h4>Current mission</h4>
            <Row label="Reference" value={row.current_trip_reference ?? "—"} />
            <Row label="Mission category" value={row.current_emergency ?? "—"} />
            <Row label="Destination" value={row.current_destination ?? "—"} />
            <Row label="ETA" value={row.current_eta ? fmtEta(row.current_eta) : "—"} />
          </section>
        </div>
      )}
    </article>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="amb-row">
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}
