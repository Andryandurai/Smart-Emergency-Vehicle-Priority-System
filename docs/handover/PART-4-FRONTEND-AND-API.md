# Part 4 — Frontend Documentation & API Reference (§7–8)

See [README.md](README.md) for the index.

## Contents

- [§7 Frontend Documentation](#7-frontend-documentation)
  - [7.1 Application shell and routing](#71-application-shell-and-routing)
  - [7.2 Pages](#72-pages-11)
  - [7.3 Zustand stores](#73-zustand-stores)
  - [7.4 Hooks](#74-hooks)
  - [7.5 API layer](#75-api-layer)
  - [7.6 Map components](#76-map-components)
  - [7.7 Chart components](#77-chart-components)
  - [7.8 Notification components](#78-notification-components)
  - [7.9 Shared UI primitives](#79-shared-ui-primitives)
  - [7.10 Forms, tables, modals](#710-forms-tables-and-modals)
- [§8 API Reference](#8-api-reference)

---

# §7 Frontend Documentation

**Stack:** React 19 · TypeScript 5.7 (`strict`, `noUncheckedIndexedAccess`) ·
Vite 6 · React Router 7 · Zustand 5 · Leaflet 1.9 + react-leaflet 5 · Recharts 3.
41 source files, ~7,300 lines.

**There is no React Context in this application.** State that must be shared is
in a Zustand store; state that is local stays local. Context was not needed and
adding it would only introduce a provider tree and re-render surface.

---

## 7.1 Application shell and routing

### `main.tsx`
Mounts `<App />` into `#root` with `createRoot`, imports the global stylesheet.
No `StrictMode` double-invocation issues to work around because no component
relies on effects running exactly once.

### `app/App.tsx`

**Purpose.** Route table, bootstrap, and the loading gate.

| Concern | Behaviour |
|---|---|
| Bootstrap | Calls `authStore.bootstrap()` once on mount — a silent refresh against the httpOnly cookie |
| Loading gate | While `status` is `idle`/`checking`, renders a boot splash. **Guards must not redirect during the initial refresh**, or every reload bounces an authenticated operator to `/login` |
| Lazy route | `/analytics` is `React.lazy` + `Suspense` so Recharts (437 kB) never loads for an operator on the map |

Route table:

| Path | Component | Guard |
|---|---|---|
| `/login` | `LoginPage` | none |
| `/driver` | `DriverPage` | **none — Layer 4 promise** |
| `/boards` | `BoardsPage` | **none — roadside signs** |
| `/` | `OperationsPage` | `RequireAuth` |
| `/hospitals` | `HospitalListPage` | `RequireAuth` |
| `/hospital/:code` | `HospitalPage` | `RequireAuth` |
| `/paramedic` | `ParamedicSelectPage` | `RequireAuth` |
| `/paramedic/:callsign` | `ParamedicPage` | `RequireAuth` |
| `/analytics` | `AnalyticsPage` (lazy) | `RequireAuth` |
| `/settings` | `SettingsPage` | `RequireAuth` |
| `*` | `NotFoundPage` | none |

### `app/AppShell.tsx`

**Purpose.** The persistent frame: brand, navigation, notification bell, whoami.

- **Props:** none. Reads `authStore`.
- **State:** none of its own.
- **Behaviour:** `NAV.filter(item => !item.authOnly || user)` — the navigation
  only offers what the role can actually open. An admin additionally gets a
  link to `/admin/`.
- **Renders `<NotificationCentre enabled={Boolean(user)} />`** — in the shell,
  not on a page, because the point of a notification is that it reaches someone
  looking at something else.

### `app/RequireAuth.tsx`

**Purpose.** Route guard. Redirects to `/login` when unauthenticated, carrying
`state.from` so the deep link survives — being dropped on the dashboard after
asking for `/analytics` is a small thing that erodes trust in a tool.

---

## 7.2 Pages (11)

### `OperationsPage.tsx` — the Emergency Operations Dashboard (4.11)

| Aspect | Detail |
|---|---|
| **Purpose** | The single live picture: map, fleet, corridors, disruptions, event log |
| **Props** | none (route component) |
| **State** | `basemapId`, a 1 Hz `tick` for relative ETAs, plus store subscriptions |
| **Stores** | `opsStore` (vehicles, trips, pre-emptions, alerts, log), `authStore`, `notifyStore` |
| **Hooks** | `useGisLayers`, `usePolling` ×4, `useSocket("/ws/ops/")` |
| **API** | vehicles 2 s · trips 3 s · pre-emptions 4 s · events 10 s; GIS layers at per-layer intervals |
| **Socket events handled** | `snapshot`, `vehicle_position`, `trip_created`, `trip_stage`, `hospital_assigned`, `route_updated`, `priority_directive`, `signal_preempted`, `signal_released`, `driver_alerts`, `eta_update`, `traffic_update`, `fleet_health`, `notification`, `road_event_created`, `road_event_cleared` |
| **Lifecycle** | Mount → bootstrap layers + open socket; 1 s interval for ETA countdowns; unmount closes socket and clears timers |

Two details worth carrying forward:

```tsx
const trips = useOpsStore(useShallow(selectTripList));
```
**`useShallow` is required, not stylistic.** Those selectors build a new array
per call; Zustand 5 sits on `useSyncExternalStore`, which compares snapshots
with `Object.is`. Without it the page enters an infinite render loop and renders
blank. This was a real bug found by the Playwright suite.

```tsx
onGap: () => { store.addLog("missed live frames - resyncing"); void store.refreshAll(); }
```
A sequence gap means this client missed frames. Refetching beats rendering a
board that quietly stopped updating.

### `AnalyticsPage.tsx` — AI Traffic Analytics (4.8 / 4.9)

| Aspect | Detail |
|---|---|
| **Purpose** | Trends, distributions, demand profile, corridor outcomes, hospital load, hotspots, exports |
| **State** | `days` (7/30/90), ten result slices, `selected` series, `error` |
| **API** | Ten parallel calls in one `Promise.all` — a waterfall would show a half-built dashboard |
| **Charts** | `TrendLines`, `ResponseHistogram`, `DemandChart`, `StackedBars`, `DistributionDonut` ×2, `HorizontalBars` ×2 |
| **Lifecycle** | Refetches whenever `days` changes; `AbortController` cancels in-flight requests on unmount |

The series picker plots **one unit at a time** — seconds and counts on a shared
y-axis makes both unreadable. Mismatched selections dim their chip and the chart
subtitle says why.

### `HospitalPage.tsx` / `HospitalListPage.tsx`

Hospital Preparedness Dashboard (4.7). Inbound trips with ETA, category and
priority; acknowledge; capacity editing; diversion toggle. Socket
`/ws/hospital/<code>/`. Clinical fields render only when the role permits, and
a `RedactionNote` appears when they do not.

### `ParamedicPage.tsx` / `ParamedicSelectPage.tsx`

The crew screen. Vehicle selection, then: live route, next junction, siren mode,
patient assessment form, hospital recommendation with candidate list and
override, stage advance, handover. Socket `/ws/vehicle/<callsign>/`.

### `DriverPage.tsx` — **public**

Layer 4 for road users. Requests geolocation, polls `/alerts/nearby/`, shows
approaching vehicles with an approach arrow and lane instruction, offers push
opt-in. No account, no operational data.

### `BoardsPage.tsx` — **public**

Roadside VMS estate: which boards are displaying what, and when it expires.

### `SettingsPage.tsx`

Account and capabilities, live backend report (`/info/`),
`<NotificationSettings />`, and the role catalogue.

### `LoginPage.tsx`

Username/password with demo-account quick-fill buttons. Errors render in a
`.badge.bad` block — a login failure that is swallowed is worse than one that is
shown.

### `NotFoundPage.tsx`

---

## 7.3 Zustand stores

### `authStore.ts`

```ts
interface AuthState {
  user: CurrentUser | null;
  status: "idle" | "checking" | "authenticated" | "anonymous";
  error: string | null;
  submitting: boolean;
  bootstrap(): Promise<void>;
  login(username, password): Promise<boolean>;
  logout(): Promise<void>;
  hasRole(...roles: Role[]): boolean;
  can(capability): boolean;
}
```

**Deliberately not persisted.** The access token is in the API client's module
scope and the refresh token is an httpOnly cookie. Persisting user state here
would only create a window in which the UI claims to be signed in while the
server disagrees. `bootstrap()` re-establishes the session from the cookie —
the cookie is the source of truth.

`status` distinguishes "still checking" from "definitely signed out", which is
what stops guards redirecting mid-refresh.

### `opsStore.ts`

Holds `vehicles` and `trips` as **keyed maps** (upsert by callsign / id — the
socket delivers individual updates, and a list would need a scan per frame),
`preemptions`, `events`, `alerts`, `log`, `followedCallsign`, `lastError`.

Actions: `applySnapshot`, `upsertVehicle`, `applyEta`, `pushAlerts`, `addLog`,
`follow`, `refreshVehicles`, `refreshTrips`, `refreshPreemptions`,
`refreshEvents`, `refreshAll`.

Selectors: `selectTripList` (sorted by priority), `selectVehicleList`,
`selectOpenPreemptions`, `selectActiveHolds`.
**The first three build new arrays and must be wrapped in `useShallow`** — this
is documented at the definition site.

### `notifyStore.ts`

Three sources feed one list — the REST inbox (history), the socket
`notification` event (instant while a tab is open), and `postMessage` from the
service worker (tab backgrounded). Reconciled on `uuid`, then **collapsed on
`dedupe_key`** so three reroutes of one trip occupy one row showing the latest
state. `MAX_ITEMS = 80`.

`markRead` is optimistic — the badge clears on click, not after a round trip.
A failed inbox fetch **does not blank the list**: what is on screen is still
true, and clearing it looks like "all clear".

---

## 7.4 Hooks

### `useSocket(path, options)`

| Aspect | Detail |
|---|---|
| **Input** | WebSocket path, `{ handlers, onGap }` |
| **Output** | `{ status, viewer, missedFrames, send }` |
| **Behaviour** | Auto-reconnect with backoff; sequence-gap detection; viewer identity; clean teardown |

Reconnect backoff matters: a control room with a flapping network must not
hammer the server, and a socket that gives up silently is worse than one that
reports `disconnected`.

### `usePolling(fn, intervalMs)`

Interval + `AbortController`, cancelled on unmount. **The correctness floor**:
if the socket dies, or the deployment runs the in-memory channel layer where a
worker's events never reach the web process, the console is seconds stale rather
than broken.

### `useGisLayers()`

Fetches the catalogue and basemaps, then polls **each layer at its own
`refresh_seconds`** — vehicles at 3 s and the accident heatmap at 300 s in one
timer would mean either stale vehicles or 100× unnecessary queries.

Two behaviours worth knowing: a **403 stops that layer's polling** (otherwise an
anonymous visitor generates a 401 every three seconds forever), and **layer
choices persist to `localStorage`**.

### `usePushNotifications(enabled)`

Registers the service worker on mount regardless of subscription state (a
browser that granted permission in a previous session must not need a click),
listens for `postMessage`, polls the inbox every 60 s as a slow fallback, and
exposes `enable()` / `disable()`.

---

## 7.5 API layer

### `api/client.ts`

| Concern | Behaviour |
|---|---|
| Access token | Module scope only. `setAccessToken`, `getAccessToken` |
| Refresh | **Single-flight** — a burst of 401s triggers one refresh, not N |
| Auth-lost | `setAuthLostHandler` lets `authStore` react to an unrecoverable 401 |
| Errors | `ApiError` with `status`, `payload`, `isAuthError`, `fieldErrors` |
| Methods | `get`, `post`, `patch`, `put`, `delete`, `raw` |

### `api/endpoints.ts`

Every URL the console uses, in one file, grouped: `auth`, `service`, `fleet`,
`dispatch`, `hospitals`, `network`, `alerts`, `analytics`, `notify`, `charts`.

**Components never build URLs.** This is what makes "before changing a
serializer, does the console care?" answerable with one grep.

`charts.downloadExport()` deserves note: exports are fetched **with the
Authorization header** and handed to the browser as a blob, honouring the
server's `Content-Disposition`. They were `<a href download>` and every export
returned 401 — a browser navigation carries no bearer token.

### `api/types.ts`

Every response shape. Kept in step with the serializers by hand; a mismatch is
caught by `tsc` at the call site rather than at runtime.

---

## 7.6 Map components

### `MapCanvas.tsx`

| Export | Purpose |
|---|---|
| `MapCanvas` | `MapContainer` with `preferCanvas`, configurable basemap, `FALLBACK_BASEMAP` |
| `vehicleIcon(level, type)` | `DivIcon` with a glyph and priority colour |
| `VehicleMarkers` | Fleet markers with popups and click-to-follow |
| `SegmentsLayer` | Road network coloured by congestion; **memoised on the collection** — re-projecting 4,000 polylines every vehicle tick would dominate the frame budget |
| `RouteLine` | Dashed active route |
| `Dot` | Generic circle marker |
| `AlertCircle` | Imperative `L.circle` for alert radii |
| `FollowVehicle` | Pans when following |
| `FitBounds` | Fits a point set |
| `MapLegend` | Priority and congestion key |

`FALLBACK_BASEMAP` is CARTO dark over OSM data — **keyless**. An emergency
platform must not need a tile contract to draw a map.

### `map/layers.ts`

`toLatLng`, `lineToLatLngs`, `congestionColour`, `priorityColour`, `pointStyle`,
`lineStyle`, `describeFeature`, `DEFAULT_ACTIVE_LAYERS`, and the layer types.

**`toLatLng` is the only place in the codebase that flips coordinate order.**

### `map/GisLayer.tsx`

Renders any layer from its spec: Points as styled `CircleMarker`s, LineStrings
as `Polyline`s, heat surfaces as **graduated CircleMarkers**.

Heatmaps are not `leaflet.heat`: that renders a canvas blob with no hit-testing,
so an operator cannot click a hot cell to ask *why* — and "which junction is
this" is the only question a delay heatmap is asked.

### `map/LayerControl.tsx`

Hand-built rather than `L.control.layers`, because it shows state Leaflet has no
concept of: **feature counts**, which layers are heat surfaces, and which are
**locked behind a sign-in**. An operator asking "why can't I see vehicles"
should get the answer from the control, not from devtools.

---

## 7.7 Chart components

### `charts/primitives.tsx`

| Export | Chart | Used for |
|---|---|---|
| `ChartFrame` | wrapper | Title, subtitle, **explicit empty state** |
| `TrendLines` | multi-line | Daily metric trends |
| `StackedBars` | stacked bar | Pre-emption outcomes per day |
| `DemandChart` | area + second axis | Volume against response time by hour |
| `DistributionDonut` | donut | Category and priority mix |
| `ResponseHistogram` | bar + reference line | Response times against the 8-minute target |
| `HorizontalBars` | horizontal bar | Weekday demand, hospital load |
| `formatValue(value, unit)` | — | `252` → `4m 12s`; `null` → `"not measured"` |

Four rules centralised here:

1. **`connectNulls` is off everywhere.** A day with no measurable response time
   must break the line, not plot at the floor — a dip to zero reads as an
   impossibly fast day.
2. **Tooltips format by unit.**
3. **One theme** across every chart.
4. **Empty is stated, not blank** — an empty axis frame looks like a failed
   render.

`isAnimationActive={false}` throughout: an operational dashboard that re-animates
every poll is distracting and costs frames.

### `charts/TrendTiles.tsx`

KPI tiles with a direction arrow. **The colour comes from the server's
`higher_is_better`**, which has three states — `true`, `false`, and `null` for
metrics where neither direction is an improvement. A city having more
emergencies is not the platform performing worse, and painting it red says it
is.

---

## 7.8 Notification components

### `NotificationCentre.tsx`

Bell with unread badge, click-away panel, push banner, notification list.
`criticalUnread` adds a pulsing border (with `prefers-reduced-motion` respected).
Clicking an item marks it read and navigates to its `link`.

The push banner gives **each state its own wording**, because each has a
different remedy: `subscribed`, `denied` ("blocked in site settings — the page
cannot ask again"), `insecure`, `unconfigured`, `prompt`.

### `NotificationSettings.tsx`

Device list with health, category mutes, quiet hours, push enable/disable, and a
**test send that reports what actually happened** — attempted, delivered,
failed. "Sent" without a delivery count is the sort of reassurance that gets
discovered to be false during an incident.

### `lib/push.ts`

`isSupported`, `isSecureContextForPush`, `urlBase64ToUint8Array`,
`registerServiceWorker`, `currentStatus`, `enablePush`, `disablePush`.

Every function returns a **typed state, not a boolean**. Subscribe **rolls back**
if the server rejects (otherwise the browser holds a subscription the server
does not know about and the UI reads "enabled"); unsubscribe **tells the server
first**, for the mirror reason.

---

## 7.9 Shared UI primitives

`components/ui.tsx`:

| Export | Purpose |
|---|---|
| `Card` | Titled panel |
| `Stat` | Big number + label + sub-label |
| `Badge` | Tone chip (`ok`/`warn`/`bad`/default) |
| `Empty` | Explicit empty state |
| `ErrorNote` | Error banner |
| `RedactionNote` | **"Clinical data hidden for your role"** — the redaction is visible, not silent |
| `ConnectionDot` | Socket status + missed-frame count |
| `levelClass`, `LEVEL_LABEL` | Priority styling |
| `fmtTime`, `fmtEta`, `fmtDistance`, `fmtDuration`, `congestionColour` | Formatters |

`RedactionNote` matters: a screen that silently omits fields looks like a screen
with no data. Saying "hidden for your role" is both honest and a support-call
deflection.

---

## 7.10 Forms, tables and modals

**There are no modal dialogs in this application.** Every action is either
inline or a page. A modal over a live map hides the situation the operator
opened the map to watch.

**Forms** are controlled React state with server-side validation as the source
of truth; `ApiError.fieldErrors` maps DRF's 400 body onto fields. The main forms
are login, patient assessment (`ParamedicPage`), capacity/diversion
(`HospitalPage`), and notification preferences.

**Tables** use one `.data` class and a local `Table` helper in the pages that
need it. Deliberately not a datagrid library: the tables are ≤ 20 rows, read-only,
and a grid would add 100 kB for sorting nobody asked for.

---

# §8 API Reference

**Base:** `/api/v1/` · **Auth:** `Authorization: Bearer <access>` unless noted ·
**Errors:** DRF-standard.

**155 application endpoints** across ten route groups. Full machine-readable
permissions: `GET /api/v1/auth/policy/` (administrators).

## 8.0 Common error responses

| Status | Body | Meaning |
|---|---|---|
| 400 | `{"field": ["message"]}` | Serializer validation failed |
| 401 | `{"detail": "Authentication credentials were not provided."}` | No/invalid token |
| 403 | `{"detail": "<permission message>"}` | Authenticated, wrong role |
| 404 | `{"detail": "Not found."}` | Missing, or an unknown GIS layer / export dataset (which also returns `available`) |
| 405 | `{"detail": "Method \"GET\" not allowed."}` | e.g. GET on `/notify/subscribe/` |
| 409 | `{"detail": "..."}` | Conflict, e.g. test push with no subscriptions |
| 429 | — | Nginx rate limit (auth: 12/min, api: 60/s) |
| 503 | `{"status": "not-ready", "checks": {...}}` | Readiness probe with a dependency down |

## 8.1 Health & service

| Method | URL | Auth | Response |
|---|---|---|---|
| GET | `/health/live/` | public | `{"status":"alive"}` — checks nothing |
| GET | `/health/ready/` | public | `{"status":"ready","checks":{"database":"ok","channel_layer":"..."}}`; 503 if not |
| GET | `/health/` | public | Combined legacy probe |
| GET | `/info/` | public | Active backends (database, spatial, channel layer, routing, congestion, CV, traffic provider) |

```bash
curl http://127.0.0.1:8000/api/v1/health/ready/
# {"status":"ready","checks":{"database":"ok","channel_layer":"InMemoryChannelLayer"}}
```

## 8.2 Authentication

| Method | URL | Auth | Body | Response |
|---|---|---|---|---|
| POST | `/auth/jwt/create/` | public | `{username, password}` | `{access}` + `Set-Cookie: sevps_refresh` |
| POST | `/auth/jwt/refresh/` | cookie | `{}` | `{access}` |
| POST | `/auth/jwt/verify/` | public | `{token}` | `{}` or 401 |
| POST | `/auth/jwt/logout/` | public | `{}` | Blacklists refresh, clears cookie |
| GET | `/auth/me/` | any signed-in | — | Identity + roles + capabilities |
| GET | `/auth/roles/` | public | — | Role catalogue |
| GET | `/auth/policy/` | **admin** | — | Live REST + WebSocket policy audit |
| POST | `/auth/token/` | public | `{username, password}` | Legacy DRF token *(deprecated)* |

**Example**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/jwt/create/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"police","password":"sevps-police"}'
```
```json
{ "access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." }
```

**Errors:** 401 on bad credentials; 400 on a refresh with no cookie and no body.

## 8.3 Fleet — Layer 1

| Method | URL | Permission | Purpose |
|---|---|---|---|
| GET/POST | `/fleet/vehicles/` | role | List / create |
| GET/PUT/PATCH/DELETE | `/fleet/vehicles/{id}/` | role | Detail |
| GET | `/fleet/vehicles/live/` | role | Every online vehicle |
| GET | `/fleet/vehicles/nearest/?lat=&lon=&type=` | role | Nearest available |
| **POST** | `/fleet/vehicles/{id}/telemetry/` | role (crew scoping) | **Ingest a GPS fix** |
| GET | `/fleet/vehicles/{id}/track/` | role | Telemetry history |
| GET | `/fleet/vehicles/{id}/projection/?seconds=` | role | Dead-reckoned position |
| POST | `/fleet/vehicles/{id}/status/` | role | Set status |
| GET/POST | `/fleet/stations/` | role | Stations |

**Telemetry request**
```json
{ "latitude": 13.0604, "longitude": 80.2496, "speed_kmh": 42.5, "heading_deg": 118.0, "accuracy_m": 8 }
```
**Validation:** latitude −90…90, longitude −180…180, speed ≥ 0, heading 0…360.
**Response:** updated vehicle + any orchestration result (route/corridor/ETA
changes). **Errors:** 400 out-of-range; 403 crew acting for another vehicle.

## 8.4 Dispatch — Layers 3 & 6

| Method | URL | Permission | Purpose |
|---|---|---|---|
| GET/POST | `/dispatch/trips/` | role | List / **open a response** |
| GET | `/dispatch/trips/live/` | role | Active trips |
| GET | `/dispatch/trips/{id}/` | role | Detail (clinical fields redacted by role) |
| POST | `/dispatch/trips/{id}/assess/` | role | **Patient assessment → hospital recommendation** |
| POST | `/dispatch/trips/{id}/stage/` | role | Advance stage |
| POST | `/dispatch/trips/{id}/handover/` | role | Complete |
| POST | `/dispatch/trips/{id}/cancel/` | **dispatcher** | Stand down |
| POST | `/dispatch/trips/{id}/condition/` | role | Deterioration update → may escalate priority |
| GET | `/dispatch/trips/{id}/corridor/` | role | Corridor status |
| POST | `/dispatch/trips/{id}/corridor/sync/` | role | Force reconciliation |
| POST | `/dispatch/trips/{id}/corridor/release/` | **traffic police** | Manual release |
| POST | `/dispatch/trips/{id}/reroute/` | role | Force reroute |
| POST | `/dispatch/corridor/tick/` | **traffic police** | Run the safety sweep |
| GET | `/dispatch/priority-profiles/` | **public** | The four levels, documented |
| GET | `/dispatch/preemptions/?open=1` | role | Pre-emptions |
| GET | `/dispatch/routes/`, `/dispatch/directives/` | role | Plans and directives |

**Create a trip**
```json
{ "incident_latitude": 13.0604, "incident_longitude": 80.2496,
  "emergency_category": "cardiac", "incident_address": "Anna Salai",
  "caller_number": "+91...", "auto_assign": true }
```
**Response:** the trip, with `reference`, assigned `vehicle_callsign`, derived
`priority_level`, `siren_mode` and the first `active_route`.
**Errors:** 400 invalid category / no coordinates; 409 no vehicle available.

**Assess**
```json
{ "emergency_category": "cardiac", "patient_age": 61,
  "patient_notes": "ST elevation", "patient_deteriorating": false }
```
**Response:** `{ trip, recommendation: {hospital, score, candidates, warnings, rule}, corridor: [...] }`

## 8.5 Hospitals — Layer 5

| Method | URL | Permission | Purpose |
|---|---|---|---|
| GET | `/hospitals/hospitals/` | **public read** | Directory |
| PATCH | `/hospitals/hospitals/{id}/capacity/` | **hospital staff** | Update beds |
| POST | `/hospitals/hospitals/{id}/diversion/` | **hospital staff** | Declare/clear diversion |
| GET | `/hospitals/hospitals/{id}/inbound/` | role | Inbound trips |
| POST | `/hospitals/recommend/` | **public** | Category + location → ranked hospitals |
| GET | `/hospitals/rule-lookup/{category}/` | **public** | One clinical rule |
| GET | `/hospitals/rules/catalogue/` | **admin** | All rules |
| POST | `/hospitals/rules/seed/` | **admin** | Seed defaults |
| POST | `/hospitals/alerts/{id}/acknowledge/` | role | Acknowledge inbound |
| GET | `/hospitals/recommendation-logs/` | role | Decision audit |

**`/hospitals/recommend/` is public** because it takes a location and a category
and returns public hospital capability. No patient record is involved.

```json
{ "latitude": 13.06, "longitude": 80.25, "emergency_category": "cardiac" }
```
```json
{ "hospital": { "code": "APL", "name": "Apollo Main", "score": 0.87 },
  "candidates": [ { "code": "APL", "score": 0.87, "travel_time_s": 412,
                    "capability_match": 1.0, "beds_available": 16,
                    "excluded": null } ],
  "warnings": [], "rule": { "required_facilities": ["emergency_dept","cath_lab"],
                            "golden_window_min": 90, "time_critical": true } }
```
**Errors:** 400 unknown category; 200 with `hospital: null` and a warning when
nothing qualifies — this is a real operational outcome, not an error.

## 8.6 Network & GIS

| Method | URL | Permission | Purpose |
|---|---|---|---|
| GET | `/network/segments/geojson/?limit=` | public read | Road network GeoJSON |
| POST | `/network/segments/speeds/` | traffic police | Bulk speed ingest |
| GET | `/network/signals/status/` | public read | Signal estate summary |
| POST | `/network/signals/{id}/heartbeat/` | traffic police | Controller liveness |
| GET/POST | `/network/events/` | public read / traffic write | Road disruptions |
| POST | `/network/events/{id}/clear/` | traffic police | Clear |
| POST | `/network/cameras/{id}/analyse/` | traffic police | CV on one camera |
| GET | `/network/cv/status/` | **public** | CV backend + estate counters |
| POST | `/network/cv/sweep/` | traffic police | Sweep the estate |
| GET | `/network/cv/emergency/` | role | Recent emergency-vehicle sightings |
| GET | `/network/gis/layers/` | **public** | Layer catalogue |
| GET | `/network/gis/layers/{name}/` | **per-layer** | One FeatureCollection |
| GET | `/network/gis/basemaps/` | **public** | Tile providers |

**Layer response envelope**
```json
{ "type": "FeatureCollection",
  "features": [ { "type": "Feature",
                  "geometry": { "type": "Point", "coordinates": [80.2496, 13.0604] },
                  "properties": { "code": "APL", "is_on_diversion": false } } ],
  "metadata": { "layer": "hospitals", "count": 8,
                "generated_at": "2026-08-03T09:14:22+05:30", "truncated": false } }
```
**Note the coordinate order** — `[lon, lat]`, per RFC 7946.
**Errors:** 404 with `available: [...]` for an unknown layer; 400 for a
non-integer `limit`; 401/403 for a role-gated layer.

## 8.7 Alerts — Layer 4

| Method | URL | Permission | Purpose |
|---|---|---|---|
| GET | `/alerts/nearby/?lat=&lon=&radius_m=` | **public** | Approaching-vehicle warnings |
| POST | `/alerts/position/` | **public** | Device position → geohash cell |
| GET | `/alerts/boards/live/` | **public** | VMS board messages |
| POST | `/alerts/boards/clear-expired/` | traffic police | Expire messages |
| GET | `/alerts/driver-alerts/` | role | Full alert table (**not** public) |
| GET | `/alerts/devices/` | role | Registered devices |

## 8.8 Analytics

| Method | URL | Permission | Purpose |
|---|---|---|---|
| GET | `/analytics/summary/?days=` | role | Whole-dashboard aggregate |
| GET | `/analytics/trends/?days=` | role | Daily series + catalogue |
| GET | `/analytics/trends/summary/?days=` | role | Direction of travel |
| GET | `/analytics/demand/?days=` | role | Hour-of-day + weekday profile |
| GET | `/analytics/distribution/?days=` | role | Category + priority mix |
| GET | `/analytics/corridor-outcomes/?days=` | role | Pre-emption outcomes per day |
| GET | `/analytics/response-distribution/?days=` | role | Histogram vs the 8-minute target |
| GET | `/analytics/hospital-load/?days=` | role | Per-hospital load + override rate |
| GET | `/analytics/accident-hotspots/` | role | Clustered blackspots |
| POST | `/analytics/accident-hotspots/recompute/` | traffic police | Recluster |
| POST | `/analytics/rollup/` | traffic police | Materialise a day |
| GET | `/analytics/export/` | role | Export catalogue with exact columns |
| GET | `/analytics/export/{dataset}.csv?days=` | role | Streamed CSV |

`?days=` is clamped 1…365; junk falls back to 30 rather than erroring.

## 8.9 Notifications

| Method | URL | Permission | Purpose |
|---|---|---|---|
| GET | `/notify/vapid-key/` | **public** | Public key — required before the permission prompt |
| POST | `/notify/subscribe/` | **public (POST only)** | Register a browser/device |
| POST | `/notify/unsubscribe/` | **public (POST only)** | Retire a subscription |
| GET | `/notify/subscriptions/` | role | The caller's **own** devices |
| GET/PATCH | `/notify/preferences/` | role | Mutes and quiet hours |
| GET | `/notify/inbox/` | role | Last 24 h, filtered by role |
| POST | `/notify/read/` · `/notify/read/{uuid}/` | role | Mark read |
| POST | `/notify/test/` | role | Test push to **own** devices only |
| GET | `/notify/health/` | role | Whether push can actually deliver |

**Subscribe**
```json
{ "endpoint": "https://fcm.googleapis.com/fcm/send/...",
  "keys": { "p256dh": "BN...", "auth": "k9..." },
  "device_id": "phone-1", "latitude": 13.06, "longitude": 80.25 }
```
201 on create, **200 on re-subscribe** (idempotent on `endpoint`).
**Errors:** 400 without `keys` for Web Push, or with only one of lat/lon;
405 on GET.

## 8.10 Brain / AI

| Method | URL | Permission | Purpose |
|---|---|---|---|
| POST | `/brain/route/` | **public** | Compute a route |
| POST | `/brain/route/compare/` | **public** | A* vs Dijkstra |
| GET | `/brain/congestion/forecast/` | **public** | Aggregate forecast |
| GET | `/brain/network/summary/` | **public** | Graph health |
| POST | `/brain/network/rebuild/` | traffic police | Invalidate caches |
| GET | `/brain/corridor/{trip_id}/preview/` | role | Planned corridor before commitment |
| GET | `/brain/priority/ranking/` | role | Ranked active trips |
| POST | `/brain/reroute/{trip_id}/evaluate/` | traffic police | Assess one trip |
| POST | `/brain/reroute/reassess/` | traffic police | Assess all |
| GET | `/brain/ml/models/` | **public** | Which models are trained |
| GET | `/brain/ml/congestion/` | **public** | Congestion prediction + SHAP |
| GET | `/brain/ml/eta/{trip_id}/` | role | ETA prediction |
| POST | `/brain/ml/priority/` | role | Advisory priority + rule comparison |
| GET | `/brain/ml/corridor/{signal_id}/` | role | Pre-emption success likelihood |
| POST | `/brain/ml/hospital/` | **public** | Explained recommendation |

**Every prediction response carries the same envelope:**
```json
{ "value": 0.62, "confidence": 0.81, "actionable": true, "source": "model",
  "explanation": { "method": "shap",
    "contributions": [ { "feature": "hour_of_day", "value": 18, "contribution": 0.14 } ] } }
```
`source` is `model` or `baseline`, so a consumer always knows whether a trained
model produced the number.

---

**Previous:** [Part 3 — Backend](PART-3-BACKEND.md)
**Next:** [Part 5 — Auth, AI, CV, GIS](PART-5-AUTH-AI-CV-GIS.md)
