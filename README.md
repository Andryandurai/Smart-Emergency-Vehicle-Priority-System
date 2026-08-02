# SEVPS — Smart Emergency Vehicle Priority System

An AI-driven traffic management platform that gives ambulances, fire engines and police
vehicles an uninterrupted path through a congested city — predicting their route, holding
traffic signals green ahead of them, warning drivers in their way, and putting the patient
in the *right* hospital rather than the nearest one.

Built as a Django platform. All six layers of the problem statement are implemented and
working end to end, with no IoT hardware required for the initial deployment.

```
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py seed_users
npm --prefix frontend install && npm --prefix frontend run build
python manage.py runserver          # then open http://127.0.0.1:8000/
python manage.py simulate --trips 3 # in a second terminal — watch the console move
```

---

## 1. What is running

| Layer | Problem statement | Where it lives |
|---|---|---|
| **1** | Emergency Vehicle Tracking | [apps/fleet/](apps/fleet/) |
| **2** | AI Traffic Intelligence Engine | [apps/brain/](apps/brain/) |
| **3** | Smart Traffic Signal Control | [apps/dispatch/corridor.py](apps/dispatch/corridor.py), [controllers.py](apps/dispatch/controllers.py) |
| **4** | Driver Alert System | [apps/alerts/](apps/alerts/) |
| **5** | Hospital Recommendation & Coordination | [apps/hospitals/](apps/hospitals/) |
| **6** | Ambulance Light & Siren Priority Control | [apps/dispatch/siren.py](apps/dispatch/siren.py) |
| — | Road graph, signals, cameras, disruptions | [apps/network/](apps/network/) |
| — | Analytics & hotspots (4.8, 4.9) | [apps/analytics/](apps/analytics/) |
| — | Operator dashboards (4.7, 4.11) | [apps/dashboards/](apps/dashboards/) |

### The reactive chain

One GPS fix from an ambulance drives everything else. The whole sequence lives in one
readable function — [`apps/dispatch/orchestrator.py:on_vehicle_position`](apps/dispatch/orchestrator.py) —
so the end-to-end behaviour of the platform can be read and tested as a unit:

```
GPS fix
  └─ route progress          apps/brain/eta.py
     ├─ ETA refresh          → hospital dashboard, ops dashboard
     ├─ green corridor       apps/brain/corridor.py (plan) → apps/dispatch/corridor.py (actuate)
     ├─ driver alerts        apps/alerts/dispatcher.py    → geohash-sharded WebSocket fan-out
     ├─ reroute check        apps/brain/rerouting.py      → new route if the world changed
     └─ priority reassess    apps/dispatch/siren.py       → lights / siren / signal entitlement
```

---

## 2. Design decisions worth knowing

**Routing is time-dependent, not shortest-path.** A conventional router asks "which road is
shortest?". SEVPS asks "which road will be fastest *at the moment the ambulance reaches
it*". Edge cost therefore depends on arrival time at the edge's tail node, which rules out
NetworkX's stock `astar_path` (its weight callback cannot see accumulated time). The search
in [apps/brain/router.py](apps/brain/router.py) carries arrival time in the label and
queries the congestion forecaster per edge. This is what makes "forecasts congestion
several minutes before the ambulance reaches the area" an actual routing input rather than
a dashboard widget.

Both **A\*** and **Dijkstra** are implemented over the same time-dependent cost. The A\*
heuristic (straight-line distance ÷ fastest speed in the network) is admissible, so it
returns the same optimum while expanding materially fewer nodes — asserted directly in
[apps/brain/tests.py](apps/brain/tests.py) and exposed at `POST /api/v1/brain/route/compare/`.

**Priority level changes the route, not just the siren.** A Level 1 vehicle gets a green
corridor, so it pays almost nothing at signalised junctions; a Level 4 transport waits like
everyone else. Modelling that *inside* the search means a Level 1 route may legitimately
prefer a road with more signals but higher speed, while a Level 4 route avoids them. On the
seeded network the same origin/destination pair costs ~5.9 min at Level 1 and ~12.3 min at
Level 4.

