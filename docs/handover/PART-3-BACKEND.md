# Part 3 — Backend Documentation (§6)

Every Django app, model, serializer, view, service, utility, middleware, signal,
management command, permission, authentication mechanism, WebSocket consumer,
routing file and URL. See [README.md](README.md) for the index.

## Contents

- [6.0 How to read this section](#60-how-to-read-this-section)
- [6.1 apps/core](#61-appscore--cross-cutting-foundation)
- [6.2 apps/fleet](#62-appsfleet--layer-1-vehicle-tracking)
- [6.3 apps/network](#63-appsnetwork--road-infrastructure-cv-gis)
- [6.4 apps/brain](#64-appsbrain--layer-2-ai-traffic-intelligence)
- [6.5 apps/hospitals](#65-appshospitals--layer-5-hospital-recommendation)
- [6.6 apps/dispatch](#66-appsdispatch--layers-3--6)
- [6.7 apps/alerts](#67-appsalerts--layer-4-driver-alerts)
- [6.8 apps/analytics](#68-appsanalytics--features-48--49)
- [6.9 apps/notify](#69-appsnotify--push-delivery)
- [6.10 apps/dashboards](#610-appsdashboards--ui-serving)
- [6.11 Middleware and signals](#611-middleware-and-signals)
- [6.12 Management commands](#612-management-commands-14)
- [6.13 Permission classes](#613-permission-classes)
- [6.14 WebSocket consumers](#614-websocket-consumers)
- [6.15 Routing files](#615-routing-files)

---

## 6.0 How to read this section

Each app is documented as: **purpose → models → serializers → views → services →
dependencies**. For every unit the four questions are answered: what it is
responsible for, what goes in, what comes out, and what it depends on.

A structural rule holds throughout and is worth stating once:

> **Views orchestrate; services decide.** A `views.py` resolves permissions,
> validates input through a serializer, calls a domain service, and serialises
> the result. All behaviour lives in the service modules. This is why
> `manage.py simulate` can drive the whole platform without issuing a single
> HTTP request — it calls the same services.

---

## 6.1 `apps/core` — cross-cutting foundation

**Purpose.** Everything shared. Geometry maths, the role registry, permissions,
JWT, the realtime wrapper, the notification vocabulary, the spatial abstraction,
base models, and the machine-checked access policy.

**Rule:** `core` imports no other SEVPS app at module level. Where it must
(push fan-out inside `publish()`), the import is function-local and wrapped in
`try/except`, so a deployment without `apps.notify` still publishes normally.

### 6.1.1 `geo.py` — pure geometry

| Function | Input | Output | Notes |
|---|---|---|---|
| `haversine_m(lat1, lon1, lat2, lon2)` | four floats | metres | Great-circle distance |
| `distance_m(a, b)` | two `Point` | metres | Convenience over the above |
| `bearing_deg(...)` | four floats | 0–360 | Compass bearing; drives the "approach arrow" in driver alerts |
| `bearing_delta(a, b)` | two bearings | signed degrees | Shortest angular difference |
| `destination_point(lat, lon, bearing, distance)` | | `Point` | Dead reckoning |
| `bounding_box(lat, lon, radius_m)` | | `(min_lat, min_lon, max_lat, max_lon)` | **The SQLite prefilter.** Cheap index-usable box before the expensive haversine |
| `interpolate(a, b, fraction)` | | `Point` | Position along an edge |
| `project_on_segment(p, a, b)` | | `(Point, fraction, distance)` | Foot of perpendicular — how a vehicle is located on its route |
| `polyline_length_m(points)` | | metres | Route length |
| `point_along_polyline(points, d)` | | `Point` | "Where will the vehicle be in 40 s" |
| `distance_to_polyline_m(p, points)` | | `(distance, offset)` | Off-route detection |
| `geohash(lat, lon, precision=6)` | | str | **Precision 6 ≈ 1.2 km cell** — the driver-alert shard key |
| `geohash_neighbours(lat, lon, p)` | | list[str] | The 8 surrounding cells; a vehicle near a boundary must warn both sides |
| `polyline_to_geojson(points)` | | dict | |
| `kmh_to_ms` / `ms_to_kmh` | | float | |

**Dependencies: none.** Deliberately. This module is importable from a
management command, a consumer, a test or a REPL with no Django setup.

### 6.1.2 `roles.py` — the role registry

One registry consumed by three things that must never disagree: the permission
classes, `seed_users`, and the JWT claims.

| Symbol | Purpose |
|---|---|
| `Role` | Canonical group names as constants. **Import these; never hard-code the string** |
| `RoleSpec` | Per-role metadata: label, description, `clinical_access`, `traffic_control`, `dispatch_control`, `aliases` |
| `ROLES` | `dict[str, RoleSpec]` — the registry |
| `ALIASES` | legacy group name → canonical key, derived from the specs |
| `canonical(group_name)` | Resolve a possibly-legacy name |
| `user_roles(user)` | Every role held, aliases resolved. **A superuser implicitly holds all** |
| `has_role(user, *roles)` | Membership test |
| `group_names_for(role)` | The inverse of `canonical` — canonical *plus* aliases. Needed when a role must become a database filter rather than a check against a loaded user |
| `OPERATIONAL_ROLES` | Everything except `public_users` |
| `CLINICAL_ROLES` / `TRAFFIC_ROLES` / `DISPATCH_ROLES` | Derived from the spec flags |
| `may_view_clinical_data(user)` | The PHI gate |
| `describe_roles()` | Machine-readable catalogue served at `/auth/roles/` |

**Why a superuser holds every role:** an administrator locked out of their own
platform during an incident is a worse failure than an over-broad grant.

**Why aliases rather than renames:** a deployment that already has `operators`
and `paramedics` groups keeps working after upgrade. That is the entire point of
an incremental migration.

### 6.1.3 `permissions.py`

Documented in full at [§6.13](#613-permission-classes).

### 6.1.4 `jwt.py` — token issuance

| Symbol | Purpose |
|---|---|
| `SEVPSTokenObtainPairSerializer` | Adds `roles`, `is_superuser` to the token claims so a client can render its own UI by role without a second round trip |
| `SEVPSTokenObtainPairView` | Issues the pair; sets the refresh cookie |
| `CookieTokenRefreshSerializer` | Reads the refresh token from the **cookie** when absent from the body |
| `SEVPSTokenRefreshView` / `SEVPSTokenVerifyView` | Rotation and validity |
| `REFRESH_COOKIE = "sevps_refresh"` | httpOnly, `Path=/api/v1/auth/`, `SameSite=Lax`, `Secure` when not DEBUG |

**Why the cookie is path-scoped:** it is only ever needed by the auth endpoints.
Scoping it means it is not attached to every request to every endpoint, which
shrinks both the CSRF surface and the accidental-logging surface.

### 6.1.5 `ws_auth.py` — WebSocket authentication

```python
def JWTAuthMiddlewareStack(inner):
    return AuthMiddlewareStack(JWTAuthMiddleware(inner))
```

**The nesting order is load-bearing and was a real bug.** Reversed, the session
middleware runs *after* the JWT middleware and overwrites an authenticated
`scope["user"]` with `AnonymousUser`. Every socket then connects anonymously and
the dashboards silently degrade.

### 6.1.6 `ws_policy.py` — socket authorisation, declared

A REST view's permission class is visible in review; a consumer has no
equivalent. `ConsumerPolicy` gives it one.

| Policy | Socket | Allowed roles | Commands |
|---|---|---|---|
| `OPS_POLICY` | `/ws/ops/` | any operational role | — |
| `VEHICLE_POLICY` | `/ws/vehicle/<callsign>/` | crew, dispatcher, admin | telemetry |
| `HOSPITAL_POLICY` | `/ws/hospital/<code>/` | hospital, dispatcher, admin | acknowledge |
| `SIGNALS_POLICY` | `/ws/signals/` | **traffic police, admin only** | signal commands |
| `DRIVERS_POLICY` | `/ws/drivers/` | anonymous permitted | position |

`SIGNALS_POLICY` exists because a live probe found `/ws/signals/` accepting
**unauthenticated commands** to traffic-signal controllers.

### 6.1.7 `consumers.py` — `GroupConsumer`

Base class for all five sockets.

| Responsibility | Detail |
|---|---|
| Authorisation | `_authorise()` runs before any group is joined. `self._groups = []` is initialised **first** so a refusal cannot crash `disconnect()` |
| Snapshot | On connect, the policy's snapshot function sends current state so a client is not blank until the next event |
| Sequencing | Every frame carries `seq` and `ts`. A gap tells the client it missed frames |
| Coalescing | `COALESCED_EVENTS = {"vehicle_position": "callsign"}`, `COALESCE_WINDOW_S = 0.9` |
| Subscription filters | A client narrows what it receives without a second socket |
| Viewer identity | `viewer_identity()` tells the client whether the socket authenticated |

`_authorise()` wraps its role check in `database_sync_to_async` — reading
`user.groups` in an async context otherwise raises `SynchronousOnlyOperation`.

### 6.1.8 `realtime.py`, `notifications.py`, `live.py`, `spatial.py`, `api_policy.py`

| Module | Key symbols | Notes |
|---|---|---|
| `realtime.py` | `broadcast`, `broadcast_many`, `broadcast_ops`, `hospital_group`, `vehicle_group`, `driver_group` | **Never raises.** A broken fan-out must not abort a dispatch decision |
| `notifications.py` | `Notification`, `Severity`, `publish()`, plus `hospital_prepare`, `corridor_failed`, `priority_escalated`, `route_blocked`, `no_hospital_available` | One function per operationally important message, so wording and audience are decided once and reviewable in one place |
| `live.py` | `tick_etas`, `tick_traffic`, `tick_fleet_health`, `tick_all`; `STALE_AFTER_S = 60`, `ETA_CHANGE_THRESHOLD_S = 15.0` | The worker's per-cycle work. The threshold stops an ETA that wobbles by two seconds from generating a broadcast storm |
| `spatial.py` | `postgis_available()`, `near_queryset()`, `generated_column_sql()`, `POINT_TABLES` (13), `LINESTRING_TABLES`, `spatial_status()` | The dual-backend switch, resolved once and cached |
| `api_policy.py` | `PUBLIC_READ_ENDPOINTS`, `CLINICAL_ENDPOINTS`, `audit_api_permissions()`, `policy_summary()` | Walks the live URLconf and flags endpoints relying on the global default or public without a written reason |

### 6.1.9 Core views

| View | URL | Permission | Returns |
|---|---|---|---|
| `LivenessView` | `/health/live/` | public | `{"status":"alive"}` — **checks nothing** |
| `ReadinessView` | `/health/ready/` | public | DB + channel-layer status; 503 when not ready |
| `HealthView` | `/health/` | public | Combined; kept for existing monitors |
| `ServiceInfoView` | `/info/` | public | Which optional backends are actually active |
| `WhoAmIView` | `/auth/me/` | `IsAuthenticated` | Identity + capabilities, **re-derived from the database** |
| `RoleCatalogueView` | `/auth/roles/` | public | Static role catalogue |
| `AccessPolicyView` | `/auth/policy/` | `IsAdministrator` | Live REST + WebSocket policy audit |
| `LogoutView` | `/auth/jwt/logout/` | public | Blacklists the refresh token, clears the cookie |

**Liveness checks nothing on purpose.** A liveness probe answers "should the
orchestrator kill this container", and the honest answer during a database
outage is *no* — restarting cannot fix it and only removes a process that would
have recovered.

---

## 6.2 `apps/fleet` — Layer 1, vehicle tracking

**Purpose.** Where every emergency vehicle is, how fast, facing where, and how
long ago it said so.

### Models

- **`Station`** — home base. `code` unique.
- **`EmergencyVehicle`** — the operational unit. Key methods:
  - `is_online` — `last_seen_at` within `STALE_AFTER_S`
  - `project(seconds)` — dead reckoning from last heading and speed, used to
    render smooth motion between 1 Hz fixes
  - `ingest(lat, lon, ...)` — updates position and appends telemetry
- **`VehicleTelemetry`** — append-only GPS history.
- **`VehicleQuerySet(GeoQuerySet)`** — `.online()`, `.available()`,
  `.on_mission()`, and inherited `.near()`.

> `VehicleQuerySet` **extends `GeoQuerySet`** rather than `models.QuerySet`.
> An earlier version did not, and `.near()` silently disappeared from the
> custom manager.

### Serializers

| Serializer | Purpose |
|---|---|
| `StationSerializer` | Station CRUD |
| `EmergencyVehicleSerializer` | Adds `is_online`, display labels |
| `VehicleTelemetrySerializer` | Track playback |
| `TelemetryIngestSerializer` | **Input validation** for a GPS fix: lat/lon bounds, optional speed/heading/accuracy |
| `VehicleStatusSerializer` | Status transition |

### Views — `EmergencyVehicleViewSet`

| Action | Method · URL | Purpose |
|---|---|---|
| list/retrieve/create/update | `/fleet/vehicles/` | CRUD |
| `live` | GET `…/live/` | Every online vehicle, one payload — the ops dashboard's polling floor |
| `nearest` | GET `…/nearest/?lat=&lon=&type=` | Nearest available vehicle of a type |
| `telemetry` | POST `…/{id}/telemetry/` | **Ingest a GPS fix.** Triggers `orchestrator.on_vehicle_position()` |
| `track` | GET `…/{id}/track/` | Telemetry history |
| `projection` | GET `…/{id}/projection/?seconds=` | Dead-reckoned future position |
| `status` | POST `…/{id}/status/` | Set operational status |

**`telemetry` is the busiest write path in the platform** — one call per vehicle
per second. It is deliberately thin: validate, ingest, hand to the orchestrator,
return. Everything expensive (rerouting, corridor sync) is decided inside the
orchestrator and short-circuits early when nothing has changed.

**Dependencies:** `core.geo`, `core.realtime`, `dispatch.orchestrator`.

---

## 6.3 `apps/network` — road infrastructure, CV, GIS

**Purpose.** The physical world: junctions, roads, signals, cameras, live
traffic observations, learned profiles, disruptions and historical accidents.
Also hosts the computer-vision package and the GIS layer registry.

### Models

| Model | Role |
|---|---|
| `Intersection` | Graph node. `is_signalised`, `base_delay_s` |
| `RoadSegment` | Directed graph edge. Live speed, congestion index, closure. `apply_speed()` recomputes congestion |
| `TrafficSignal` | One-to-one with a signalised intersection. `supports_preemption`, `min_recovery_s`, current pre-emption state |
| `CameraFeed` | CV source, optionally bound to an intersection or segment |
| `TrafficObservation` | Append-only measurement (sensor / camera / probe / simulated) |
| `TrafficProfile` | Learned speed factor per `(segment, weekday, hour)` |
| `RoadEvent` | Live disruption: accident, closure, construction, flooding, blockage |
| `AccidentRecord` | Historical incident, for hotspot clustering |

### Serializers

Standard ModelSerializers plus:
- **`RoadSegmentGeoSerializer`** — GeoJSON output for the map, `[lon, lat]`.
- **`SpeedUpdateSerializer`** — bulk speed ingestion from roadside sensors.
- **`CameraAnalysisRequestSerializer`** — optional frame + parameters.

### Views

| ViewSet / view | Notable actions |
|---|---|
| `IntersectionViewSet` | CRUD |
| `RoadSegmentViewSet` | `geojson` (map layer), `speeds` (bulk ingest) |
| `TrafficSignalViewSet` | `status` (estate summary), `heartbeat` (controller liveness) |
| `CameraFeedViewSet` | `analyse` (run CV on one camera) |
| `TrafficObservationViewSet`, `RoadEventViewSet` (`clear`), `AccidentRecordViewSet` | CRUD + lifecycle |
| `cv_views.py` | `VisionStatusView`, `analyse_one`, `sweep_cameras`, `emergency_sightings`, `AnalyseAllCamerasView` |
| `gis_views.py` | `LayerCatalogueView`, `LayerView` (per-layer permission resolution), `basemaps` |

**`PublicReadTrafficWrite`** is the permission on the infrastructure ViewSets:
road geometry and signal locations are equivalent to published OpenStreetMap
data and navigation integrations consume them without accounts; *changing* them
is a traffic-authority action.

### The CV package — `apps/network/cv/`

| File | Contents |
|---|---|
| `detection.py` | `Detection`, `Finding` (`is_actionable` at 0.55), `FrameAnalysis`, Greenshields helpers, `FINDING_TO_EVENT` |
| `backends.py` | `detect_with_yolo`, `detect_simulated`, `detect()` with automatic fallback |
| `analysers.py` | Six detectors — see [Part 5 §11](PART-5-AUTH-AI-CV-GIS.md) |
| `pipeline.py` | `analyse_camera`, `ingest`, `sweep`, `_update_still_tracks` |

`apps/network/vision.py` is now a **backward-compatible facade** over the
package; existing imports keep working.

### The GIS registry — `apps/network/gis.py`

Eleven layer builders plus `LayerSpec`/`LAYERS` and `basemap_providers()`.
Documented in [Part 5 §12](PART-5-AUTH-AI-CV-GIS.md).

---

## 6.4 `apps/brain` — Layer 2, AI traffic intelligence

**Purpose.** Everything that decides *where* and *how long*. No models of its
own (`migrations/` is empty by design) — it is a pure computation layer over
`network`, `fleet` and `dispatch`.

### `graph.py` — the routing graph

| Symbol | Purpose |
|---|---|
| `TOPOLOGY_TTL_S = 60.0` | Nodes and edges change rarely; rebuilding is expensive |
| `STATE_TTL_S = 5.0` | Speeds and closures change constantly; rebuilding is cheap |
| `EdgeState` | Per-edge live view: length, free-flow, current speed, congestion, closure |
| `Topology` / `NetworkState` | The two cached tiers |
| `get_topology()` / `get_state()` | Cached accessors |
| `nearest_node(lat, lon)` / `nearest_nodes(...)` | Snap a coordinate onto the graph |
| `segment_between(u, v)` | Edge lookup |
| `invalidate()` | Force rebuild — called after network edits |
| `graph_summary()` | Diagnostics for `/brain/network/summary/` |

### `router.py` — time-dependent shortest path

| Symbol | Purpose |
|---|---|
| `RouteRequest` | origin, destination, priority level, departure time, algorithm |
| `Route` / `RouteStep` | Result: geometry, steps, distance, duration, node ids |
| `compute_route(request)` | Entry point |
| `_search(...)` | **The custom A*/Dijkstra.** `MAX_EXPANSIONS = 250_000` |
| `_materialise(...)` | Path → geometry, steps and timings |
| `route_between(...)`, `estimate_travel_time_s(...)`, `route_length_m(...)` | Convenience |
| `_speed_multiplier(level)`, `_signal_delay_s(level, base)` | Priority effects |

**Why not `networkx.astar_path`:** its weight callback receives `(u, v, data)`
and cannot see accumulated travel time, so it cannot express *"this edge is slow
at the time you will arrive at it"* — which is the whole point. NetworkX still
provides the graph structure and connectivity diagnostics.

**How priority changes the route.** A Level 1 vehicle gets a speed multiplier
(it can use gaps a car cannot) and a reduced per-signal delay (it expects a
green corridor). So the *same* origin/destination can produce different routes
at different priority levels — which is correct: a route that is optimal only
if you are given green lights is the wrong route for a Level 4 transfer.

### `eta.py` — progress and arrival

| Symbol | Purpose |
|---|---|
| `RouteProgress` | Distance along, distance remaining, off-route flag |
| `compute_progress(plan, position)` | Project the vehicle onto its route |
| `project_vehicle(vehicle, seconds)` | Future position + confidence |
| `eta_for_trip(trip)` | The ETA payload broadcast to dashboards |
| `upcoming_signal_arrivals(plan, progress, horizon)` | **Which junctions, and when** — the corridor planner's input |
| `OFF_ROUTE_THRESHOLD_M = 90.0` | Beyond this, the vehicle has left its route |
| `ARRIVAL_GRACE_S = 12.0` | A junction stays in the plan slightly past predicted arrival |

`ARRIVAL_GRACE_S` fixed a real defect: under sparse telemetry the vehicle's
predicted arrival passed between fixes, the junction dropped out of the plan,
and the corridor never activated.

### `corridor.py` — green-corridor planning

| Symbol | Purpose |
|---|---|
| `SignalPlanEntry` | One junction: when green, when release, clearance, score |
| `CorridorPlan` | The ordered plan, plus `skipped_signal_ids` |
| `plan_corridor(trip, progress, lookahead_s)` | Build it |
| `_clearance_for(congestion_index, lanes)` | `BASE_CLEARANCE_S = 8.0` … `MAX_CLEARANCE_S = 35.0` |

**Clearance is computed, not fixed.** A congested four-lane approach needs
longer to empty than a free-flowing two-lane one. Getting this wrong means the
ambulance arrives at a green light behind stationary traffic that has not moved
yet — the corridor "worked" and achieved nothing.

`skipped_signal_ids` exists because an earlier version cancelled and recreated
pre-emption rows every tick, churning the table and the audit trail.

### `priority.py`, `congestion.py`, `rerouting.py`

| Module | Key symbols | Purpose |
|---|---|---|
| `priority.py` | `PriorityScore`, `score_trip`, `rank_trips`, `resolve_intersection_conflict` | **Contention.** Two ambulances converging on one junction: whoever scores lower yields, and the yield is recorded via `yielded_to` |
| `congestion.py` | `CongestionForecaster`, `SegmentForecast`, `build_forecaster`, `load_profiles`, `load_event_penalties`, `blocked_segment_ids`, `predictor_backend` | Per-segment speed factor at a future time. `LIVE_DECAY_TAU_S = 600` — a live measurement decays toward the historical profile rather than being trusted forever |
| `rerouting.py` | `RerouteDecision`, `evaluate_trip`, `reassess_active_trips` | Hysteresis: `REROUTE_MIN_GAIN_S = 45`, `REROUTE_MIN_INTERVAL_S = 30`. Without both, the route thrashes on noise and the crew loses confidence in it |

### The ML package — `apps/brain/ml/`

| File | Contents |
|---|---|
| `base.py` | `Prediction`, `Explanation`, `FeatureContribution`, `SEVPSEstimator`, `ACTIONABLE_CONFIDENCE = 0.6`, SHAP integration |
| `estimators.py` | Five estimators + the registry |
| `services.py` | The callable surface: `predict_congestion`, `predict_eta`, `predict_priority`, `predict_corridor_success`, `predict_clearance`, `explain_hospital_recommendation` |
| `training.py` | `TrainingResult`, `should_deploy` (`MIN_IMPROVEMENT = 0.05`), `_split` |

Detail in [Part 5 §10](PART-5-AUTH-AI-CV-GIS.md).

### Brain views

`views.py` — `RouteView`, `CompareAlgorithmsView`, `CongestionForecastView`,
`network_summary`, `rebuild_graph`, `corridor_preview`, `priority_ranking`,
`evaluate_reroute`, `reassess_all`.
`ml_views.py` — `CongestionPredictionView`, `predict_trip_eta`,
`PriorityPredictionView`, `predict_corridor`, `ExplainedRecommendationView`,
`ModelStatusView`.

---

## 6.5 `apps/hospitals` — Layer 5, hospital recommendation

**Purpose.** Given a location, a clinical category and a priority level, decide
which hospital can actually treat this patient — and record why.

### `rules.py` — the clinical rule table

`ResolvedRule` carries required facilities, preferred facilities, default
priority, ICU requirement, golden window and crew guidance.
`resolve_rule(category)` reads the database and falls back to a built-in seed;
`seed_rules()` populates it.

Example: `cardiac` requires `emergency_dept` **and** `cath_lab`, prefers
`cardiac_icu`, defaults to Level 1, is time-critical with a 90-minute golden
window.

### `recommender.py` — the scoring engine

```
recommend_hospital(origin, category, priority_level, ...)
  ├─ candidates within SEVPS_HOSPITAL_SEARCH_RADIUS_KM  (default 25)
  ├─ _apply_hard_filters()   diversion → capability → capacity
  ├─ _score_candidate()      weighted, interpretable
  ├─ if none survive: _relax()  tiered, never relaxing diversion
  └─ Recommendation(best, ranked candidates, warnings, rule snapshot)
```

Weights, from `settings.SEVPS["HOSPITAL_WEIGHTS"]`:

| Factor | Weight | Why |
|---|---|---|
| `capability` | 0.34 | The largest single factor. A hospital that cannot treat the condition is not a near miss |
| `travel_time` | 0.30 | Real routed time, not straight-line distance |
| `bed_availability` | 0.18 | |
| `workload` | 0.12 | Patients waiting per doctor on duty |
| `quality` | 0.06 | Outcome index, a tiebreaker rather than a driver |

**Interpretable weights, not a learned model.** A clinician must be able to ask
"why this hospital?" and get an answer they can argue with. The ML layer
*explains* this recommendation (`explain_hospital_recommendation`) but does not
replace it.

**Relaxation is tiered and never relaxes diversion.** If nothing survives, the
recommender relaxes *capability preferences* first, then *capacity*, and never
sends a patient to a hospital that has declared it cannot receive them. Warnings
are retained through relaxation — an early version cleared them, so a relaxed
recommendation looked like a clean one.

### Views

`HospitalViewSet` (`capacity`, `diversion`, `inbound`),
`HospitalCapabilityViewSet`, `EmergencyRuleViewSet` (`catalogue`, `seed`),
`HospitalAlertViewSet` (`acknowledge`), `HospitalRecommendationLogViewSet`,
`RecommendHospitalView`, `RuleLookupView`.

**`IsHospitalStaff` object permission.** When `Hospital.staff_group` is set it
becomes real multi-tenant isolation — only that group may write to that
hospital. When unset (the single-tenant pilot default) any hospital-role user
may act. An earlier version required the group unconditionally, which meant **no
hospital user could act at all** because no deployment sets it.

---

## 6.6 `apps/dispatch` — Layers 3 & 6

**Purpose.** The trip lifecycle, route plans, signal pre-emption execution,
controller adapters and the priority ladder.

### `orchestrator.py` — the trip lifecycle

| Function | Responsibility |
|---|---|
| `create_trip(...)` | Open a response, assign a vehicle, derive priority, plan the first route |
| `assign_hospital(trip, ...)` | Run the recommender (or accept an override), notify the hospital, replan |
| `plan_route_to(trip, destination)` | Compute and attach a route |
| `apply_new_route(trip, route, reason)` | Activate a new plan, deactivate the old, broadcast |
| `on_vehicle_position(vehicle)` | **The hot path.** Progress → arrival check → corridor sync → driver alerts → ETA broadcast |
| `advance_stage(trip, stage, reason)` | Stage machine with timestamping |
| `notify_hospital(trip, ...)` | Inbound alert + notification |

`ARRIVAL_RADIUS_M = 70.0` — within this of the destination, arrival is detected
automatically rather than waiting for the crew to press a button.

**Trip reference generation retries against the unique constraint** with
savepoints. A read-then-write `max(reference) + 1` raced under concurrent
dispatch.

### `corridor.py` — execution

| Function | Responsibility |
|---|---|
| `sync_corridor(trip, progress)` | Reconcile the plan against reality: arm, activate, release |
| `_activate_due(trip)` | Send green commands for junctions whose time has come |
| `_resolve_conflict(preemption)` | Contention: lower score yields, `yielded_to` set |
| `_release_passed(trip)` | Release junctions the vehicle has passed |
| `release_corridor(trip, reason)` | Manual release (traffic police) |
| `tick_corridors()` | The worker's safety sweep — **releases anything overdue even if the trip stopped reporting** |
| `corridor_status(trip)` | Read model for dashboards |

`tick_corridors()` is the safety net. If a vehicle's telemetry dies mid-corridor,
nothing else would ever release those junctions.

### `controllers.py` — signal controller adapters

| Class | Purpose |
|---|---|
| `BaseSignalController` | Interface: `request_green`, `release`, `heartbeat` |
| `SimulatedSignalController` | Default. Updates model state, no network |
| `HttpSignalController` | Real controllers over HTTP, `REQUEST_TIMEOUT_S = 3.0` |
| `NullSignalController` | Explicit no-op for decommissioned junctions |
| `get_controller(signal)` / `register_controller(...)` | Resolution and extension |

`_jsonable(value)` recursively coerces datetimes before storing
`controller_response` in a `JSONField` — an uncoerced datetime raised at write
time and lost the audit record.

### `siren.py` — Layer 6

`PriorityProfile` defines, per level: siren mode, light pattern, whether a green
corridor is granted, speed multiplier, and contraflow permission.

| Level | Meaning | Corridor | Contraflow |
|---|---|---|---|
| 1 | Critical | yes | permitted |
| 2 | High | yes | no |
| 3 | Moderate | no | no |
| 4 | Non-critical transport | no | no |

`derive_priority(trip)` maps category + deterioration + stage onto a level;
`apply_priority(...)` writes a `PriorityDirective` audit row and broadcasts;
`stand_down(vehicle)` returns to normal running.

### Serializers — including redaction

**`ClinicalRedactionMixin`** is the single point where PHI is gated. It inspects
`context["request"].user` (or `context["user"]`), and when the user lacks a
clinical role it blanks `patient_age`, `patient_notes` and `caller_number` and
sets `clinical_data_redacted: true`.

**It fails closed:** no context means redact. This surfaced three call sites
that were not passing context — all fixed, and now covered by the RBAC matrix.

### Views — `EmergencyTripViewSet`

`live`, `assess`, `stage`, `handover`, `cancel`, `corridor`, `corridor/release`,
`corridor/sync`, `reroute`, `condition`. Plus `SignalPreemptionViewSet`,
`RoutePlanViewSet`, `PriorityDirectiveViewSet`, `corridor_tick`,
`priority_profiles`.

---

## 6.7 `apps/alerts` — Layer 4, driver alerts

**Purpose.** Warn the road users who are about to be in the way — *before* the
vehicle arrives.

### `dispatcher.py`

| Function | Responsibility |
|---|---|
| `build_message(seconds_away, level, type)` | Human wording + instruction |
| `broadcast_driver_alerts(trip, progress)` | **The core.** Points ahead along the route → geohash cells → alerts → socket + push + VMS |
| `_point_ahead(plan, progress, seconds)` | Where the vehicle will be in N seconds |
| `_update_display_boards(...)` | Roadside signs on the corridor |
| `_push_to_subscribed_drivers(issued)` | Web Push, **newly issued alerts only** |
| `clear_expired_boards()` | Worker task |
| `active_alerts_near(lat, lon, radius)` | The anonymous road-user lookup |

**Targeting is ahead, not around.** A radius around the vehicle mostly warns
people it has already passed. `DRIVER_ALERT_RADIUS_M = 800`,
`DRIVER_ALERT_MAX_ETA_S = 90`.

**Only new alerts push.** A refresh updates the ETA for an existing cell; the
socket carries that. Buzzing a phone every few seconds for one ambulance is how
a safety channel gets muted permanently.

### Views

`DisplayBoardViewSet` (`live`, `clear-expired`), `DriverDeviceViewSet`,
`DriverAlertViewSet`, `NearbyAlertsView` (**public**), `DevicePositionView`
(**public**).

`alerts-nearby` and `alerts-position` are public because Layer 4's promise is
that a road user's phone receives a warning **without an account**. The device
endpoint stores a coarse geohash cell only, never linked to an identity.

---

## 6.8 `apps/analytics` — features 4.8 & 4.9

### `services.py` — aggregate statistics

`response_time_stats`, `corridor_usage`, `congestion_hotspots`,
`high_delay_intersections`, `emergency_movement_stats`,
`identify_accident_hotspots`, `rollup_daily_metrics`, `dashboard_summary`.

`corridor_usage` reports **cost as well as usage** — `total_hold_seconds` is
how long other road users waited. A corridor system that reports only its
benefits is not measuring itself.

### `trends.py` — chart-shaped series

`daily_series`, `trend`, `demand_profile`, `category_distribution`,
`corridor_outcomes`, `response_distribution`, `hospital_load`, plus the `SERIES`
catalogue.

Three invariants, each pinned by tests:
1. **Contiguous series** — every day emitted, including empty ones.
2. **Zero ≠ not measured** — counts default to `0`, measurements to `null`.
3. **Local-time buckets** — with the timezone reported.

Historical days come from `DailyMetric` where a rollup exists and are computed
live otherwise; the response reports `materialised_days` vs
`computed_live_days`. **Reading never writes a rollup.**

### `exports.py`

Eight streamed CSV datasets, each matching a chart exactly, with provenance
headers (`# window:`, `# generated:`). Unknown dataset → **404 with the valid
list**, never an empty file.

---

## 6.9 `apps/notify` — push delivery

### Models

`PushSubscription` (user nullable for anonymous road users; `endpoint` unique),
`NotificationRecord` (durable history), `NotificationDelivery` (receipts),
`NotificationPreference` (mute list + quiet hours).

### `service.py`

`record_notification`, `infer_category`, `resolve_audience`,
`resolve_driver_audience`, `push_payload`, `deliver`, `publish_and_deliver`,
`deliver_driver_alert`, `prune_dead_subscriptions`. `MAX_FANOUT = 500`.

**Audience is resolved by role at send time**, so a nurse added to the group
this morning is reached this afternoon with no resync.

### Backends

`WebPushBackend` (primary), `FCMBackend` (optional adapter), `ConsoleBackend`
(logs the reason when nothing is configured), behind `get_backend()` which
**falls back rather than raising**.

### `vapid.py`

`generate_keypair`, `public_key_from_private`, `private_key_pem`, `public_key`,
`is_configured`, `claims`, `claims_configured`, `write_keypair`.

The keypair is **deployment identity, not a rotating secret** — regenerating it
invalidates every push subscription in the fleet.

---

## 6.10 `apps/dashboards` — UI serving

| View | Purpose |
|---|---|
| `spa_index` | Serves the built React console for any non-API route; a helpful 501 when unbuilt |
| `service_worker` | **`/sw.js` from the site root** with `Service-Worker-Allowed: /` and `no-cache` |
| `operations`, `hospital`, `paramedic`, `driver`, `boards`, `analytics`, … | Legacy server-rendered screens under `/legacy/` |
| `OpsConsumer` | The `/ws/ops/` consumer |

The legacy screens are **kept, not deprecated**: they are the fallback for kiosk
and embedded displays with no JavaScript build pipeline, and they keep the
platform usable if the SPA build is absent.

---

## 6.11 Middleware and signals

### Middleware (order matters)

| # | Middleware | Purpose |
|---|---|---|
| 1 | `corsheaders.CorsMiddleware` | **Must be first** — CORS headers on error responses too |
| 2 | `SecurityMiddleware` | HSTS, SSL redirect, nosniff |
| 3 | `SessionMiddleware` | Legacy screens and admin |
| 4 | `CommonMiddleware` | URL normalisation |
| 5 | `CsrfViewMiddleware` | Session-authenticated forms. JWT paths are exempt by design |
| 6 | `AuthenticationMiddleware` | `request.user` |
| 7 | `MessageMiddleware` | Legacy flash messages |
| 8 | `XFrameOptionsMiddleware` | `DENY` |

WebSockets use a separate stack: `JWTAuthMiddlewareStack` in `sevps/asgi.py`.

### Signals

**One signal receiver in the whole platform**, in `apps/core/apps.py`:

```python
@receiver(connection_created)
def configure_sqlite(sender, connection, **kwargs):
    # WAL, synchronous=NORMAL, busy_timeout=20000
```

Without it, the simulator and the web process fight over the SQLite file and the
simulator dies with "database is locked". Django's ORM signals
(`post_save` etc.) are deliberately **not** used for business logic — an
orchestration step hidden in a signal is one that cannot be read in the flow it
belongs to.

---

## 6.12 Management commands (14)

| Command | App | Purpose |
|---|---|---|
| `seed_users` | core | Role groups + one demo account per role |
| `seed_demo` | core | Complete demonstration deployment: network, hospitals, fleet |
| `simulate` | core | Live end-to-end simulation with moving vehicles |
| `sevps_worker` | core | Background maintenance: corridor sweep, CV, board expiry, rollups |
| `migrate_to_postgres` | core | SQLite → PostgreSQL/PostGIS with verification at both ends |
| `verify_migration` | core | Fingerprint the database, or compare against an earlier fingerprint |
| `backfill_geometry` | core | Fill PostGIS LineString columns from JSON polylines |
| `import_osm` | network | Import a routable network from OpenStreetMap |
| `learn_traffic_profiles` | network | Recompute historical profiles from observations |
| `analyse_cameras` | network | Run the CV pipeline over the camera estate |
| `train_models` | brain | Train all estimators; deploy only those beating their baseline |
| `train_congestion_model` | brain | Train the congestion model specifically |
| `generate_vapid_keys` | notify | Create the Web Push keypair (refuses to overwrite without `--force`) |
| `push_sweep` | notify | Retire stale or repeatedly failing subscriptions |

`sevps_worker` is the process that must run continuously in production — and
exactly once. See [Part 8 §18](PART-8-DEPLOYMENT-TESTING-PERFORMANCE-SECURITY.md).

---

## 6.13 Permission classes

| Class | Rule | Used for |
|---|---|---|
| `BaseRolePermission` | Base: `required_roles` + `allow_safe_methods` | — |
| `ReadOnlyOrAuthenticated` | Anyone reads; any authenticated writes | Weakest write policy; non-sensitive data only |
| `IsAuthenticatedRole` | **An operational role** for every method | Trips, fleet, analytics, notify |
| `PublicRead` | Public, **read only** | Health, info, catalogues, public GIS layers |
| `PublicDeviceRegistration` | Public, **POST allowed** — self-registration only | Push subscribe/unsubscribe |
| `IsAdministrator` | `administrators` | Rule catalogue, policy audit |
| `PublicReadTrafficWrite` | Anyone reads; traffic control writes | Road network, signals, cameras, events |
| `PublicReadHospitalWrite` | Anyone reads; hospital staff write | Hospital directory and capacity |
| `IsTrafficPolice` | traffic control roles | Signal override, corridor release, CV sweep |
| `IsDispatcher` | dispatch roles | Open/cancel responses |
| `IsAmbulanceCrew` | crew + dispatcher + admin | Telemetry, assessment |
| `IsHospitalStaff` | hospital + admin, with object scoping | Capacity, diversion |
| `IsCrewForVehicle` | Crew may only act for their assigned vehicle | Telemetry |
| `IsOperator` | Alias of `IsTrafficPolice` | ~19 pre-RBAC call sites |

**`IsAuthenticatedRole` is not "any authenticated user."** `public_users` is a
real role whose spec has always read *"no operational or clinical access"*, and
until Phase 12 the permission layer did not enforce it — a citizen with an
account could read live ambulance positions. `/auth/me/` deliberately uses
DRF's plain `IsAuthenticated` so a public user can still read their own account.

---

## 6.14 WebSocket consumers

| Consumer | Route | Group(s) | Snapshot | Commands |
|---|---|---|---|---|
| `OpsConsumer` | `/ws/ops/` | `ops` | Vehicles, trips, open pre-emptions | — |
| `HospitalConsumer` | `/ws/hospital/<code>/` | `hospital.<code>` | Inbound trips, **serializer-redacted** | acknowledge |
| `VehicleConsumer` | `/ws/vehicle/<callsign>/` | `vehicle.<callsign>` | Active trip, route, directives | telemetry |
| `DriverAlertConsumer` | `/ws/drivers/` | `drivers.<geohash6>` | Live alerts in the cell | position |
| `SignalControlConsumer` | `/ws/signals/` | `signals` | Signal estate | signal commands |

`HospitalConsumer` routes its snapshot **through the serializer** rather than a
bespoke payload builder. It previously used `as_hospital_payload()`, which
bypassed `ClinicalRedactionMixin` entirely — PHI over the socket.

---

## 6.15 Routing files

| File | Contents |
|---|---|
| `sevps/urls.py` | `/admin/`, `/api/v1/`, then `apps.dashboards.urls` |
| `sevps/api_urls.py` | Health ×3, info, auth block, then one `include()` per app |
| `sevps/routing.py` | The five WebSocket routes |
| `sevps/asgi.py` | `ProtocolTypeRouter` binding HTTP and the JWT-authenticated WebSocket router |
| `apps/dashboards/urls.py` | `/legacy/`, `/sw.js`, then the SPA catch-all regex |

The SPA catch-all excludes `api/`, `admin/`, `static/`, `media/`, `ws/` and
`legacy/`, and is **deliberately last**. `/sw.js` must precede it or the service
worker would be served the SPA's `index.html`.

---

**Previous:** [Part 2 — Stack, Structure, Database](PART-2-STACK-STRUCTURE-DATABASE.md)
**Next:** [Part 4 — Frontend & API Reference](PART-4-FRONTEND-AND-API.md)
