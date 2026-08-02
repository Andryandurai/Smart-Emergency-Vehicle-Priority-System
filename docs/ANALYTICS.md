# Analytics & Charts (Phase 10)

## The gap this phase closed

`apps/analytics/services.py` already answered *"what were the numbers over the
last 30 days"* — one aggregate per metric, rendered as stat tiles and tables.
What it could not answer is the question a control room actually asks: **is
this getting better or worse?** An average response time of 6m 40s means
nothing without knowing whether it was 5m 10s last month.

Phase 10 adds the series, the profiles, the distributions and the exports —
and the charts to read them. The existing summary endpoints, tiles and tables
are unchanged and still on the page; a chart shows a *shape*, a table gives you
the *number to put in a report*, and a control room needs both.

---

## Recharts, not Chart.js

The brief listed both. **Recharts** was chosen:

- **It is React, not a canvas wrapped in React.** Chart.js requires
  `react-chartjs-2` plus an imperative create/update/destroy lifecycle kept in
  step with React's own re-renders. That whole class of bug does not exist
  here — charts are components with props.
- **SVG output.** Inspectable, selectable, printable at any resolution. This
  screen's output ends up in city reports, and a rasterised canvas at 96 dpi
  does not.
- **The trade is real but does not apply.** SVG gets slow above a few thousand
  DOM nodes. SEVPS series are capped at 365 daily points and 24 hourly buckets.

**Recommendation: adopt Recharts, do not add Chart.js.** Two charting libraries
in one bundle is 900 kB to render the same rectangles two ways.

### Bundle cost was addressed, not ignored

Recharts is ~437 kB. Adding it naively pushed the main chunk from 256 kB to
**709 kB**, meaning every operator opening the live map during an incident
downloaded charting code they were not going to use.

Two changes fixed it:

```ts
manualChunks: { charts: ["recharts"] }          // vite.config.ts
const AnalyticsPage = lazy(() => import("@/pages/AnalyticsPage"))   // App.tsx
```

| Chunk | Size | Loaded |
|---|---|---|
| `index` | 256 kB | always |
| `leaflet` | 155 kB | always (every screen has a map) |
| `charts` | 437 kB | **only when `/analytics` is opened** |
| `AnalyticsPage` | 17 kB | only when `/analytics` is opened |

---

## Three properties that keep a chart honest

These are the ones worth stating, because getting them wrong does not raise an
error — it draws something confidently wrong.

### 1. Series are contiguous

Every day in the window is emitted, including days with no activity. A series
that omits quiet days compresses the x-axis: a fortnight with two busy days
renders identically to a fortnight that was busy throughout.

### 2. Zero and "not measured" are different values

`trips: 0` is a real measurement — nothing happened. `avg_response_time_s:
null` means nothing was measurable. Plotting the second as zero draws a cliff
to the floor that reads as a dramatic improvement in ambulance response times.

So each series declares its `kind`: `count` metrics default to `0`, `measure`
metrics default to `null`, and **`connectNulls` is off on every chart** so a
null breaks the line rather than being bridged over.

### 3. Buckets carry their own timezone

The demand profile is keyed on **local time**. "The evening peak is at 18:00"
is a claim about Chennai; a UTC-keyed profile shifts every peak by five and a
half hours and nobody notices because the numbers still look plausible. The
response includes `timezone` so the claim is checkable.

---

## Trend direction has three states, not two

The KPI tiles show an arrow whose colour comes from `higher_is_better`,
decided **on the server**. It must be server-side: "cross-traffic held: up 12%"
is bad news and "trips completed: up 12%" is good news, and a client inferring
it from the sign of the change gets one of them backwards.

The third state matters as much as the first two:

| `higher_is_better` | Meaning | Metrics |
|---|---|---|
| `true` | Up is an improvement | trips completed |
| `false` | Up is a regression | response times, cross-traffic held, ETA error, reroutes, crew overrides |
| `null` | **Neither** | emergency trips, corridors created, signals held, driver alerts |

A city having more emergencies this fortnight is not SEVPS performing worse.
The first implementation painted "Emergency trips ▲ 100%" red, which says the
platform is failing when what happened is that the city had a busy week. A
dashboard that cries wolf about things nobody controls gets ignored about the
things they do.

`improving: null` also covers "nothing to compare against" — distinct from "no
change", because an unmeasured metric is not a stable one.

Comparison is **the recent half of the window against the half before it**, not
today vs yesterday. Emergency volume is noisy enough day to day that a
day-on-day arrow points down roughly half the time regardless of reality.

---

## Where the numbers come from

Historical days are read from `DailyMetric` where a rollup exists, and computed
live otherwise:

- **Rollups are the materialised record** and are cheap to read across 90 days.
- **Today has no rollup yet**, and a dashboard whose most recent point is
  always missing is one nobody trusts.

The response reports `materialised_days` and `computed_live_days`, and the UI
says so when live computation dominates — a dashboard silently recomputing 90
days on every request means the rollup schedule is not running, which is an
operational fact, not an implementation detail.

Reading the series **never writes a rollup**
(`test_reading_the_series_does_not_write_a_rollup`). A partial day materialised
by a page view would later be found already present and skipped by the real
nightly recompute.

---

## Endpoints

All authenticated. Daily emergency volume and per-hospital load is operational
intelligence even though no individual trip is identifiable, so none of these
is in the public allowlist.

