/**
 * Recharts wrappers carrying the decisions every SEVPS chart must share.
 *
 * Recharts rather than Chart.js: the console is React 19, and Recharts is
 * declarative React components rendering SVG. Chart.js needs the
 * react-chartjs-2 wrapper plus an imperative canvas lifecycle (create, update,
 * destroy) that has to be kept in step with React's own — a class of bug that
 * simply does not exist here. SVG also means a chart is inspectable and
 * printable, which matters for a screen whose output ends up in city reports.
 * The trade is render cost above a few thousand points; these series are at
 * most 365 daily points, well inside where SVG is comfortable.
 *
 * Four things are centralised because getting them inconsistent is what makes
 * a dashboard untrustworthy:
 *
 *  1. **Null is a gap, not zero.** `connectNulls` is off everywhere. A day
 *     with no measurable response time must break the line, not plot at the
 *     floor — a dip to zero reads as an impossibly fast day.
 *  2. **Tooltips format by unit.** Seconds render as `4m 12s`, not `252`.
 *  3. **One theme.** Grid, axis and tooltip colours match the dark console.
 *  4. **Empty is stated, not blank.** A chart with no data says so; an empty
 *     axis frame looks like a failed render.
 */
import type { ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { Slice } from "@/api/types";

const AXIS = "#7f8c9b";
const GRID = "#26313d";

const TOOLTIP_STYLE = {
  background: "#131a21",
  border: "1px solid #26313d",
  borderRadius: 6,
  fontSize: 12,
} as const;

/** Seconds are the unit most SEVPS metrics use and the one raw numbers read
 *  worst in: "252" means nothing, "4m 12s" is an ambulance response time. */
export function formatValue(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined) return "not measured";
  if (unit !== "s") return `${round(value)}${unit ? ` ${unit}` : ""}`;
  if (value < 60) return `${round(value)}s`;
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value % 60);
  if (minutes < 60) return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function round(value: number): number {
  return Math.abs(value) >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
}

/**
 * Recharts types a tooltip value as `ValueType | undefined` - it may be a
 * number, a string, an array, or absent. Narrowing once here keeps every
 * formatter below honest about that instead of each one casting and hoping.
 */
function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

type TooltipFormatter = (value: unknown, name: unknown) => [string, string];

