/**
 * AI Traffic Analytics Dashboard - features 4.8 and 4.9.
 *
 * Phase 10 turned this from stat tiles and tables into charts. The tiles and
 * tables were kept, not replaced: a chart shows a shape and a table gives you
 * the number to put in a report, and a control room needs both. The existing
 * `analytics.summary` call is unchanged and still drives the tiles.
 *
 * Every chart has a matching CSV export of exactly the data it is drawn from,
 * because the alternative is someone re-typing numbers out of a screenshot.
 */
import { useCallback, useEffect, useState } from "react";

import { analytics, charts } from "@/api/endpoints";
import type {
  AnalyticsSummary,
  CategoryDistribution,
  CorridorOutcomes,
  DailyTrends,
  DemandProfile,
  ExportDataset,
  HospitalLoad,
  Hotspot,
  ResponseDistribution,
  TrendSummary,
} from "@/api/types";
import {
  DemandChart,
  DistributionDonut,
  HorizontalBars,
  ResponseHistogram,
  StackedBars,
  TrendLines,
} from "@/components/charts/primitives";
import { TrendTiles } from "@/components/charts/TrendTiles";
import { Dot, MapCanvas } from "@/components/MapCanvas";
import { Badge, Card, Empty, ErrorNote, Stat, fmtDuration } from "@/components/ui";

const WINDOWS = [7, 30, 90];