**Capability is a hard filter in hospital selection.** The rule engine converts the
paramedic's category tap into a mandatory facility set; hospitals lacking any of them are
*excluded*, with the reason recorded, before distance is even considered. A near hospital
that cannot treat the patient is not a candidate at any distance. Only the survivors are
then scored on travel time, beds, workload and quality — and each candidate carries its
per-factor scores so the ranking can be defended afterwards.

When nothing qualifies, the fallback relaxes constraints **in tiers, weakest first**:
capability before capacity, and *never* a declared diversion — a hospital's formal refusal
to accept patients is a clinical decision the algorithm has no standing to reverse. If that
leaves no candidate, the case escalates to a human instead of being silently forced. Every
relaxation is retained as a visible warning on the recommendation rather than erased.

**Signal preemption is fail-safe, never fail-open.** A controller that cannot be reached
leaves its junction on normal timing — which is safe. Holds are capped
(`SIGNAL_MAX_HOLD_S`), exactly one vehicle may hold a junction at a time, and a background
sweep force-releases anything that outlived its window. Without that sweep, a vehicle
losing GPS mid-corridor would leave a junction green indefinitely; a light stuck green is
far more dangerous than one that reverts early.

**Driver alerts target the road ahead, not a circle.** Alerts are placed at sample points
along the vehicle's *planned route* over the next 90 seconds, each carrying the seconds
until the vehicle reaches that point. A driver 15 seconds ahead is told "15 seconds"; a
driver the ambulance has already passed is told nothing. Level 4 transports raise no alerts
at all — noise is what makes real warnings ignorable.

**The AI engine is stateless.** [apps/brain/](apps/brain/) owns no tables. Everything it
needs lives in `network` (world state) and `dispatch` (routes, corridors), and everything it
produces is returned or written back. A route can therefore be recomputed from scratch at
any moment and the engine scales horizontally with no coordination.

---

## 3. Running it

### Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # optional — every value has a working default
python manage.py migrate
python manage.py seed_demo    # synthetic Chennai network, hospitals, fleet, history
python manage.py seed_users   # role groups + one demo account per role
```

`seed_demo` builds 144 intersections, ~520 directed segments with an
arterial/secondary/residential hierarchy, 58 signalised junctions, 22 cameras, 22 VMS
boards, 8 hospitals with realistic capability mixes, a 9-vehicle fleet and a 260-record
accident archive. The grid is deliberately *irregular* — jittered geometry, some one-way
links, class-dependent speeds — because a perfect grid makes routing look better than it is.

### Processes

```bash
npm --prefix frontend install     # once
npm --prefix frontend run build   # build the React console