export function ChartFrame({
  title,
  subtitle,
  height = 240,
  empty,
  children,
}: {
  title?: string;
  subtitle?: string;
  height?: number;
  empty?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="chart">
      {title && (
        <div className="chart-head">
          <span className="chart-title">{title}</span>
          {subtitle && <span className="chart-sub">{subtitle}</span>}
        </div>
      )}
      {empty ? (
        <div className="chart-empty" style={{ height }}>
          No data in this window.
        </div>
      ) : (
        <div style={{ width: "100%", height }}>
          <ResponsiveContainer width="100%" height="100%">
            {children as never}
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

interface SeriesLine {
  key: string;
  label: string;
  colour: string;
  unit: string;
}

/** Daily metric lines. `connectNulls` stays off — see the module note. */
export function TrendLines({
  data,
  series,
  height = 260,
  title,
  subtitle,
}: {
  data: Record<string, unknown>[];
  series: SeriesLine[];
  height?: number;
  title?: string;
  subtitle?: string;
}) {
  const unit = series[0]?.unit ?? "";
  return (
    <ChartFrame title={title} subtitle={subtitle} height={height} empty={data.length === 0}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" stroke={AXIS} tick={{ fontSize: 11 }} minTickGap={24} />
        <YAxis
          stroke={AXIS}
          tick={{ fontSize: 11 }}
          width={52}
          tickFormatter={(value: number) => formatValue(value, unit)}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelStyle={{ color: "#e6edf3" }}
          formatter={((value, name) => {
            const spec = series.find((item) => item.label === name);
            return [formatValue(asNumber(value), spec?.unit ?? unit), String(name)];
          }) as TooltipFormatter}
        />
        {series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {series.map((item) => (
          <Line
            key={item.key}
            type="monotone"
            dataKey={item.key}
            name={item.label}
            stroke={item.colour}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4 }}
            connectNulls={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ChartFrame>
  );
}

export function StackedBars({
  data,
  series,
  height = 240,
  title,
  subtitle,
}: {
  data: Record<string, unknown>[];
  series: { key: string; label: string; colour: string }[];
  height?: number;
  title?: string;
  subtitle?: string;
}) {
  const empty = data.every((row) => series.every((item) => !row[item.key]));
  return (
    <ChartFrame title={title} subtitle={subtitle} height={height} empty={empty}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" stroke={AXIS} tick={{ fontSize: 11 }} minTickGap={20} />
        <YAxis stroke={AXIS} tick={{ fontSize: 11 }} width={40} allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "#e6edf3" }} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        {series.map((item) => (
          <Bar
            key={item.key}
            dataKey={item.key}
            name={item.label}
            stackId="outcome"
            fill={item.colour}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
    </ChartFrame>
  );
}

/** Demand profile: volume as bars, response time as a line on a second axis.
 *  The correlation is the whole point — a busy hour that stays fast is
 *  capacity working; a busy hour that slows is capacity running out. */
export function DemandChart({
  data,
  height = 260,
  title,
  subtitle,
}: {
  data: { label: string; trips: number; avg_response_s: number | null }[];
  height?: number;
  title?: string;
  subtitle?: string;
}) {
  return (
    <ChartFrame
      title={title}
      subtitle={subtitle}
      height={height}
      empty={data.every((row) => !row.trips)}
    >
      <AreaChart data={data} margin={{ top: 8, right: 8, left: 4, bottom: 4 }}>
        <defs>
          <linearGradient id="demandFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#4da3ff" stopOpacity={0.5} />
            <stop offset="100%" stopColor="#4da3ff" stopOpacity={0.05} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" stroke={AXIS} tick={{ fontSize: 11 }} interval={1} />
        <YAxis yAxisId="left" stroke={AXIS} tick={{ fontSize: 11 }} width={36} allowDecimals={false} />
        <YAxis
          yAxisId="right"
          orientation="right"
          stroke="#ff9f43"
          tick={{ fontSize: 11 }}
          width={48}
          tickFormatter={(value: number) => formatValue(value, "s")}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelStyle={{ color: "#e6edf3" }}
          formatter={((value, name) =>
            name === "Avg response"
              ? [formatValue(asNumber(value), "s"), String(name)]
              : [String(value ?? "-"), String(name)]) as TooltipFormatter}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Area
          yAxisId="left"
          type="monotone"
          dataKey="trips"
          name="Trips"
          stroke="#4da3ff"
          fill="url(#demandFill)"
          strokeWidth={2}
          isAnimationActive={false}
        />
        <Line
          yAxisId="right"
          type="monotone"
          dataKey="avg_response_s"
          name="Avg response"
          stroke="#ff9f43"
          strokeWidth={2}
          dot={false}
          connectNulls={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartFrame>
  );
}

/** Donut rather than pie: the hole carries the total, which is the number
 *  people actually read off a category breakdown. */
export function DistributionDonut({
  slices,
  total,
  height = 240,
  title,
  subtitle,
}: {
  slices: Slice[];
  total: number;
  height?: number;
  title?: string;
  subtitle?: string;
}) {
  return (
    <ChartFrame title={title} subtitle={subtitle} height={height} empty={slices.length === 0}>
      <PieChart>
        <Pie
          data={slices}
          dataKey="value"
          nameKey="label"
          innerRadius="55%"
          outerRadius="80%"
          paddingAngle={2}
          isAnimationActive={false}
        >
          {slices.map((slice) => (
            <Cell key={slice.key} fill={slice.colour} stroke="#131a21" />
          ))}
        </Pie>
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={((value, name) => {
            const count = asNumber(value) ?? 0;
            const share = total ? Math.round((count / total) * 100) : 0;
            return [`${count} trips (${share}%)`, String(name)];
          }) as TooltipFormatter}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
      </PieChart>
    </ChartFrame>
  );
}

/** Response-time histogram with the clinical target drawn on it, so the
 *  threshold is visible rather than something the reader has to know. */
export function ResponseHistogram({
  buckets,
  targetLabel,
  height = 240,
  title,
  subtitle,
}: {
  buckets: { label: string; count: number; within_target: boolean }[];
  targetLabel: string | null;
  height?: number;
  title?: string;
  subtitle?: string;
}) {
  return (
    <ChartFrame
      title={title}
      subtitle={subtitle}
      height={height}
      empty={buckets.every((bucket) => !bucket.count)}
    >
      <BarChart data={buckets} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" stroke={AXIS} tick={{ fontSize: 10 }} interval={0} angle={-20} textAnchor="end" height={48} />
        <YAxis stroke={AXIS} tick={{ fontSize: 11 }} width={40} allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "#e6edf3" }} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
        {targetLabel && (
          <ReferenceLine
            x={targetLabel}
            stroke="#2ecc71"
            strokeDasharray="4 4"
            label={{ value: "target", fill: "#2ecc71", fontSize: 10, position: "top" }}
          />
        )}
        <Bar dataKey="count" name="Trips" isAnimationActive={false}>
          {buckets.map((bucket) => (
            <Cell key={bucket.label} fill={bucket.within_target ? "#2ecc71" : "#ff9f43"} />
          ))}
        </Bar>
      </BarChart>
    </ChartFrame>
  );
}

export function HorizontalBars({
  data,
  dataKey,
  labelKey,
  colour = "#4da3ff",
  height = 240,
  title,
  subtitle,
}: {
  data: Record<string, unknown>[];
  dataKey: string;
  labelKey: string;
  colour?: string;
  height?: number;
  title?: string;
  subtitle?: string;
}) {
  return (
    <ChartFrame title={title} subtitle={subtitle} height={height} empty={data.length === 0}>
      <BarChart data={data} layout="vertical" margin={{ top: 8, right: 16, left: 8, bottom: 4 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" stroke={AXIS} tick={{ fontSize: 11 }} allowDecimals={false} />
        <YAxis
          type="category"
          dataKey={labelKey}
          stroke={AXIS}
          tick={{ fontSize: 11 }}
          width={130}
        />
        <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "#e6edf3" }} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
        <Bar dataKey={dataKey} fill={colour} isAnimationActive={false} radius={[0, 3, 3, 0]} />
      </BarChart>
    </ChartFrame>
  );
}
