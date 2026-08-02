/**
 * KPI tiles showing each metric's direction of travel.
 *
 * The arrow's colour comes from `improving`, which the **server** decides via
 * `higher_is_better`. That has to be server-side: "cross-traffic held: up 12%"
 * is bad news and "trips completed: up 12%" is good news, and a client
 * inferring it from the sign of the change gets one of them backwards. Getting
 * it backwards on an emergency dashboard is worse than showing no arrow.
 */
import type { TrendMetric } from "@/api/types";
import { formatValue } from "@/components/charts/primitives";

export function TrendTiles({ metrics }: { metrics: TrendMetric[] }) {
  if (metrics.length === 0) return null;
  return (
    <div className="trend-tiles">
      {metrics.map((metric) => (
        <TrendTile key={metric.key} metric={metric} />
      ))}
    </div>
  );
}

function TrendTile({ metric }: { metric: TrendMetric }) {
  const tone =
    metric.improving === null ? "flat" : metric.improving ? "good" : "bad";
  const arrow =
    metric.change_pct === null || metric.change_pct === 0
      ? "—"
      : metric.change_pct > 0
        ? "▲"
        : "▼";

  return (
    <div className={`trend-tile ${tone}`}>
      <div className="trend-label">{metric.label}</div>
      <div className="trend-value">{formatValue(metric.current, metric.unit)}</div>
      <div className="trend-change">
        <span className="trend-arrow">{arrow}</span>
        {metric.change_pct === null ? (
          // Distinguished from "no change": a metric with nothing to compare
          // against is not stable, it is unmeasured.
          <span className="muted">no comparison</span>
        ) : (
          <>
            {Math.abs(metric.change_pct)}% vs previous{" "}
            <span className="muted">({formatValue(metric.previous, metric.unit)})</span>
          </>
        )}
      </div>
    </div>
  );
}
