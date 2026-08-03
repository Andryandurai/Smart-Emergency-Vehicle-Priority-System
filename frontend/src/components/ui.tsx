import type { ReactNode } from "react";

import type { PriorityLevel, VehicleOwnership, VehiclePayload } from "@/api/types";

export const LEVEL_CLASS: Record<number, string> = { 1: "l1", 2: "l2", 3: "l3", 4: "l4" };
export const LEVEL_LABEL: Record<number, string> = {
  1: "Level 1 - Critical",
  2: "Level 2 - High",
  3: "Level 3 - Moderate",
  4: "Level 4 - Non-critical",
};

export function levelClass(level: PriorityLevel | number | null | undefined): string {
  return LEVEL_CLASS[level ?? 4] ?? "l4";
}

// ---------------------------------------------------------------- formatting
export function fmtTime(value: string | Date | null | undefined): string {
  if (!value) return "--:--";
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime())
    ? "--:--"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Relative ETA. Recomputed on render, so callers tick a timer to refresh. */
export function fmtEta(value: string | null | undefined): string {
  if (!value) return "no ETA";
  const seconds = (new Date(value).getTime() - Date.now()) / 1000;
  if (Number.isNaN(seconds)) return "no ETA";
  if (seconds < 0) return "arriving";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

export function fmtDistance(metres: number | null | undefined): string {
  if (metres === null || metres === undefined) return "-";
  return metres >= 1000 ? `${(metres / 1000).toFixed(1)} km` : `${Math.round(metres)} m`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? `${minutes}m ${Math.round(seconds % 60)}s`
    : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/**
 * Re-exported, not redefined.
 *
 * There were two congestion palettes - this one and the map's - and they had
 * already drifted apart, so a segment could read "heavy" in one place and be
 * drawn amber in another. The map module owns the palette; everything else
 * borrows it.
 */
export { congestionColour, trafficBand } from "@/components/map/layers";

// ---------------------------------------------------------------- components
export function Card({
  title,
  children,
  actions,
}: {
  title?: string;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          {title && <h3>{title}</h3>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({
  value,
  label,
  sub,
}: {
  value: ReactNode;
  label: string;
  sub?: ReactNode;
}) {
  return (
    <div className="stat">
      <div className="value">{value}</div>
      <div className="label">{label}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

export function Badge({
  tone = "l4",
  children,
}: {
  tone?: "l1" | "l2" | "l3" | "l4" | "ok" | "warn" | "bad";
  children: ReactNode;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

/** Fallback labels for a server that predates `ownership_display`. */
const OWNERSHIP_LABEL: Record<VehicleOwnership, string> = {
  government: "Government",
  private_hospital: "Private Hospital",
  private_service: "Private Ambulance Service",
  ngo: "NGO / Charitable Trust",
};

/**
 * Who operates a vehicle. Crews and dispatchers both need this before they
 * commit to a unit: it decides billing, the escalation contact and whether a
 * dispatch agreement covers the run.
 */
export function OwnershipTag({ vehicle }: { vehicle: Pick<VehiclePayload, "ownership" | "ownership_display"> }) {
  if (!vehicle.ownership) return null;
  return (
    <span className={`own-tag ${vehicle.ownership}`}>
      {vehicle.ownership_display ?? OWNERSHIP_LABEL[vehicle.ownership] ?? vehicle.ownership}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="badge bad" style={{ display: "block", padding: 9, marginBottom: 10 }}>
      {error}
    </div>
  );
}

/**
 * Shown wherever a trip is rendered for a role without clinical clearance.
 * Making the withholding explicit matters: a blank notes field could
 * otherwise read as "no clinical information recorded".
 */
export function RedactionNote() {
  return (
    <div className="meta muted">
      <Badge tone="warn">redacted</Badge> Patient details withheld for your role.
    </div>
  );
}

export type SocketState = "connecting" | "open" | "closed" | "unauthorised" | "forbidden";

const SOCKET_LABEL: Record<SocketState, string> = {
  connecting: "connecting",
  open: "live",
  closed: "reconnecting",
  unauthorised: "sign in required",
  forbidden: "not permitted",
};

/**
 * A refusal is not a disconnection. Showing "reconnecting" when the server has
 * decided this role may never open the socket would have an operator waiting
 * for a recovery that is never coming.
 */
export function ConnectionDot({
  status,
  missedFrames = 0,
}: {
  status: SocketState;
  missedFrames?: number;
}) {
  return (
    <div className="conn" data-state={status}>
      <span className="dot" />
      <span className="label">{SOCKET_LABEL[status]}</span>
      {missedFrames > 0 && (
        <span className="badge warn" title={`${missedFrames} frames missed`}>
          {missedFrames} dropped
        </span>
      )}
    </div>
  );
}
