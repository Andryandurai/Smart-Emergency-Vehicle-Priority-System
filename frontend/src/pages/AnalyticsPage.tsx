/**
 * AI Traffic Analytics Dashboard - features 4.8 and 4.9.
 *
 * Rendered as stat tiles and tables, matching what the API returns today.
 * Charts (Recharts) are Phase 10 in the migration plan; the data shapes here
 * are already chart-ready, so that phase is a rendering change rather than a
 * new set of endpoints.
 */
import { useCallback, useEffect, useState } from "react";

import { analytics } from "@/api/endpoints";
import type { AnalyticsSummary, Hotspot } from "@/api/types";
import { Dot, MapCanvas } from "@/components/MapCanvas";
import { Card, Empty, ErrorNote, Stat, fmtDuration } from "@/components/ui";

const WINDOWS = [7, 30, 90];

export function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [hotspots, setHotspots] = useState<Hotspot[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const [next, spots] = await Promise.all([
          analytics.summary(days, signal),
          analytics.accidentHotspots(signal),
        ]);
        setSummary(next);
        setHotspots(spots.hotspots);
        setError(null);
      } catch (err) {
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Could not load analytics.");
        }
      }
    },
    [days],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const rt = summary?.response_times;
  const cu = summary?.corridor_usage;
  const mv = summary?.movement;

  return (
    <div className="page scroll">
      <div className="page-head analytics-head">
        <div>
          <h1>AI Traffic Analytics</h1>
          <p>Response performance, green corridor usage and the network&rsquo;s problem locations.</p>
        </div>
        <div style={{ width: 180 }}>
          <label htmlFor="window">Window</label>
          <select id="window" value={days} onChange={(event) => setDays(Number(event.target.value))}>
            {WINDOWS.map((value) => (
              <option key={value} value={value}>Last {value} days</option>
            ))}
          </select>
        </div>
      </div>

      <ErrorNote error={error} />
      {!summary && !error && <Empty>Loading analytics…</Empty>}

      {rt && (
        <>
          <h2>Emergency response times</h2>
          <div className="stat-row">
            <Stat value={rt.trips} label="Trips" sub={`${rt.completed} completed · ${rt.cancelled} cancelled`} />
            <Stat value={rt.response_time.avg_min ? `${rt.response_time.avg_min}m` : "-"}
              label="Avg response" sub={`median ${fmtDuration(rt.response_time.median_s)}`} />
            <Stat value={fmtDuration(rt.response_time.p90_s)} label="p90 response"
              sub={`best ${fmtDuration(rt.response_time.best_s)}`} />
            <Stat value={fmtDuration(rt.transport_time.avg_s)} label="Avg transport"
              sub={`${rt.transport_time.count} measured`} />
            <Stat value={fmtDuration(rt.total_time.avg_s)} label="Avg call-to-hospital"
              sub="dispatch to arrival" />
          </div>
        </>
      )}

      {cu && (
        <>
          <h2>Green corridor usage &amp; cost</h2>
          <div className="stat-row">
            <Stat value={cu.preemptions_requested} label="Preemptions requested"
              sub={`${cu.activated} activated`} />
            <Stat value={cu.trips_with_corridor} label="Trips with a corridor"
              sub={`${cu.yielded} yielded to higher priority`} />
            <Stat value={fmtDuration(cu.total_hold_seconds)} label="Total cross-traffic hold"
              sub={`avg ${fmtDuration(cu.avg_hold_seconds)} per junction`} />
            <Stat value={cu.eta_accuracy.mean_abs_error_s !== null ? `${cu.eta_accuracy.mean_abs_error_s}s` : "-"}
              label="Mean ETA error" sub={`${cu.eta_accuracy.samples} samples`} />
            <Stat value={cu.failed} label="Controller failures" sub="fell back to normal timing" />
          </div>
        </>
      )}

      {summary && (
        <div className="grid-2" style={{ marginTop: 18 }}>
          <Card title="Congestion hotspots">
            <Table headers={["Road", "Avg speed", "Heavy share", "Samples"]}
              rows={summary.congestion_hotspots.map((row) => [
                row.name, `${row.avg_speed_kmh} km/h`,
                `${(row.heavy_share * 100).toFixed(0)}%`, String(row.samples),
              ])} />
          </Card>
          <Card title="High-delay intersections">
            <Table headers={["Junction", "Events", "Avg clearance", "ETA error"]}
              rows={summary.high_delay_intersections.map((row) => [
                row.name, String(row.events), `${row.avg_clearance_s}s`, `${row.avg_eta_error_s}s`,
              ])} />
          </Card>
        </div>
      )}

      <h2>Accident hotspots (feature 4.9)</h2>
      <div className="grid-2">
        <Card>
          <div className="hotspot-map">
            <MapCanvas zoom={12} className="map inner-map">
              {hotspots.map((spot) => (
                <Dot key={spot.id} position={[spot.latitude, spot.longitude]} colour="#e74c3c"
                  radius={5 + 10 * spot.score}>
                  <b>{spot.label}</b>
                  <br />
                  {spot.incident_count} incidents · score {(spot.score * 100).toFixed(0)}
                </Dot>
              ))}
            </MapCanvas>
          </div>
        </Card>
        <Card title="Identified locations">
          <Table headers={["Location", "Incidents", "Score"]}
            rows={hotspots.map((spot) => [
              spot.label || "-", String(spot.incident_count), (spot.score * 100).toFixed(0),
            ])} />
        </Card>
      </div>

      {mv && (
        <>
          <h2>Fleet movement</h2>
          <Card>
            <div className="stat-row">
              <Stat value={mv.fleet.total} label="Fleet size"
                sub={`${mv.fleet.online} online · ${mv.fleet.on_mission} on mission`} />
              <Stat value={`${mv.planned_distance_km} km`} label="Planned distance" sub="active routes" />
              <Stat value={mv.reroutes} label="Dynamic reroutes" sub="feature 4.10" />
              <Stat value={mv.telemetry_points.toLocaleString()} label="GPS fixes ingested" />
              <Stat value={mv.hospital_overrides} label="Crew overrides" sub="recommendation not followed" />
            </div>
            <div style={{ marginTop: 14 }}>
              <Table headers={["Emergency category", "Trips"]}
                rows={Object.entries(mv.trips_by_category).map(([k, v]) => [k, String(v)])} />
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

function Table({ headers, rows }: { headers: string[]; rows: string[][] }) {
  if (rows.length === 0) return <Empty>No data in this window.</Empty>;
  return (
    <table className="data">
      <thead>
        <tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index}>{row.map((cell, i) => <td key={i}>{cell}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}
