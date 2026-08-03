/**
 * Live fleet board.
 *
 * One row per ambulance, joining the four things an operations manager
 * otherwise correlates by hand: where it is, who is on it, what it is doing,
 * and whether it is fit to do it. Readiness and status are separate columns
 * on purpose - a vehicle can be Available and Not Ready at the same time, and
 * a board that collapses those into one word is a board that will eventually
 * send a grounded ambulance to a cardiac arrest.
 *
 * Polled *and* socket-driven: the socket carries the deltas, the poll is the
 * correctness floor for a dashboard left open on a wall screen overnight.
 */
import { useCallback, useMemo, useState } from "react";

import { driverOps } from "@/api/endpoints";
import type { FleetBoard, FleetRow, MaintenanceReport, VehicleReadiness } from "@/api/types";
import { Badge, Card, ConnectionDot, Empty, ErrorNote, Stat, fmtTime, levelClass } from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";
import { useAuthStore } from "@/stores/authStore";

const READINESS_TONE: Record<VehicleReadiness, "ok" | "warn" | "bad" | undefined> = {
  ready: "ok",
  temporarily_ready: "warn",
  not_ready: "bad",
  maintenance: "bad",
  unchecked: undefined,
};

export function FleetBoardPage() {
  const [board, setBoard] = useState<FleetBoard | null>(null);
  const [reports, setReports] = useState<MaintenanceReport[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "attention" | "on_call">("all");
  const isAdmin = useAuthStore(
    (state) => Boolean(state.user?.is_superuser) || Boolean(state.user?.roles.includes("administrators")),
  );

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setBoard(await driverOps.board(signal));
      setError(null);
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err instanceof Error ? err.message : "Could not load the fleet board.");
      }
    }
  }, []);

  usePolling(load, 6000);
  usePolling(
    (signal) => driverOps.maintenance(true, signal).then((r) => setReports(r.reports)),
    15000,
  );

  // The ops socket already carries every fleet event - reusing it rather than
  // opening a board-specific one keeps one fan-out path to reason about.
  const { status } = useSocket("/ws/ops/", {
    handlers: {
      vehicle_readiness: () => void load(),
      maintenance_report: () => void load(),
      ambulance_breakdown: () => void load(),
      transfer_accepted: () => void load(),
      shift_accepted: () => void load(),
      shift_ended: () => void load(),
      equipment_check_skipped: () => void load(),
      vehicle_status: () => void load(),
    },
  });

  const rows = useMemo(() => {
    const all = board?.vehicles ?? [];
    if (filter === "attention") {
      return all.filter(
        (row) =>
          row.readiness === "not_ready" ||
          row.readiness === "maintenance" ||
          row.inspection_status === "skipped - pending" ||
          row.inspection_status === "failed",
      );
    }
    if (filter === "on_call") return all.filter((row) => row.current_trip_reference);
    return all;
  }, [board, filter]);

  const override = async (row: FleetRow, readiness: VehicleReadiness) => {
    try {
      await driverOps.overrideReadiness(row.callsign, readiness, "Overridden from fleet board");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Override failed.");
    }
  };

  return (
    <div className="page scroll fleet-page">
      <div className="page-head fleet-head">
        <div>
          <h1>Fleet board</h1>
          <p>Every ambulance, live. Updated {fmtTime(board?.generated_at ?? null)}.</p>
        </div>
        <ConnectionDot status={status} />
      </div>

      <ErrorNote error={error} />

      {board && (
        <div className="stat-row fleet-stats">
          <Stat value={board.summary.total} label="Ambulances" />
          <Stat value={board.summary.ready} label="Ready" />
          <Stat value={board.summary.temporarily_ready} label="Temp. ready" />
          <Stat value={board.summary.not_ready} label="Not ready" />
          <Stat value={board.summary.maintenance} label="Maintenance" />
          <Stat value={board.summary.on_duty} label="Crewed" />
          <Stat value={board.summary.on_call} label="On a call" />
          <Stat value={board.summary.inspection_pending} label="Inspection due" />
        </div>
      )}

      <div className="fleet-filters">
        {([
          ["all", "All"],
          ["attention", "Needs attention"],
          ["on_call", "On a call"],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={`chip${filter === key ? " on" : ""}`}
            onClick={() => setFilter(key)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="fleet-table-wrap">
        <table className="data fleet-table">
          <thead>
            <tr>
              <th>Ambulance</th>
              <th>Location</th>
              <th>Driver</th>
              <th>Paramedic</th>
              <th>Status</th>
              <th>Vehicle</th>
              <th>Inspection</th>
              <th>Current emergency</th>
              <th>Shift</th>
              <th>Updated</th>
              {isAdmin && <th>Override</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.callsign} className={row.readiness === "not_ready" ? "grounded-row" : ""}>
                <td>
                  <div className="fl-callsign">{row.callsign}</div>
                  <div className="veh-reg small">{row.registration || "—"}</div>
                </td>
                <td className="mono small">
                  {row.latitude.toFixed(4)}, {row.longitude.toFixed(4)}
                  {row.is_stale && <div className="stale">stale fix</div>}
                </td>
                <td>{row.driver_name ?? <span className="muted">—</span>}</td>
                <td>{row.paramedic_name ?? <span className="muted">—</span>}</td>
                <td>{row.status_display ?? row.status}</td>
                <td>
                  <Badge tone={READINESS_TONE[row.readiness]}>{row.readiness_display}</Badge>
                </td>
                <td className={`insp ${row.inspection_status.replace(/[^a-z]/g, "")}`}>
                  {row.inspection_status}
                </td>
                <td>
                  {row.current_trip_reference ? (
                    <>
                      <div className="small">
                        <b>{row.current_emergency}</b>
                      </div>
                      <div className="small muted">
                        {row.current_trip_reference}
                        {row.current_destination ? ` → ${row.current_destination}` : ""}
                      </div>
                      {row.current_priority_level && (
                        <Badge tone={levelClass(row.current_priority_level) as "l1"}>
                          L{row.current_priority_level}
                        </Badge>
                      )}
                    </>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td className="small">{row.shift_status_display}</td>
                <td className="small muted">{fmtTime(row.updated_at)}</td>
                {isAdmin && (
                  <td>
                    <select
                      className="override-select"
                      value=""
                      onChange={(event) =>
                        event.target.value &&
                        void override(row, event.target.value as VehicleReadiness)
                      }
                    >
                      <option value="">Set…</option>
                      <option value="ready">Ready</option>
                      <option value="not_ready">Not ready</option>
                      <option value="maintenance">Maintenance</option>
                      <option value="unchecked">Unchecked</option>
                    </select>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <Empty>No ambulances match this filter.</Empty>}
      </div>

      <Card title="Open maintenance reports">
        {reports.length === 0 ? (
          <Empty>No open faults. Every ambulance is mechanically clear.</Empty>
        ) : (
          reports.map((report) => (
            <div key={report.id} className="trip l1">
              <div className="head">
                <span className="ref">
                  {report.vehicle} · {report.registration}
                </span>
                <Badge tone="bad">{report.state_display}</Badge>
              </div>
              <div className="meta">{report.reasons.join(" · ")}</div>
              {report.remarks && <pre className="rc-remarks">{report.remarks}</pre>}
              <div className="meta muted">
                Reported by {report.reported_by ?? "unknown"} at {fmtTime(report.reported_at)}
                {report.from_inspection ? " (from inspection)" : ""}
              </div>
              {isAdmin && (
                <div className="btn-row">
                  <button
                    type="button"
                    className="ghost"
                    onClick={() =>
                      void driverOps
                        .acknowledgeFault(report.id)
                        .then(() => driverOps.maintenance(true))
                        .then((r) => setReports(r.reports))
                    }
                  >
                    Acknowledge
                  </button>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() =>
                      void driverOps
                        .resolveFault(report.id, "Cleared by fleet management")
                        .then(() => driverOps.maintenance(true))
                        .then((r) => setReports(r.reports))
                        .then(() => load())
                    }
                  >
                    Resolve &amp; return to service
                  </button>
                </div>
              )}
            </div>
          ))
        )}
      </Card>
    </div>
  );
}
