/**
 * The AI route and its alternatives.
 *
 * Every option states *why* it exists - "saves 3 min", "avoids a closure" -
 * because a driver asked to choose between three lines on a map with only
 * ETAs to go on will take the shortest one every time, which is exactly the
 * decision the routing engine was supposed to make for them.
 */
import type { RoutePreview } from "@/api/types";
import { fmtDistance } from "@/components/ui";

export interface RouteOption {
  id: string;
  label: string;
  route: RoutePreview;
  /** Why this option is on the list at all. */
  reason: string;
  /** Seconds saved against the recommended route. Negative is slower. */
  gainS: number;
  trafficLevel: "clear" | "moderate" | "heavy";
  isRecommended: boolean;
}

const TRAFFIC_COPY: Record<RouteOption["trafficLevel"], string> = {
  clear: "Clear",
  moderate: "Moderate",
  heavy: "Heavy",
};

export function RoutePanel({
  options,
  selectedId,
  onSelect,
  loading,
}: {
  options: RouteOption[];
  selectedId: string | null;
  onSelect: (option: RouteOption) => void;
  loading: boolean;
}) {
  return (
    <div className="nav-panel">
      <h4>Route options</h4>
      {loading && options.length === 0 && <p className="nav-empty">Computing routes…</p>}
      {!loading && options.length === 0 && (
        <p className="nav-empty">No destination set — no route to compare.</p>
      )}

      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          className={`route-opt${selectedId === option.id ? " selected" : ""}${
            option.isRecommended ? " recommended" : ""
          }`}
          onClick={() => onSelect(option)}
        >
          <div className="route-opt-head">
            <span className="route-label">
              {option.isRecommended && <span className="ai-tag">AI</span>}
              {option.label}
            </span>
            <span className={`route-traffic ${option.trafficLevel}`}>
              {TRAFFIC_COPY[option.trafficLevel]}
            </span>
          </div>
          <div className="route-opt-metrics">
            <span>
              <b>{Math.round(option.route.total_duration_min)}</b> min
            </span>
            <span>{fmtDistance(option.route.total_distance_m)}</span>
            <span>
              <b>{option.route.signalised_nodes.length}</b> signals
            </span>
            {option.gainS !== 0 && (
              <span className={option.gainS > 0 ? "gain" : "loss"}>
                {option.gainS > 0 ? "−" : "+"}
                {Math.abs(Math.round(option.gainS / 60))} min
              </span>
            )}
          </div>
          <div className="route-reason">{option.reason}</div>
        </button>
      ))}
    </div>
  );
}
