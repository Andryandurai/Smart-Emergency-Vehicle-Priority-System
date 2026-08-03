# Part 9 — Future Work · File Changes · Packages · Summary (§22, §24–26)

See [README.md](README.md) for the index.

## Contents

- [§22 Future Improvements](#22-future-improvements)
- [§24 File Change Report](#24-file-change-report)
- [§25 Package Report](#25-package-report)
- [§26 Final Project Summary](#26-final-project-summary)

---

# §22 Future Improvements

Ordered by what would deliver most, soonest. Each entry says what, why, and
what it depends on.

## 22.1 Immediate — the two things never executed

### 1. Build and run the container stack

**Status:** never run. Docker is not installed on the development machine.
**Risk:** low but non-zero. The artifacts are validated by 30 parsing tests
which found two real defects, but a build can still fail on something only a
build reveals — a wheel that needs a compiler, an Alpine/glibc mismatch in the
Node stage, a file the `.dockerignore` excludes that turns out to be needed.

```bash
docker compose build
docker compose up -d
curl -f http://localhost:8000/api/v1/health/ready/
# then the production shape, and a WebSocket smoke test through Nginx:
# a 101 on /ws/ops/, not a 200
```

### 2. Execute the PostgreSQL/PostGIS cutover

**Status:** `migrate_to_postgres` is written and its dump path verified against
fixtures; it has never run against a live server.
**Why it matters:** the spatial tests that exercise `ST_DWithin` and the GiST
indexes are **skipped** on SQLite (3 of them). Until this runs, the PostGIS path
is tested only by construction.

```bash
docker compose up -d db
SEVPS_DB_HOST=127.0.0.1 python manage.py migrate_to_postgres
SEVPS_DB_ENGINE=postgres SEVPS_ENABLE_POSTGIS=1 python manage.py verify_migration
SEVPS_DB_ENGINE=postgres SEVPS_ENABLE_POSTGIS=1 pytest -m spatial
```

## 22.2 Near term

### Scale the worker safely

The single hard scaling limit. Today `replicas: 1` because two workers would
both release a junction and send duplicate commands to real infrastructure.

**Options, in order of preference:**

| Approach | Effort | Notes |
|---|---|---|
| **Postgres advisory lock per tick** | Low | The worker takes a lock before each sweep; a second instance becomes a warm standby. Reuses the pattern already in `entrypoint.sh` |
| Partition by geographic region | Medium | Each worker owns a set of junctions. Needs a stable partition key and rebalancing on failure |
| Leader election (etcd / Redis Redlock) | High | Correct but adds a dependency and a failure mode |

Start with the advisory lock — it converts "must be exactly one" into "one
active, N standby" for a few dozen lines.

### Move push delivery off the request path

Push is synchronous today, bounded by `MAX_FANOUT = 500` and a 6-second HTTP
timeout. Worst case is minutes. A task queue (Celery + Redis, already half
present) would make `publish_and_deliver` enqueue rather than send.

**Only `deliver()` moves.** Everything else — the durable record, audience
resolution, receipts — stays where it is.

### Signal controller authentication

The largest gap before a real municipal integration. The HTTP adapter takes an
endpoint URL and no credentials. A production deployment needs mutual TLS or
signed commands, plus replay protection.

**Blocks:** any real traffic-authority pilot.

### Multi-city tenancy

The `city` field exists and the analytics honour it, but there is no isolation,
no per-city configuration and no cross-city routing. Needed before a second
deployment shares an instance.

## 22.3 AI and ML roadmap

| Model | Task | Data needed | Value |
|---|---|---|---|
| **Demand forecasting** | Predict call volume by cell and hour | 6–12 months of trips | Pre-positioning vehicles — the highest-value model not yet built |
| **Survival-weighted priority** | Predict outcome benefit from arriving N minutes sooner | Linked hospital outcome data | Would turn the priority ladder from a rule into evidence. **Requires outcome linkage the platform does not have** |
| **Corridor benefit estimation** | Predict seconds saved vs cross-traffic seconds cost | Existing pre-emption + hold data | Would let the platform decline low-benefit corridors |
| **Graph neural congestion** | Model spatial propagation of jams | Existing observations | Current per-segment model ignores that a jam spreads upstream |
| **Trajectory prediction** | Learned motion model beating dead reckoning | Telemetry (already collected) | Better projection between fixes |
| **CV: emergency vehicle classifier** | Purpose-trained, not COCO | Labelled frames | Would let CV lead rather than corroborate |
| **CV: signal state recognition** | Read the actual light from a camera | Labelled frames | Closes the loop — verify a pre-emption actually took |

**The principle to preserve:** every one of these must keep a statistical
baseline and must not be given decision authority over a clinical or safety
rule. `EMERGENCY_PRIORITY` is the template — advisory, with disagreement
recorded.

## 22.4 Scalability

| Dimension | Today | Next | Trigger |
|---|---|---|---|
| Web tier | N replicas | Same, autoscaled | > 100 concurrent dashboards |
| Worker | 1 | Advisory-lock standby | Any HA requirement |
| Channel layer | Redis single | Redis Cluster / sharded groups | > 10k concurrent sockets |
| Database | Single PostgreSQL | Read replica for analytics | Analytics queries affecting dispatch latency |
| Telemetry | Row per fix | Partition by month, or TimescaleDB | > 10M rows |
| Routing | In-process graph | Same; the graph is small | > 10⁵ edges |
| Push | Synchronous | Task queue | > 500 subscriptions |

**Telemetry partitioning is the first thing to hit.** At 20 vehicles × 1 Hz ×
8 hours that is ~576k rows per day. Monthly partitions plus a retention policy
is the standard answer.

## 22.5 Microservices — a recommendation against, for now

The natural split lines are visible (routing, CV, notifications), but:

- **The domain is tightly coupled by design.** A position update touches Layers
  1, 2, 3 and 4 in one call. Splitting that into four network hops adds latency
  to the hot path and four new failure modes to a safety-relevant flow.
- **The team is small.** Microservices trade operational complexity for team
  independence. With one team the trade is all cost.
- **The scaling limit is the worker, not the monolith**, and that is a
  concurrency problem, not a decomposition one.

**If a split is ever forced**, the right first extraction is the **CV pipeline**
— it is genuinely independent, CPU-heavy, has a narrow interface
(`frame → Finding[]`), and would benefit from separate GPU scheduling.

## 22.6 Kubernetes

Compose is sufficient for a single-city deployment. Kubernetes becomes worth it
at multi-region, or when the CV tier needs GPU node pools.

**Migration notes for whoever does it:**

| Compose concept | Kubernetes |
|---|---|
| `migrate` service | `initContainer` or a `Job` with a pre-install hook |
| `web` replicas | `Deployment` + `HorizontalPodAutoscaler` |
| `worker` replicas 1 | `Deployment` with `replicas: 1` and `strategy: Recreate` — **not** RollingUpdate, which briefly runs two |
| healthcheck | `livenessProbe` → `/health/live/`, `readinessProbe` → `/health/ready/` (the split already exists for exactly this) |
| `vapid_keys` volume | `Secret` |
| `static_files` | `initContainer` populating an `emptyDir`, or object storage + CDN |

## 22.7 CI/CD

Present: five-job CI. Absent: continuous *delivery*.

**Next steps:** build and push the image on tag → deploy to staging → smoke test
`/health/ready/` → manual gate → production. Plus dependency scanning
(`pip-audit`, `npm audit`) and image scanning (Trivy) — neither is wired up
today.

## 22.8 Monitoring and logging

**The most significant operational gap.** The platform is well tested and
poorly observed.

| Need | Recommendation |
|---|---|
| Metrics | `django-prometheus` + a `/metrics` endpoint. **First four to instrument:** corridor activation success rate, ETA error distribution, push delivery rate, WebSocket connection count |
| Tracing | OpenTelemetry on `on_vehicle_position` — it is the hot path and the one whose latency budget matters |
| Log aggregation | Structured JSON logging to Loki/CloudWatch. The `sevps.*` logger hierarchy already exists |
| Alerting | Alert on: readiness failing, push delivery rate < 0.9, corridor failure rate rising, worker heartbeat missing |
| Error tracking | Sentry, with **PHI scrubbing configured before it is enabled** — trip objects carry patient notes |

That last point is not optional. Wiring Sentry without scrubbing would ship
patient data to a third party.

## 22.9 Product roadmap

| Feature | Value | Depends on |
|---|---|---|
| Native Android driver app | Reaches road users without a browser open | The FCM adapter (already built) |
| CAD integration | SEVPS consumes real dispatch rather than its own trip creation | A CAD partner and a defined interface |
| Hospital EHR handover | Push the assessment into the receiving ED's system | HL7/FHIR mapping |
| Public-facing corridor map | Transparency about where corridors run | A policy decision about disclosure |
| Multi-vehicle convoy | Fire + ambulance + police as one corridor | Contention model extension |
| Historical replay | Reconstruct any incident on the map | Telemetry is already retained |
| What-if simulation | "What if we pre-positioned here?" | Demand forecasting |

---

# §24 File Change Report

## 24.1 A note on git history

The repository has two commits — `836e2b3` (initial) and `0b65f78` ("Changed
framework to django") — and the second contains the entire twelve-phase
migration. **Git cannot therefore attribute changes per phase.** This report is
derived from the migration work itself rather than from `git log`, and is
accurate to what was done.

Untracked at the time of writing: this handover document set.

## 24.2 Summary

| Category | Count |
|---|---|
| Created | ~150 source files (excluding migrations and `__init__.py`) |
| Modified | ~35 existing files |
| Deleted | **0** |
| Moved / renamed | 2 (see §24.6) |
| Total source | **260 files · ~37,500 lines** (excluding `node_modules`, `dist`, migrations) |

**Nothing was deleted.** The brief required that no existing functionality be
removed, and none was. The legacy server-rendered screens, the `/network/segments/`
endpoint, the DRF token endpoint and `apps/network/vision.py` all still work.

## 24.3 Created — backend

| Path | Phase | Why |
|---|---|---|
| `apps/core/roles.py` | 2 | The six-role registry — one source of truth for RBAC |
| `apps/core/permissions.py` | 2 | Every permission class in one reviewable file |
| `apps/core/jwt.py` | 2 | Role claims + httpOnly refresh cookie |
| `apps/core/ws_auth.py` | 2 | JWT over WebSocket (nesting order matters) |
| `apps/core/api_policy.py` | 4 | Machine-checked access policy |
| `apps/core/ws_policy.py` | 4 | Declared socket authorisation |
| `apps/core/consumers.py` | 4 | `GroupConsumer` — sequencing, coalescing, filters |
| `apps/core/notifications.py` | 4 | One vocabulary for "something happened to a person" |
| `apps/core/live.py` | 4 | Worker tick functions |
| `apps/core/spatial.py` | 3 | The dual-backend spatial abstraction |
| `apps/core/migrations/0001_postgis_spatial_columns.py` | 3 | Generated geography columns + GiST |
| `apps/core/management/commands/migrate_to_postgres.py` | 3 | SQLite → PostgreSQL with verification |
| `apps/core/management/commands/verify_migration.py` | 3 | Fingerprint and compare |
| `apps/core/management/commands/backfill_geometry.py` | 3 | LineString columns from JSON |
| `apps/brain/ml/` (4 files) | 6 | Prediction envelope, estimators, services, training |
| `apps/brain/ml_views.py` | 6 | ML API surface |
| `apps/brain/management/commands/train_models.py` | 6 | Gated training |
| `apps/network/cv/` (4 files) | 7 | Detection types, backends, six analysers, pipeline |
| `apps/network/cv_views.py` | 7 | CV API surface |
| `apps/network/gis.py` | 8 | Eleven-layer registry + basemap providers |
| `apps/network/gis_views.py` | 8 | Catalogue, per-layer, basemaps |
| `apps/notify/` (21 files) | 9 | The entire push subsystem |
| `apps/analytics/trends.py` | 10 | Chart-shaped series, profiles, distributions |
| `apps/analytics/exports.py` | 10 | Eight streamed CSV datasets |
| `apps/core/views.py` — `LivenessView`, `ReadinessView` | 11 | Split probes |
| `conftest.py`, `pytest.ini`, `.coveragerc` | 12 | Pytest as runner |
| `apps/core/tests_rbac_matrix.py` | 12 | 306-assertion authorisation sweep |
| `apps/core/tests_deployment.py` | 11 | Parses the compose files so a published port fails CI |

## 24.4 Created — frontend

| Path | Phase | Why |
|---|---|---|
| `frontend/` (whole tree) | 5 | React 19 console replacing nothing — the legacy screens remain |
| `src/api/client.ts` | 5 | Single-flight JWT refresh |
| `src/api/endpoints.ts` · `types.ts` | 5 | Every URL and shape in one place each |
| `src/stores/authStore.ts` · `opsStore.ts` | 5 | Auth and operations state |
| `src/hooks/useSocket.ts` · `usePolling.ts` | 5 | Socket with gap detection; the polling floor |
| `src/components/MapCanvas.tsx` | 5 | Leaflet primitives |
| `src/components/map/` (3 files) | 8 | Declarative GIS layers |
| `src/hooks/useGisLayers.ts` | 8 | Per-layer polling + preferences |
| `src/stores/notifyStore.ts` | 9 | Three sources into one list |
| `src/hooks/usePushNotifications.ts` · `src/lib/push.ts` | 9 | Push registration with typed failure states |
| `src/components/NotificationCentre.tsx` · `NotificationSettings.tsx` | 9 | Bell and preferences |
| `src/components/charts/` (2 files) | 10 | Recharts wrappers + KPI tiles |
| `frontend/e2e/` (4 files) | 12 | Playwright: auth, operations, analytics |
| `frontend/playwright.config.ts` | 12 | Starts Django + Vite, own database |

## 24.5 Created — infrastructure and docs

| Path | Phase | Why |
|---|---|---|
| `static/js/sw.js` | 9 | Push service worker — must be root-scoped |
| `docker/entrypoint.sh` | 11 | Role dispatcher with migration lock |
| `docker/nginx/nginx.conf` · `conf.d/proxy_headers.inc` | 11 | Edge configuration |
| `docker-compose.override.yml` · `docker-compose.prod.yml` | 11 | Local vs production overlays |
| `.github/workflows/ci.yml` | 12 | Five-job CI |
| `docs/*.md` (12 files) | 2–12 | One per subject, written with the phase |
| `docs/handover/*.md` (10 files) | — | This set |

## 24.6 Modified — and why

| File | Change | Phase | Risk |
|---|---|---|---|
| `sevps/settings.py` | JWT, roles, PostGIS, Redis, VAPID, CV, ML, deployment hardening, `SQLITE_PATH` | 2–12 | Every addition is env-gated with a working default |
| `sevps/api_urls.py` | Auth block; `notify/` include; split health probes | 2, 9, 11 | Additive |
| `sevps/asgi.py` | `JWTAuthMiddlewareStack` | 2 | Sockets previously session-only |
| `apps/core/models.py` | `GeoQuerySet.near()` | 3 | Additive |
| `apps/core/apps.py` | `connection_created` → SQLite WAL | 4 | Fixed "database is locked" |
| `apps/core/views.py` | Liveness/readiness added; `HealthView` unchanged | 11 | Existing probes keep working |
| `apps/core/roles.py` | `group_names_for()`; `OPERATIONAL_ROLES` | 9, 12 | The second closed a security defect |
| `apps/core/permissions.py` | `PublicDeviceRegistration`; `IsAuthenticatedRole` now requires an operational role | 9, 12 | **Behavioural** — `public_users` lost operational read |
| `apps/core/notifications.py` | `publish()` also persists and pushes | 9 | Wrapped; return shape unchanged plus two keys |
| `apps/core/api_policy.py` | Allowlist entries; recognises `PublicDeviceRegistration` | 8, 9, 11 | Audit still fails on undeclared endpoints |
| `apps/*/serializers.py` | `ClinicalRedactionMixin` + context passing | 4 | Closed a PHI leak |
| `apps/*/views.py` | Explicit `permission_classes` everywhere | 4 | Closed the "global default" gap |
| `apps/*/consumers.py` | Rebased on `GroupConsumer` + policies | 4 | Closed two socket defects |
| `apps/hospitals/recommender.py` | Tiered `_relax()`; warnings retained | 4 | Fixed a relaxation that erased its own warnings |
| `apps/dispatch/controllers.py` | `_jsonable()` coercion | 4 | Fixed datetime-in-JSONField |
| `apps/dispatch/corridor.py` | `skipped_signal_ids` | 4 | Stopped pre-emption row churn |
| `apps/brain/eta.py` | `ARRIVAL_GRACE_S = 12.0` | 4 | Corridor never activated under sparse telemetry |
| `apps/alerts/dispatcher.py` | Push fan-out for new alerts | 9 | Fail-soft; extra `pushed` key |
| `apps/analytics/views.py` · `urls.py` | Nine chart/export endpoints | 10 | Additive |
| `apps/dashboards/views.py` · `urls.py` | `spa_index`; `/sw.js`; login moved to `/legacy/login/` | 5, 9 | Resolved a `/login` ownership ambiguity |
| `apps/network/vision.py` | Now a facade over `cv/` | 7 | Existing imports keep working |
| `frontend/vite.config.ts` | `charts` chunk; configurable proxy target; `/sw.js` | 9–12 | Build only |
| `frontend/src/app/App.tsx` | Lazy analytics route | 10 | Improves first load everywhere else |
| `frontend/src/pages/OperationsPage.tsx` | GIS layers; notify store; **`useShallow`** | 8, 9, 12 | The last fixed an infinite render loop |
| `frontend/src/pages/AnalyticsPage.tsx` | Charts around the existing tiles; authenticated export download | 10, 12 | Nothing removed |
| `apps/core/tests_auth.py` | Matrix corrected for the `public_users` fix | 12 | Encoded the old defect |
| `apps/notify/tests.py` | Flaky assertion fixed | 10 | Asserted "44" absent from a payload containing a UUID |
| `requirements.txt` | pywebpush, sklearn stack, pytest, PyYAML; optional groups commented with rationale | 6–12 | Base install stays small |
| `.env.example` | Every new variable documented | 2–12 | |
| `.dockerignore` | `node_modules`, `dist` excluded | 11 | Would have added hundreds of MB |
| `Dockerfile` | Node build stage; entrypoint; liveness healthcheck | 11 | Image now builds the console |
| `docker-compose.yml` | `migrate` service; ports removed to the override | 11 | **Secure by default** |

## 24.7 Moved / renamed

| From | To | Why |
|---|---|---|
| Session login at `/login/` | `/legacy/login/` | The React console claims `/login` client-side; the same screen resolved to two different apps depending on a trailing slash |
| `apps/network/vision.py` (implementation) | `apps/network/cv/` (package) | The module reached ~800 lines covering six unrelated detectors. The original path remains as a facade |

## 24.8 Deleted

**None.** Explicitly:

| Kept | Why |
|---|---|
| `templates/` + `static/css/` legacy screens | Fallback for kiosk/embedded displays with no build pipeline |
| `/api/v1/auth/token/` (DRF token) | Field devices cut over on their own schedule |
| `/api/v1/network/segments/geojson/` | Predates the GIS registry; still used by the paramedic and hospital maps |
| `IsOperator` | ~19 call sites; kept as an alias of `IsTrafficPolice` |
| `apps/network/vision.py` | Facade over the new package |
| `SegmentsLayer`, `RouteLine`, `Dot` | Still used outside the ops map |
| Legacy group names (`operators`, `paramedics`, `hospital`) | Aliased, so an existing deployment upgrades without regrouping users |

---

# §25 Package Report

**76 Python packages installed** (including transitive) · **7 npm runtime
dependencies · 13 dev dependencies**.

## 25.1 Python — direct, required

| Package | Version | Needed for | Used by |
|---|---|---|---|
| `Django` | 5.0.9 | Framework, ORM, admin, migrations, auth | Everything |
| `djangorestframework` | 3.15.2 | REST API, serializers, permissions | Every `views.py`, `serializers.py` |
| `channels` | 4.1.0 | WebSocket/ASGI | `sevps/routing.py`, `apps/*/consumers.py` |
| `daphne` | 4.1.2 | ASGI server (HTTP + WS in one process) | `sevps/asgi.py`, Dockerfile |
| `djangorestframework-simplejwt` | 5.3.1 | JWT issuance, refresh, blacklist | `apps/core/jwt.py` |
| `django-cors-headers` | 4.4.0 | CORS in development | `MIDDLEWARE` |
| `python-dotenv` | 1.0.1 | `.env` loading | `sevps/settings.py` |
| `networkx` | 3.3 | Road graph structure and diagnostics | `apps/brain/graph.py` |
| `requests` | 2.32.3 | HTTP signal controllers | `apps/dispatch/controllers.py` |
| `pywebpush` | 2.3.0 | **Web Push — the primary notification transport** | `apps/notify/backends/webpush.py` |
| `cryptography` | 50.0.0 | P-256 keygen, PEM handling for VAPID | `apps/notify/vapid.py` |
| `scikit-learn` | 1.5.2 | Gradient-boosted estimators | `apps/brain/ml/estimators.py` |
| `numpy` | 2.1.3 | Feature vectors | `apps/brain/ml`, `apps/network/cv` |
| `joblib` | 1.4.2 | Model persistence | `models/*.joblib` |
| `shap` | 0.46.0 | Per-prediction attribution | `apps/brain/ml/base.py` |
| `psycopg[binary]` | 3.2.1 | PostgreSQL driver | Production; installed in the image |
| `PyYAML` | 6.0.3 | Parses the compose files in tests | `apps/core/tests_deployment.py` |
| `pytest` | 9.1.1 | Test runner | Whole suite |
| `pytest-django` | 4.12.0 | Django integration | `pytest.ini`, `conftest.py` |
| `pytest-cov` | 7.1.0 | Coverage | `.coveragerc` |

## 25.2 Python — optional, commented in `requirements.txt`

| Package | Enables | Without it |
|---|---|---|
| `channels-redis` 4.2.0 | Cross-process WebSocket fan-out | In-memory layer; console polls as a fallback |
| `opencv-python-headless` 4.10 | Frame decode for CV | Simulated detections |
| `ultralytics` 8.2.79 | YOLOv8 inference | Simulated detections |
| `firebase-admin` 6.5.0 | FCM for native Android | Web Push only — **the normal case** |
| `celery` 5.4.0 + `redis` 5.0.8 | Future task queue | Not used today |

**Why these are commented rather than pinned:** a laptop pilot on SQLite with
the in-memory channel layer needs none of them, and requiring them would make a
plain `pip install -r requirements.txt` fail without a Postgres toolchain or a
600 MB torch download.

## 25.3 Python — transitive

| Package | Pulled in by | Purpose |
|---|---|---|
| `asgiref` 3.12.1 | Django, Channels | Sync/async bridging — also used directly for `database_sync_to_async` |
| `sqlparse` 0.5.5 | Django | SQL formatting |
| `PyJWT` 2.13.0 | SimpleJWT | JWT primitives |
| `py-vapid` 1.9.4 | pywebpush | VAPID JWT signing |
| `http-ece` 1.2.1 | pywebpush | RFC 8291 payload encryption |
| `certifi` 2026.7.22 | requests, pywebpush | CA bundle |
| `scipy` 1.17.1 | scikit-learn, SHAP | Scientific routines |
| `numba` 0.66.0 / `llvmlite` 0.48.0 | SHAP | JIT for explainer speed |
| `cloudpickle` 3.1.2 | SHAP | Serialisation |
| `slicer` 0.0.8 | SHAP | Array slicing |
| `tqdm` 4.70.0 | SHAP, sklearn | Progress bars |
| `packaging` 26.2 | many | Version handling |
| `pytest-xdist` 3.8.0 | (installed, optional) | Parallel test execution |

## 25.4 npm — runtime (7)

| Package | Version | Needed for | Used by |
|---|---|---|---|
| `react` | ^19.0.0 | UI runtime | Everything |
| `react-dom` | ^19.0.0 | DOM renderer | `main.tsx` |
| `react-router-dom` | ^7.1.1 | Routing, guards, deep links | `app/App.tsx`, `RequireAuth.tsx` |
| `zustand` | ^5.0.2 | State management | `stores/*` |
| `leaflet` | ^1.9.4 | Map engine | `components/MapCanvas.tsx` |
| `react-leaflet` | ^5.0.0 | React bindings for Leaflet | Same, `map/GisLayer.tsx` |
| `recharts` | ^3.10.1 | Charts | `charts/primitives.tsx` — **lazy-loaded** |

**Seven runtime dependencies is deliberate.** Every one earns its place; there
is no UI component library, no date library, no HTTP client (the platform uses
`fetch`), and no datagrid.

## 25.5 npm — development (13)

| Package | Purpose |
|---|---|
| `typescript` ^5.7.2 | Type system |
| `vite` ^6.0.5 | Build tool and dev server |
| `@vitejs/plugin-react` ^4.3.4 | JSX transform, Fast Refresh |
| `vitest` ^2.1.8 | Unit test runner |
| `jsdom` ^25.0.1 | DOM for Vitest |
| `@testing-library/react` ^16.1.0 | Component testing |
| `@testing-library/dom` ^10.4.0 | Query utilities |
| `@testing-library/jest-dom` ^6.6.3 | DOM matchers |
| `@playwright/test` ^1.62.1 | End-to-end |
| `@types/react` ^19.0.2 | React types |
| `@types/react-dom` ^19.0.2 | DOM types |
| `@types/leaflet` ^1.9.15 | Leaflet types |
| `@types/node` ^22.20.1 | Node types for the config files |

## 25.6 Container images

| Image | Used for |
|---|---|
| `python:3.11-slim` | Application base and runtime |
| `node:22-alpine` | Frontend build stage (not shipped) |
| `postgis/postgis:16-3.4` | Database — **not plain postgres**, so a missing extension fails at migrate time rather than silently running without spatial indexes |
| `redis:7-alpine` | Channel layer |
| `nginx:1.27-alpine` | Edge |

---

# §26 Final Project Summary

## 26.1 Executive summary

**SEVPS is a working emergency-vehicle priority platform.** It tracks emergency
vehicles in real time, predicts their arrival against actual traffic, clears
traffic signals ahead of them, warns the road users about to be in the way,
routes patients to hospitals that can actually treat them, and measures whether
any of it is working.

It was built to a six-layer specification and then modernised through twelve
phases onto a current stack — React 19, JWT with six-role RBAC, PostgreSQL/
PostGIS, scikit-learn with SHAP explanations, YOLOv8 computer vision, Leaflet
GIS, Web Push notifications, Recharts analytics, containerised deployment, and a
four-layer test suite.

**Scale.** 260 source files, ~37,500 lines. 155 API endpoints, 5 WebSocket
consumers, 26 database tables, 11 GIS layers, 5 ML estimators, 6 CV detectors,
14 management commands.

**Quality.** 721 backend tests + 123 subtests, 33 frontend unit tests, 34
end-to-end tests. Type checking clean. Production build clean. Two
machine-checked access-policy audits that fail the build rather than trusting
review.

**What the work actually found.** Testing and probing a running server, rather
than reading code, uncovered **five security defects** — patient data readable
anonymously over REST and again over WebSocket, unauthenticated commands to
traffic-signal controllers, hospital staff locked out of their own hospital, and
a citizen role able to read live ambulance positions. It also found a
product-breaking infinite render loop, a CSV export that returned 401 for every
user, a machine-learning evaluation comparing error on different rows, and two
deployment defects that would have failed a `docker compose up`. Every one now
has a regression test.

**Honest status.** Two things have never been executed because they need
software that was not installed and installing it was not authorised: the Docker
build, and the PostgreSQL/PostGIS cutover. Both are first on the roadmap.

## 26.2 Technical summary

| Layer | Implementation |
|---|---|
| **Frontend** | React 19 + TypeScript 5.7 (`strict`, `noUncheckedIndexedAccess`), Vite 6, React Router 7, Zustand 5, Leaflet + react-leaflet, Recharts (lazy). 256 kB main chunk |
| **API** | Django 5.0 + DRF 3.15 under Daphne (ASGI). 155 endpoints, every one with an explicit, audited permission class |
| **Realtime** | Django Channels 4.1, five consumers, sequenced and coalesced frames, group fan-out, in-memory → Redis by config |
| **Auth** | SimpleJWT. Access token in memory (15 min), refresh in an httpOnly path-scoped cookie (7 days), rotation + blacklist |
| **RBAC** | Six roles with legacy aliases, three capability axes, clinical redaction at the serializer, 306 parametrised assertions |
| **Database** | SQLite (WAL) → PostgreSQL + PostGIS. Generated `geography` columns + GiST, **no GDAL required** |
| **Routing** | Custom time-dependent A*/Dijkstra over a NetworkX graph with a two-tier cache (60 s topology, 5 s state) |
| **AI** | Five scikit-learn estimators, each with a statistical baseline, SHAP attribution, gated deployment (+5 % over baseline) |
| **CV** | Six detectors over YOLOv8 or simulated frames, Greenshields speed inference, evidence-carrying findings |
| **GIS** | Server-owned 11-layer registry, per-layer permissions and refresh, heat surfaces without a plugin |
| **Notifications** | Web Push (VAPID) primary + optional FCM adapter, durable history, delivery receipts, role-resolved audience |
| **Analytics** | Daily rollups, contiguous trend series, demand profiles, response distributions, 8 CSV exports |
| **Deployment** | 4-stage Docker image that builds the console, 3 compose files (secure by default), Nginx edge, split health probes |
| **Testing** | pytest (runner, not rewrite) + Vitest + Playwright, five-job CI |

### The ten decisions that most shape the codebase

1. **Time-dependent routing with a custom search** — because `networkx.astar_path` cannot see accumulated time.
2. **PostGIS through generated columns, not GeoDjango fields** — so GDAL is not a dependency anywhere.
3. **Access token in memory, refresh in an httpOnly cookie** — an XSS cannot exfiltrate a durable credential.
4. **Machine-checked access policy** — making an endpoint public requires an allowlist edit with a written reason.
5. **Fail-closed clinical redaction** — no serializer context means redact.
6. **Polling alongside every WebSocket** — the socket is latency, the poll is correctness.
7. **A model advises; a rule decides** — `EMERGENCY_PRIORITY` is advisory and records disagreement.
8. **Interpretable hospital scoring, not a learned model** — a clinician must be able to argue with it.
9. **Compose that is secure by default** — a forgotten overlay fails closed, not open.
10. **`toLatLng()` is the only coordinate flip** — because a swap renders an empty map, not a visibly wrong one.

## 26.3 Developer handover guide

### Day one

```bash
pip install -r requirements.txt
python manage.py migrate && python manage.py seed_users && python manage.py seed_demo
python manage.py runserver                        # http://127.0.0.1:8000/
cd frontend && npm install && npm run dev         # http://127.0.0.1:5173/
python manage.py simulate --trips 3               # make something happen
```

Sign in as `police` / `sevps-police`. Watch the Operations console.

### Reading order

1. `docs/handover/PART-1` §2 — the architecture.
2. `apps/core/roles.py` and `permissions.py` — the authorisation spine.
3. `apps/dispatch/orchestrator.py` — `on_vehicle_position()` is the hot path and the best single view of how the layers compose.
4. `apps/brain/router.py` — `_search()` is the heart of Layer 2.
5. `frontend/src/pages/OperationsPage.tsx` — how the client consumes all of it.
6. `docs/handover/PART-7` §16 — the walkthrough, once the above is familiar.

### Rules to work by

| Rule | Why |
|---|---|
| **Views orchestrate, services decide** | It is why the simulator can drive the platform without HTTP |
| **Declare `permission_classes` explicitly** | The audit fails the build otherwise |
| **Import `Role.X`, never the string** | Legacy aliases resolve only through the registry |
| **Pass serializer context** | Redaction fails closed; forgetting it hides data, not leaks it — but the omission is a bug |
| **`useShallow` on array selectors** | The alternative is an infinite render loop |
| **`toLatLng()` is the only flip** | |
| **Fan-out is fail-soft** | Never abort a dispatch decision for a broadcast |
| **Run `pytest -m rbac` before a permission change** | It names the exact cell that changed |

### Where things live

| Looking for | Go to |
|---|---|
| A tunable constant | `settings.SEVPS` dict |
| A permission decision | `apps/core/permissions.py` + `api_policy.py` |
| A domain rule | `apps/<app>/<domain>.py`, never `views.py` |
| An API URL used by the client | `frontend/src/api/endpoints.ts` |
| A WebSocket event's origin | grep `broadcast(` / `broadcast_ops(` |
| Why a decision was made | The module docstring — that is where the reasoning is |

## 26.4 Maintenance guide

### Routine

| Task | Frequency | Command |
|---|---|---|
| Daily metric rollup | Nightly | `manage.py shell -c "from apps.analytics.services import rollup_daily_metrics; rollup_daily_metrics()"` |
| Accident hotspot recompute | Weekly | `POST /api/v1/analytics/accident-hotspots/recompute/` |
| Traffic profile learning | Weekly | `manage.py learn_traffic_profiles` |
| Push subscription sweep | Monthly | `manage.py push_sweep --days 60` |
| Model retraining | Monthly / on drift | `manage.py train_models` |
| Dependency review | Quarterly | `pip list --outdated`, `npm outdated` |

**The worker must run continuously and exactly once.** If it stops, signal holds
are never released — the single most operationally significant failure mode.

### Diagnostics

| Symptom | First check |
|---|---|
| Dashboards stale but data correct | `/api/v1/info/` → `channel_layer`. In-memory means worker events do not reach the browser |
| Corridors not activating | `sevps_worker` running? `/dispatch/preemptions/?open=1` state field |
| Push not arriving | `/api/v1/notify/health/` → `webpush_configured`, `delivery_rate` |
| Routes look wrong | `/brain/network/summary/` → graph node/edge counts; `POST /brain/network/rebuild/` |
| Everything slow on SQLite | Check WAL is on; consider the PostgreSQL cutover |
| Charts empty | `/analytics/trends/` → `computed_live_days` high means rollups are not running |
| Analytics ≠ dashboard | Window mismatch — exports carry `# window:` for exactly this |

### Modifying safely

| Change | Do this |
|---|---|
| Add an endpoint | Declare `permission_classes`; if public, add to `PUBLIC_READ_ENDPOINTS` with a reason; add to the RBAC matrix |
| Add a role | One `RoleSpec` in `roles.py`; derived sets update automatically; extend the matrix |
| Add a GIS layer | One `LayerSpec` + builder in `gis.py`; the client needs no change |
| Add a metric | One `SeriesSpec` in `trends.py`; **set `higher_is_better` explicitly**, `null` for demand |
| Add an ML model | Subclass `SEVPSEstimator`; **provide a baseline**; register it |
| Change a model | Write the migration; CI's `makemigrations --check` catches omissions |
| Change a permission | Run `pytest -m rbac` first — it names the cell |

## 26.5 Deployment checklist

### Pre-deployment

- [ ] `pytest` green · `npx vitest run` green · `npx playwright test` green
- [ ] `npx tsc --noEmit` clean · `npx vite build` clean
- [ ] `manage.py makemigrations --check --dry-run` → no changes
- [ ] `SEVPS_DEBUG=0 manage.py check --deploy` → only the HSTS-preload warning
- [ ] `.env` created; **`SEVPS_SECRET_KEY` is not the development key**
- [ ] `SEVPS_ALLOWED_HOSTS` set to real hostnames
- [ ] `SEVPS_VAPID_PRIVATE_KEY` and `SEVPS_VAPID_SUBJECT` set — **a real contactable URI**
- [ ] Database password set and not the default
- [ ] TLS certificate in place; `SEVPS_BEHIND_TLS_PROXY=1`
- [ ] `SEVPS_SECURE_SSL_REDIRECT=0` if the edge already redirects
- [ ] Cloud LB idle timeout raised above 3600 s for `/ws/`
- [ ] `seed_users` demo passwords changed or the accounts removed

### Deploy

- [ ] `docker compose -f docker-compose.yml -f docker-compose.prod.yml build`
- [ ] `... up -d`
- [ ] `docker compose logs migrate` → migrations completed
- [ ] `curl -f https://<host>/api/v1/health/ready/` → `ready`
- [ ] `curl -I https://<host>/sw.js` → `Service-Worker-Allowed: /`
- [ ] WebSocket smoke test: **101**, not 200, on `/ws/ops/`
- [ ] Sign in as each role; confirm the navigation matches the matrix
- [ ] `/api/v1/notify/health/` → `webpush_configured: true`
- [ ] `/api/v1/info/` → `channel_layer` is Redis, `database` is postgresql

### Post-deployment

- [ ] Worker running and exactly one instance
- [ ] Nightly rollup scheduled
- [ ] `push_sweep` scheduled
- [ ] Backups configured for `postgres_data`
- [ ] **`SEVPS_VAPID_PRIVATE_KEY` backed up** — losing it invalidates every subscription
- [ ] Monitoring on: readiness, worker heartbeat, push delivery rate, corridor failure rate
- [ ] Runbook for: worker down, Redis down, controller unreachable

## 26.6 Future roadmap

| Horizon | Item | Value | Effort |
|---|---|---|---|
| **Now** | Build and run the container stack | Validates 11 phases of deployment work | Low |
| **Now** | Execute the PostGIS cutover | Un-skips 3 spatial tests; proves the production path | Low |
| **Now** | Monitoring and structured logging | The platform is well tested and poorly observed | Medium |
| **Next** | Worker HA via advisory lock | Removes the only hard scaling limit | Low |
| **Next** | Push delivery on a task queue | Removes a synchronous bound from the request path | Medium |
| **Next** | Signal controller authentication | **Blocks any real municipal integration** | Medium |
| **Next** | Sentry with PHI scrubbing | Error visibility without a data breach | Low |
| **Later** | Demand forecasting model | Pre-positioning — the highest-value model not yet built | Medium |
| **Later** | Multi-city tenancy | A second deployment on one instance | High |
| **Later** | Native Android driver app | The FCM adapter already exists | High |
| **Later** | CAD integration | SEVPS consumes real dispatch | High |
| **Watch** | Kubernetes | Only at multi-region or GPU scheduling | High |
| **Watch** | Microservices | **Recommended against today** — see §22.5 | High |

---

## Closing note

The most useful thing in this codebase is not any single module — it is that
**the reasoning is written down next to the code**. Nearly every non-obvious
decision carries a comment explaining what would go wrong if it were done the
other way: why the middleware nesting order matters, why liveness checks
nothing, why `useShallow` is required, why the base compose file publishes no
ports, why a model advises and a rule decides.

Those comments are the real handover. When you change something and a test
fails, read the docstring above it before you change the test — it will usually
tell you which of the two is wrong.

---

**Previous:** [Part 8 — Deployment, Testing, Performance, Security](PART-8-DEPLOYMENT-TESTING-PERFORMANCE-SECURITY.md)
**Index:** [README.md](README.md)