| Endpoint | Returns |
|---|---|
| `GET /analytics/trends/` | Daily points + the series catalogue (label, unit, colour, kind) |
| `GET /analytics/trends/summary/` | Each metric's recent half vs previous half |
| `GET /analytics/demand/` | Hour-of-day and day-of-week profile, local time |
| `GET /analytics/distribution/` | Trips by emergency category and priority level |
| `GET /analytics/corridor-outcomes/` | Preemptions per day, split by outcome |
| `GET /analytics/response-distribution/` | Histogram against the 8-minute target |
| `GET /analytics/hospital-load/` | Trips per hospital with crew override rate |
| `GET /analytics/export/` | Export catalogue, with exact column lists |
| `GET /analytics/export/<dataset>.csv` | Streamed CSV |

All accept `?days=` (clamped 1–365; junk falls back to 30 rather than erroring).

### The series catalogue lives on the server

Label, unit, colour and direction come from `SERIES` in `trends.py`, not from
the React component — the same reasoning as the Phase 8 GIS layer registry.
Adding a metric is one change, and two screens plotting "response time" cannot
disagree about what it is measured in.

The frontend picker plots **one unit at a time**. Seconds and counts on a
shared y-axis makes both unreadable, so selecting a mismatched metric dims its
chip and the chart says why.

---

## Charts on the page

| Chart | Type | Why this shape |
|---|---|---|
| Daily trend | Multi-line | Direction over time; nulls break the line |
| Response distribution | Histogram + target line | An average hides the shape — two services with an 8-minute mean, one tight and one bimodal, are not the same service |
| Demand by hour | Area + second axis | Volume against response time in the same hour: a busy hour that stays fast is capacity working; one that slows is capacity running out |
| Preemption outcomes | Stacked bars | Activated / yielded / failed mean different things: yielded is the contention rule working, failed is a junction to go and look at |
| Emergency mix | Donut | Total in the hole — the number people actually read off a breakdown |
| Day of week | Horizontal bars | Rostering signal |
| Hospital load | Horizontal bars + table | A high override rate is the recommender disagreeing with crews about a site |

Bucket edges for the response histogram are `0, 4, 6, 8, 10, 15, 20, 30`
minutes — dense where the clinical decision is (most cardiac-arrest survival
guidance is written around 8 minutes) and coarse in the tail, rather than
evenly spaced round numbers.

Two corridor-outcome details that were wrong in the first pass and are now
pinned by tests:

- **Yielding is recorded by the `yielded_to` relation, not a state.** Reading
  `state` alone files a yielded preemption as "cancelled", which renders as a
  fault when it is the priority rule working correctly.
- **Activation is detected by `activated_at`, not current state.** A released
  preemption still activated; checking state alone reports it as pending.

---

## Exports

Every chart has a matching CSV of exactly the data it is drawn from — not a
similar query that happens to be nearby, which is how a report and a dashboard
end up disagreeing. Cities run on spreadsheets; a dashboard that cannot produce
an attachable file gets replaced by someone re-typing numbers out of it, and
re-typed numbers are wrong numbers.

```
daily · demand-hourly · demand-weekday · categories
corridors · response-distribution · hospitals · hotspots
```

Each file opens with its own provenance:

```
# SEVPS Daily metrics
# window: last 30 days
# generated: 2026-08-02T23:05:53+05:30
date,trips_total,trips_completed,avg_response_time_s,...
```

One cell per comment line — a two-element row renders as
`# window...,# generated...` and reads as two columns of data.

Responses are streamed, and an unknown dataset returns **404 with the list of
valid ones**, not an empty file. A report pipeline handed a zero-row CSV
reports zero incidents, which is worse than reporting an error.

---

## What changed in existing files

| File | Change | Risk |
|---|---|---|
| `apps/analytics/views.py` | 9 new views appended | none — additive |
| `apps/analytics/urls.py` | 9 new routes | none — additive |
| `frontend/src/api/types.ts`,`endpoints.ts` | chart types + `charts` client | none — additive |
| `frontend/src/pages/AnalyticsPage.tsx` | charts added around the existing tiles and tables | none removed; every previous section still renders |
| `frontend/src/app/App.tsx` | analytics route lazy-loaded | improves first load on every other route |
| `frontend/vite.config.ts` | `charts` manual chunk | build only |
| `apps/notify/tests.py` | flaky assertion fixed (see below) | test-only |

`apps/analytics/services.py` was **not modified**. `trends.py` and `exports.py`
are new modules beside it, and `_live_day()` reimplements the same arithmetic
as `rollup_daily_metrics()` read-only rather than calling it, precisely so a
dashboard request cannot write.

### A flake found and fixed

`test_driver_alert_push_stays_anonymous` (Phase 9) asserted the patient age
`"44"` was absent from the whole serialised push payload — which contains a
random UUID. About one run in twelve, a UUID contains `44` and the test failed.
It now checks the human-readable fields for leaked values and asserts the
clinical fields are absent as *keys*, which is what the test was actually for.

---

## Verification

```
python manage.py test apps.analytics       # 50 tests
python manage.py test apps                 # 388 tests, OK (3 skipped)
cd frontend && npx vitest run              # 33 tests
cd frontend && npx tsc --noEmit && npx vite build
```

Live probes (server on :8013, admin JWT): all nine endpoints 401 anonymously
and 200 for a signed-in role; unknown dataset 404 with `available`; `?days=`
honoured, clamped at 365, and junk falling back to 30; CSV streaming with real
seeded data; and the trend verdicts confirmed as **neutral** for emergency
volume, **worse** for cross-traffic held, **improving** for completions.