python manage.py runserver        # ASGI (Daphne): HTTP + WebSockets in one process
python manage.py sevps_worker     # corridor safety sweep, CV sweep, board expiry, rollups
python manage.py simulate --trips 3 --events    # end-to-end demonstration
```

Frontend development with hot reload — Vite proxies API and WebSocket traffic to Django,
so the httpOnly refresh cookie behaves exactly as it does in production:

```bash
npm --prefix frontend run dev     # http://127.0.0.1:5173
```

`simulate` is not a mock: it moves vehicles along the routes SEVPS actually computes and
feeds them through the same orchestrator and corridor logic as production.

**Multi-process note.** The default in-memory channel layer is per-process, so WebSocket
events published by `simulate` or `sevps_worker` do not reach dashboards held open by the
server process. The dashboards poll as a fallback (2–4 s) and therefore stay correct under
every topology — but for instant push across processes, set `SEVPS_REDIS_URL` and install
`channels-redis`. Actions taken through the API or the dashboards themselves are raised
inside the server process and always push instantly.

SQLite is configured in WAL mode ([apps/core/apps.py](apps/core/apps.py)) so the server,
worker and simulator can write concurrently; without it the default rollback journal
deadlocks that topology within seconds.

### Screens

The **React console** is the primary UI (Phase 4). The original server-rendered screens
are retained under `/legacy/` as a no-build fallback for kiosk and embedded displays.

| URL | Screen |
|---|---|
| `/` | Emergency Operations Dashboard (4.11) — live map, corridors, disruptions |
| `/paramedic/<callsign>/` | Paramedic app — assessment → recommendation → corridor |
| `/hospital/<code>/` | Hospital Preparedness Dashboard (4.7) |
| `/driver/` | Driver alert receiver — the road user's view |
| `/boards/` | Digital display board wall |
| `/analytics/` | AI Traffic Analytics (4.8) + accident hotspots (4.9) |
| `/settings` | Account, roles and live backend status |
| `/admin/` | Full data administration, including the clinical rule base |
| `/login` | React console sign-in (JWT) |
| `/legacy/…` | Server-rendered fallback screens; `/legacy/login/` for session auth |

### Roles and sign-in

There is **one authentication system with four roles**, not a separate login per app.
Dashboards are readable without signing in — a display board or a control-room wallboard
should not need credentials — but every write is gated by role.

| Account | Password | Can do |
|---|---|---|
| `admin` | `sevps-admin` | Everything, plus the Django admin site |
| `operator` | `sevps-operator` | Signal/corridor override, reroute, road events, CV sweep, hotspot recompute |
| `paramedic` | `sevps-paramedic` | Telemetry, patient assessment, hospital confirmation, stage changes |
| `hospital` | `sevps-hospital` | Live capacity, diversion, acknowledge pre-arrival alerts |

Created by `seed_users`. **These passwords are published here — change them before any
deployment reachable beyond your own machine.** The command refuses to run when `DEBUG` is
off unless you pass `--force`.

Sign in at `/login/`. Only `admin` may reach `/admin/`; the three role accounts are
deliberately non-staff, which is why they need `/login/` rather than the admin login page.
`--reset-roles` re-aligns an account that has drifted above its documented privileges.

For multi-hospital deployments, set `Hospital.staff_group`: hospital users are then scoped
to their own hospital and cannot alter another's diversion status. With it unset (the
single-tenant pilot default) any `hospital_staff` member may act.

API clients authenticate with a token instead: `POST /api/v1/auth/token/` with a username
and password, then send `Authorization: Token <key>`.

### Using a real city

```bash
python manage.py import_osm --city Coimbatore --radius 8000 --reset
python manage.py import_osm --bbox 13.00 80.20 13.12 80.30
```

Imports real streets from OpenStreetMap via Overpass, splits ways at junctions, honours
one-way and `maxspeed` tags, and creates signals from OSM's own `highway=traffic_signals`
nodes.

---

## 4. Optional subsystems

SEVPS runs fully with the base requirements. Three subsystems upgrade in place when their
dependencies are installed, and degrade gracefully when they are not — `GET /api/v1/info/`
reports which backend is actually live.

| Subsystem | Default | Upgrade |
|---|---|---|
| Congestion prediction | Statistical (live ⊕ learned profile, horizon-decayed) | `pip install scikit-learn numpy joblib` + `train_congestion_model` |
| Computer vision | Simulated detections from segment state and demand curve | `pip install ultralytics opencv-python-headless`, `SEVPS_CV_MODE=yolo` |
| Channel layer | In-memory (single process) | `pip install channels-redis`, set `SEVPS_REDIS_URL` |
| Database | SQLite | `SEVPS_DB_ENGINE=postgres`, optionally `SEVPS_ENABLE_POSTGIS=1` |

The ML training command reports its accuracy against the built-in predictor and **tells you
not to enable the model if it loses** — an ML model that underperforms a statistical
baseline is a liability, not a feature.

```bash
python manage.py learn_traffic_profiles --days 30
python manage.py train_congestion_model --days 60 --out models/congestion.joblib
```

---

## 5. Geospatial approach

The platform stores coordinates as indexed float pairs and does exact distance work in pure
Python WGS84 maths ([apps/core/geo.py](apps/core/geo.py)), rather than requiring GeoDjango.
This keeps the schema portable and means the same code runs on SQLite for a laptop pilot
and PostgreSQL for a city deployment. Radius searches use a cheap bounding-box pre-filter
(index-friendly, always a superset of the true circle) followed by an exact haversine test.

PostGIS remains available (`SEVPS_ENABLE_POSTGIS=1`) for deployments that want native
spatial indexes at scale; the scoring maths is unchanged either way. This was a deliberate
trade: GeoDjango would have required GDAL binaries on every developer and pilot machine, in
exchange for indexing gains that only matter well beyond pilot scale.

---

## 6. Tests

```bash
python manage.py test apps                 # 162 backend tests
npm --prefix frontend run typecheck        # strict TypeScript
npm --prefix frontend run test             # 19 frontend tests
```

Covering: haversine/bearing/projection against known values; A\* optimality versus Dijkstra;
time-dependent cost and priority-dependent signal delay; congestion blending and horizon
decay; the exact facility mapping from the problem statement's table; capability-over-
proximity and every hard-filter exclusion path; tiered relaxation and the
never-override-diversion guarantee; the four-level siren ladder and upgrade/downgrade
triggers; corridor hold caps, conflict resolution and the safety sweep; and alert targeting
(ahead-not-around, no Level 4 noise, dedup on repeat).

---

## 7. API

`GET /api/v1/info/` lists every endpoint and the live backend for each subsystem.
The browsable API is at `/api/v1/`. Auth is token (`POST /api/v1/auth/token/`) or session.

Reads are open by default so dashboards, display boards and navigation integrations can
consume them without credentials; writes require authentication, and control-plane actions
(signal commands, capacity edits, hotspot recomputation) require operator or hospital-staff
privileges.

Key endpoints:

```
POST /api/v1/fleet/vehicles/{id}/telemetry/    the GPS fix that drives everything
POST /api/v1/brain/route/                      time-dependent optimised route
POST /api/v1/brain/route/compare/              A* vs Dijkstra on the same request
POST /api/v1/brain/congestion/forecast/        predicted speeds N minutes ahead
POST /api/v1/hospitals/recommend/              category + location → ranked hospitals
POST /api/v1/dispatch/trips/{id}/assess/       paramedic assessment → the whole Layer 5/6 chain
GET  /api/v1/dispatch/trips/{id}/corridor/     live green corridor state
POST /api/v1/dispatch/trips/{id}/condition/    re-triage → Layer 6 upgrade/downgrade
GET  /api/v1/analytics/summary/                the analytics dashboard in one call
```

WebSockets: `/ws/ops/`, `/ws/hospital/<code>/`, `/ws/vehicle/<callsign>/`, `/ws/drivers/`,
`/ws/signals/`.

---

## 8. Integrating real traffic signals

Cities expose controllers differently. SEVPS keeps that variation at one edge:
[apps/dispatch/controllers.py](apps/dispatch/controllers.py) defines the adapter interface,
ships `simulated`, `http` and `null` (observe-only, for junctions under manual police
control), and `register_controller()` accepts a city-specific implementation. Everything
above the adapter works in terms of `ControllerResult` and never knows the difference.

For cabinets that cannot expose an inbound endpoint, a cabinet-side agent can instead
connect to `/ws/signals/` and receive `signal_command` events.

---

## 9. Honest limitations

- The **computer vision** module runs in simulated mode by default. The YOLOv8 path is
  implemented and will run against real RTSP feeds, but has only been exercised against the
  simulated backend here — no real camera estate was available.
- **Congestion prediction** ships as a statistical model. The ML path is implemented and
  benchmarked, but a genuinely useful learned model needs months of real observations; on
  synthetic data the statistical baseline is honestly competitive.
- Signal preemption has been validated against the **simulated controller** only. Field
  deployment requires the city's own controller adapter and, more importantly, a safety case
  signed off by the traffic authority — the hold caps and conflict rules here are a starting
  point for that conversation, not a substitute for it.
- The seeded road network is **synthetic**. Use `import_osm` for real geometry before
  drawing conclusions about real routes.