export function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [hotspots, setHotspots] = useState<Hotspot[]>([]);
  const [trends, setTrends] = useState<DailyTrends | null>(null);
  const [trendSummary, setTrendSummary] = useState<TrendSummary | null>(null);
  const [demand, setDemand] = useState<DemandProfile | null>(null);
  const [mix, setMix] = useState<CategoryDistribution | null>(null);
  const [corridors, setCorridors] = useState<CorridorOutcomes | null>(null);
  const [responses, setResponses] = useState<ResponseDistribution | null>(null);
  const [load, setLoad] = useState<HospitalLoad | null>(null);
  const [datasets, setDatasets] = useState<ExportDataset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);

  const load_ = useCallback(
    async (signal: AbortSignal) => {
      try {
        // One batch: eight small aggregates in parallel beats a waterfall,
        // and a partial dashboard is worse than a slightly slower whole one.
        const [next, spots, series, tsum, dem, dist, corr, resp, hosp, exp] =
          await Promise.all([
            analytics.summary(days, signal),
            analytics.accidentHotspots(signal),
            charts.trends(days, signal),
            charts.trendSummary(days, signal),
            charts.demand(days, signal),
            charts.distribution(days, signal),
            charts.corridorOutcomes(days, signal),
            charts.responseDistribution(days, signal),
            charts.hospitalLoad(days, signal),
            charts.exports(signal),
          ]);
        setSummary(next);
        setHotspots(spots.hotspots);
        setTrends(series);
        setTrendSummary(tsum);
        setDemand(dem);
        setMix(dist);
        setCorridors(corr);
        setResponses(resp);
        setLoad(hosp);
        setDatasets(exp.datasets);
        setSelected((current) => (current.length ? current : series.default_series));
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
    void load_(controller.signal);
    return () => controller.abort();
  }, [load_]);

  const rt = summary?.response_times;
  const cu = summary?.corridor_usage;
  const mv = summary?.movement;

  // A metric is plotted only against its own unit; seconds and counts on one
  // y-axis makes both unreadable.
  const chosen = (trends?.series ?? []).filter((spec) => selected.includes(spec.key));
  const chartUnit = chosen[0]?.unit ?? "";
  const plotted = chosen.filter((spec) => spec.unit === chartUnit);

  const toggleSeries = (key: string) => {
    setSelected((current) =>
      current.includes(key)
        ? current.filter((item) => item !== key)
        : [...current, key],
    );
  };

  const targetBucket =
    responses?.buckets.find((bucket) => bucket.high_min === responses.target_minutes)?.label ??
    null;

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

      {trendSummary?.comparable && (
        <>
          <h2>Direction of travel</h2>
          <p className="muted small">
            Most recent half of the window against the half before it. Day-on-day
            comparison is too noisy at this volume to mean anything.
          </p>
          <TrendTiles
            metrics={trendSummary.metrics.filter((metric) =>
              trends?.default_series.includes(metric.key),
            )}
          />
        </>
      )}

      {trends && (
        <>
          <h2>Daily trend</h2>
          <Card>
            <div className="series-picker">
              {trends.series.map((spec) => (
                <button
                  type="button"
                  key={spec.key}
                  title={spec.description}
                  className={`series-chip${selected.includes(spec.key) ? " on" : ""}${
                    selected.includes(spec.key) && spec.unit !== chartUnit ? " muted-chip" : ""
                  }`}
                  style={selected.includes(spec.key) ? { borderColor: spec.colour } : undefined}
                  onClick={() => toggleSeries(spec.key)}
                >
                  <span className="chip-dot" style={{ background: spec.colour }} />
                  {spec.label}
                </button>
              ))}
            </div>

            <TrendLines
              data={trends.points}
              series={plotted.map((spec) => ({
                key: spec.key, label: spec.label, colour: spec.colour, unit: spec.unit,
              }))}
              subtitle={
                plotted.length < chosen.length
                  ? `Showing ${chartUnit || "count"} metrics only — mixed units share no axis.`
                  : undefined
              }
            />

            {trends.computed_live_days > 0 && (
              // Surfaced rather than hidden: a dashboard computing 90 days
              // live every request means the rollup schedule is not running.
              <div className="muted small">
                {trends.materialised_days} of {trends.window_days} days read from
                stored rollups; {trends.computed_live_days} computed on request. Run{" "}
                <code>manage.py rollup_metrics</code> nightly to materialise them.
              </div>
            )}
          </Card>
        </>
      )}

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

          <div className="grid-2">
            {responses && (
              <Card>
                <ResponseHistogram
                  buckets={responses.buckets}
                  targetLabel={targetBucket}
                  title="Response time distribution"
                  subtitle={
                    responses.within_target_share !== null
                      ? `${Math.round(responses.within_target_share * 100)}% within ${responses.target_minutes} minutes · ${responses.samples} measured`
                      : "No measured responses in this window"
                  }
                />
              </Card>
            )}
            {demand && (
              <Card>
                <DemandChart
                  data={demand.hours}
                  title="Demand by hour of day"
                  subtitle={
                    demand.peak_hour
                      ? `Peak ${demand.peak_hour} · ${demand.trips} trips · ${demand.timezone}`
                      : `${demand.timezone}`
                  }
                />
              </Card>
            )}
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

          {corridors && (
            <Card>
              <StackedBars
                data={corridors.points}
                series={corridors.legend}
                title="Preemption outcomes per day"
                subtitle="Yielded is the contention rule working correctly; failed is a junction to go and look at."
              />
            </Card>
          )}
        </>
      )}

      {(mix || demand) && (
        <>
          <h2>Emergency mix &amp; weekly shape</h2>
          <div className="grid-2">
            {mix && (
              <Card>
                <DistributionDonut
                  slices={mix.categories}
                  total={mix.total}
                  title="By emergency category"
                  subtitle={`${mix.total} trips in ${mix.window_days} days`}
                />
              </Card>
            )}
            {demand && (
              <Card>
                <HorizontalBars
                  data={demand.weekdays}
                  dataKey="trips"
                  labelKey="label"
                  title="By day of week"
                  subtitle="Rostering signal"
                />
              </Card>
            )}
          </div>
          {mix && mix.levels.some((level) => level.value > 0) && (
            <Card>
              <DistributionDonut
                slices={mix.levels.filter((level) => level.value > 0)}
                total={mix.total}
                height={200}
                title="By priority level (Layer 6)"
              />
            </Card>
          )}
        </>
      )}

      {load && load.hospitals.length > 0 && (
        <>
          <h2>Hospital load &amp; routing agreement</h2>
          <div className="grid-2">
            <Card>
              <HorizontalBars
                data={load.hospitals}
                dataKey="trips"
                labelKey="name"
                colour="#4dd4c0"
                title="Trips received"
                subtitle={`${load.total_routed} routed${load.truncated ? " (top 12 shown)" : ""}`}
              />
            </Card>
            <Card title="Crew overrides by hospital">
              <p className="muted small">
                A high override rate is the recommender disagreeing with crews about
                that site — worth investigating before it is worth ignoring.
              </p>
              <table className="data">
                <thead>
                  <tr>
                    <th>Hospital</th>
                    <th>Trips</th>
                    <th>Overridden</th>
                    <th>Avg transport</th>
                  </tr>
                </thead>
                <tbody>
                  {load.hospitals.map((row) => (
                    <tr key={row.code}>
                      <td>{row.name}</td>
                      <td className="mono">{row.trips}</td>
                      <td>
                        <Badge tone={row.override_share > 0.25 ? "warn" : "ok"}>
                          {Math.round(row.override_share * 100)}%
                        </Badge>
                      </td>
                      <td className="mono">{fmtDuration(row.avg_transport_s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
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

      {datasets.length > 0 && (
        <>
          <h2>Export</h2>
          <Card>
            <p className="muted small">
              Each file contains exactly the data the matching chart is drawn from, for
              the window selected above. Columns are listed so a report pipeline can
              depend on them.
            </p>
            <div className="export-grid">
              {datasets.map((dataset) => (
                <button
                  type="button"
                  key={dataset.key}
                  className="export-card"
                  data-dataset={dataset.key}
                  data-url={charts.exportUrl(dataset.key, days)}
                  onClick={() =>
                    void charts
                      .downloadExport(dataset.key, days)
                      .catch((err: unknown) =>
                        setError(err instanceof Error ? err.message : "Export failed."),
                      )
                  }
                >
                  <span className="export-title">{dataset.title}</span>
                  <span className="export-desc">{dataset.description}</span>
                  <span className="export-cols mono">{dataset.columns.join(", ")}</span>
                </button>
              ))}
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
