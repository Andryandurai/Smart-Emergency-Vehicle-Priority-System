# Part 8 — Deployment · Testing · Performance · Security (§18–21)

See [README.md](README.md) for the index.

## Contents

- [§18 Deployment](#18-deployment)
- [§19 Testing](#19-testing)
- [§20 Performance](#20-performance)
- [§21 Security](#21-security)

---

# §18 Deployment

## 18.1 Artifacts

```
Dockerfile                      4-stage build
docker/entrypoint.sh            role dispatcher: web | worker | migrate | simulate
docker/nginx/nginx.conf         edge configuration
docker/nginx/conf.d/proxy_headers.inc
docker-compose.yml              base — no host ports
docker-compose.override.yml     local — auto-loaded, publishes ports
docker-compose.prod.yml         production — nginx, DEBUG off, replicas
.github/workflows/ci.yml        five jobs
```

> **Not yet executed.** Docker is not installed on the development machine and
> installing it was not authorised. `docker build` and `docker compose up` have
> **never been run**. What *has* run: 30 automated tests that parse these files
> and assert their structure, `sh -n` on the entrypoint, and
> `manage.py check --deploy` under production settings. That found two real
> defects (§18.8). Treat the first real `docker compose up` as the outstanding
> verification step.

## 18.2 The Docker image

Four stages, each with a reason:

| Stage | Base | Produces |
|---|---|---|
| `frontend` | `node:22-alpine` | `npm ci && npm run build` → `dist/` |
| `base` | `python:3.11-slim` | `libpq5`, `curl`. **No GDAL** |
| `deps` | `base` + build tools | Python wheels into `/install` |
| `runtime` | `base` | The shipped image |

**The image builds the console itself.** The previous Dockerfile did `COPY . .`
and relied on `frontend/dist` existing on the host — shipping whatever a
developer last built, i.e. last week's UI against this week's API, with nothing
visible from outside to say so. `npm run build` is `tsc --noEmit && vite build`,
so **a type error fails the image build** rather than failing in a browser.

**Runtime hardening:**

```dockerfile
RUN useradd --create-home --uid 10001 sevps
USER sevps
HEALTHCHECK CMD curl -fsS http://127.0.0.1:8000/api/v1/health/live/ || exit 1
```

Non-root because this container terminates traffic-signal commands, and a
compromise should not also own the filesystem.

**The healthcheck uses liveness, not readiness.** `/health/ready/` checks the
database; using it here would have Docker restart a perfectly healthy
application every time Postgres blinked — which cannot fix anything and makes
recovery slower.

## 18.3 The entrypoint

| Role | Does |
|---|---|
| `web` | wait for DB → migrate (advisory lock) → collectstatic → warn if no VAPID → `daphne --proxy-headers` |
| `worker` | wait for DB → `manage.py sevps_worker`. **Never migrates** |
| `migrate` | wait for DB → migrate → collectstatic → exit |
| `simulate` | wait for DB → `manage.py simulate` |
| anything else | passed through |

```sh
LOCK_KEY = 87310219
cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
try:    call_command("migrate", interactive=False)
finally: cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
```

Django has no internal migration lock; concurrent replicas race and can
half-apply. The second replica blocks, then finds everything applied and does
nothing.

`wait_for_db` retries for `SEVPS_DB_WAIT_ATTEMPTS × 2` seconds (default 120 s).
Compose healthchecks cover the local case; this covers managed databases (RDS,
Cloud SQL) where a failover can remove the endpoint for tens of seconds
mid-deploy.

## 18.4 Compose — secure by default

| File | Loaded when | Contains |
|---|---|---|
| `docker-compose.yml` | always | Services, volumes, dependencies. **No host ports** |
| `docker-compose.override.yml` | automatically on bare `up` | Host port publishing, `DEBUG=1` |
| `docker-compose.prod.yml` | only when named with `-f` | Nginx, `DEBUG=0`, required secrets, replicas, limits |

**Why the base file publishes nothing.** Compose skips the auto-override
whenever files are named explicitly. The obvious arrangement — ports in the
base, an overlay that removes them — fails *open*: a deploy that forgets
`-f docker-compose.prod.yml` puts an emergency service's PostgreSQL on a public
interface. This way the same mistake produces a stack that is simply
unreachable. **A visible outage beats a silent exposure.**

It also avoids the `!override` YAML tag, which needs Compose ≥ 2.24.

```bash
docker compose up --build                                              # local
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d   # production
```

**Production requires its secrets:**

```yaml
SEVPS_SECRET_KEY: ${SEVPS_SECRET_KEY:?set SEVPS_SECRET_KEY in .env}
SEVPS_VAPID_PRIVATE_KEY: ${SEVPS_VAPID_PRIVATE_KEY:?... or push alerts will not deliver}
```

`${VAR:?message}` fails the deploy with a readable error rather than silently
booting on `dev-insecure-key-do-not-use-in-production`.

## 18.5 Nginx

The WebSocket block is the part most likely to be got wrong, and the failure is
quiet:

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

location /ws/ {
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 3600s;
    proxy_buffering off;
}
```

Without the two headers the handshake returns **200 instead of 101**, the
browser falls back to polling, and the console still works — just seconds
behind, with nothing on screen to say so. And `proxy_read_timeout` must exceed
the 60-second default, because a dashboard holds one socket open all shift.

### Rate limiting

| Zone | Rate | Why |
|---|---|---|
| `auth` | 12 r/**min** | Credential stuffing. A real user signs in once |
| `api` | 60 r/**sec** | Backstop only. The console legitimately polls every 2–3 s |
| health | none | A probe tripping the limiter takes a healthy instance out of the pool — the limiter causing the outage it exists to prevent |

**Telemetry is deliberately not rate-limited.** Twenty ambulances at 1 Hz behind
one control-room NAT during a mass-casualty incident is exactly the pattern a
naive per-IP limit mistakes for an attack — at the moment the system matters
most.

### Caching

Vite emits content-hashed filenames, so `/static/` is `immutable` for a year.
`index.html` is **not** hashed and is `no-store`: cache it and a browser keeps
requesting chunk names that no longer exist after a deploy, and the app 404s its
own JavaScript. `/sw.js` is `no-cache` for the same class of reason.

### Security headers

`X-Content-Type-Options: nosniff` · `X-Frame-Options: DENY` ·
`Referrer-Policy: strict-origin-when-cross-origin` ·
`Permissions-Policy: geolocation=(self), camera=(), microphone=(), payment=()` ·
a CSP allowing self, tile hosts, and `style-src 'unsafe-inline'` (a real
concession to Leaflet's runtime styling; `script-src` stays strict).

## 18.6 Environment variables

Complete list in `.env.example`. The ones that change behaviour materially:

| Variable | Default | Effect |
|---|---|---|
| `SEVPS_DEBUG` | `1` | **`0` applies the entire security block** |
| `SEVPS_SECRET_KEY` | dev key | `ImproperlyConfigured` if the dev key survives into production |
| `SEVPS_ALLOWED_HOSTS` | `*` | Host header validation |
| `SEVPS_DB_ENGINE` | `sqlite` | `postgres` switches backend |
| `SEVPS_ENABLE_POSTGIS` | `0` | Generated columns + GiST |
| `SEVPS_SQLITE_PATH` | `sevps.sqlite3` | Lets the E2E suite use its own file |
| `SEVPS_REDIS_URL` | — | Empty = in-memory channel layer |
| `SEVPS_BEHIND_TLS_PROXY` | `1` | Trust `X-Forwarded-Proto` |
| `SEVPS_SECURE_SSL_REDIRECT` | `1` | Set `0` when the edge already redirects |
| `SEVPS_HSTS_PRELOAD` | `0` | **Off on purpose** — see below |
| `SEVPS_VAPID_PRIVATE_KEY` / `_SUBJECT` | — | Web Push |
| `SEVPS_CV_MODE` | `simulated` | `yolo` for real inference |
| `SEVPS_MAPBOX_TOKEN` | — | Adds Mapbox styles + traffic tiles |
| `SEVPS_WEB_REPLICAS` | `2` | Compose only |

### HSTS preload is off deliberately

`manage.py check --deploy` warns about it, and the warning is expected.
Preload submission is close to irreversible: removal takes months to propagate
through browser releases, and until it does **every subdomain is unreachable
over plain HTTP**. A municipal deployment may still have a roadside display
controller or a legacy signal bridge on HTTP — exactly the equipment this
platform exists to drive.

## 18.7 Deployment process

```bash
git pull
cp .env.example .env && $EDITOR .env       # first time only
python manage.py generate_vapid_keys       # first time only — see below
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose logs -f migrate             # confirm migrations completed
curl -f https://<host>/api/v1/health/ready/
```

**State that must survive a redeploy:**

| Item | Consequence of losing it |
|---|---|
| `SEVPS_SECRET_KEY` | Every session and CSRF token invalidated |
| **`SEVPS_VAPID_PRIVATE_KEY`** | **Every push subscription invalidated** — everyone must re-grant permission and finds out only when an alert fails to arrive |
| `postgres_data` | Everything |
| `media_files` | Uploaded camera frames |

## 18.8 Cloud architecture

The compose stack is the portable unit. Below is the mapping and the specific
gotchas — **not IaC**, because untested Terraform for three providers is a
liability rather than a deliverable.

| Piece | AWS | Azure | GCP |
|---|---|---|---|
| Images | ECR | ACR | Artifact Registry |
| Containers | ECS Fargate / EKS | Container Apps / AKS | Cloud Run / GKE |
| Database | RDS PostgreSQL + PostGIS | PostgreSQL Flexible Server | Cloud SQL |
| Channel layer | ElastiCache Redis | Azure Cache for Redis | Memorystore |
| Edge | ALB (+ CloudFront) | Application Gateway / Front Door | HTTPS LB (+ Cloud CDN) |
| Secrets | Secrets Manager | Key Vault | Secret Manager |

### The gotcha that bites every cloud

**Every provider's default load-balancer idle timeout is shorter than a shift**,
and the failure presents as flaky sockets rather than a configuration problem:

- **AWS ALB** — 60 s default. Raise `idle_timeout.timeout_seconds` (max 4000).
- **Azure Application Gateway** — 4 min default; ensure WebSocket support (v2 SKU).
- **GCP HTTPS LB** — backend `timeoutSec` defaults to 30 s and applies to
  WebSockets as maximum connection duration.

Nginx already sets 3600 s; the cloud LB in front must agree or it caps whatever
Nginx allows.

### Other per-cloud notes

**PostGIS** is supported on RDS and Cloud SQL but not enabled by default —
`CREATE EXTENSION postgis;` must run once, which the SEVPS migration does. Azure
Flexible Server requires PostGIS in `azure.extensions` first.

**Cloud Run is wrong for the worker.** It scales to zero and does not guarantee
a single always-on instance, and the corridor safety sweep must run
continuously. Run `web` on Cloud Run if you like; run `worker` on GKE, a VM, or
an always-on service with `min-instances=1, max-instances=1`.

**Fargate + the worker:** set `desiredCount: 1` **and**
`deploymentConfiguration.maximumPercent: 100` — the default 200 starts a
replacement task before draining the old one, which is precisely the
duplicate-worker situation the replica pin exists to prevent.

**Redis is not optional above one process.** With the in-memory layer, an event
raised by the worker never reaches a dashboard on the web tier. The console
appears to work — it polls as a fallback — and is simply always stale.

## 18.9 What the static checks caught

Two real defects, both found by parsing rather than review:

1. **A YAML syntax error.** `${SEVPS_VAPID_SUBJECT:?... mailto: or https: URI}`
   — the unquoted colons made the file unparseable. Compose would have failed to
   start the stack.
2. **`node_modules` in the build context.** `.dockerignore` excluded the SQLite
   database but not `frontend/node_modules` or `frontend/dist`, so `COPY . .`
   would have copied hundreds of megabytes in and could have shadowed the
   freshly built console with a stale host build.

Both are now asserted by tests in `apps/core/tests_deployment.py`.

---

# §19 Testing

## 19.1 Four layers

| Layer | Count | Runner | Catches what nothing below it can |
|---|---|---|---|
| Django unit / integration | 418 | pytest or `manage.py test` | Business logic: corridor timing, hospital scoring, redaction |
| RBAC matrix | 306 | `pytest -m rbac` | Authorisation as a whole-product sweep |
| Vitest | 33 | `npx vitest run` | Pure frontend logic: formatting, store reconciliation, base64 |
| Playwright | 34 | `npx playwright test` | The seams — sockets, auth round trips, charts that render nothing |

```bash
pytest                          # 721 tests + 123 subtests, ~25 s
pytest -m rbac                  # 306, ~8 s
python manage.py test apps      # 418, still works
cd frontend && npx vitest run
cd frontend && npx playwright test
```

## 19.2 Pytest as runner, not rewrite

All 418 existing `TestCase` classes were collected and passed on the first run
with no edits. A migration requiring 418 file changes would have risked more
than it gained and produced a diff large enough to hide a real behavioural
change.

**Both runners are kept working and CI runs both.** `manage.py test` is what a
municipal operator with no pytest install reaches for, and a suite that only
runs one way quietly stops running the other.

What pytest adds:

- **Parametrisation over a matrix** — the RBAC sweep is 6 roles × ~40 endpoints,
  expressed once. In unittest it is a nested loop where the first failure hides
  the rest.
- **Fixtures composed by dependency** — a test needing a junction, an ambulance
  and a hospital asks for three fixtures rather than inheriting a base class
  that builds those plus eight it does not need.
- **Markers** — `smoke`, `rbac`, `slow`, `spatial`, `ml`.

**`--reuse-db` is deliberately not the default.** SEVPS migrations create
PostGIS generated columns and GiST indexes; a stale reused database silently
skips them and the spatial tests then pass against the wrong schema.

**One fixture worth knowing about:**

```python
@pytest.fixture(autouse=True, scope="session")
def _fast_password_hashing():
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
```

PBKDF2 does ~600,000 iterations — right in production, ruinous in a suite
creating seven users per test. The RBAC matrix took **over 20 minutes** before
and **20 seconds** after.

## 19.3 The RBAC matrix

`apps/core/tests_rbac_matrix.py` is the centrepiece. Authorisation regresses
*silently*: a serializer change or a new mixin can quietly open an endpoint and
nothing fails — the feature still works, it just also works for people it should
not.

```
PUBLIC_GETS            16 URLs × 8 callers
AUTHENTICATED_GETS     19 URLs
ADMIN_ONLY_GETS         1 URL
WRITE_CASES             5 actions × 8 callers
CLINICAL_FIELDS         redaction per role
```

`outcome()` deliberately does **not** fold 404 and 405 into "denied". An
endpoint that 404s leaks less than one that 403s, but it is also not what the
policy says — and treating them as equivalent means a typo'd URL passes every
denial assertion.

## 19.4 What testing found

| Layer | Defect | Severity |
|---|---|---|
| RBAC matrix | `public_users` could read live ambulance positions, trips, routes and all analytics | **Security** |
| Playwright | Infinite render loop on the Operations console — **blank screen** | **Product-breaking** |
| Playwright | Every CSV export returned 401 — a browser navigation carries no bearer token | Functional |
| Deployment tests | YAML syntax error in the prod compose file | Deploy-blocking |
| Deployment tests | `node_modules` in the Docker build context | Build bloat |
| ML tests | Train/test split compared error on **different rows** | Silent — every model looked excellent |

Three Playwright failures were **the tests being wrong about the app**, and the
tests were fixed rather than the code: the ops console has no page heading by
design, the event log is legitimately empty (and so zero-height), and tile
requests fire before a listener can attach.

## 19.5 Coverage

```bash
pytest --cov=apps --cov-report=term-missing --cov-report=html
```

**No `fail_under` threshold, deliberately.** A number that must be met gets met
by covering whatever is cheapest — serializers and `__str__` methods — while
corridor timing arithmetic and redaction rules stay untested because they are
harder. Coverage here is a map of what has not been looked at, not a gate.

## 19.6 CI

Ordered cheapest-first so a broken build fails in seconds rather than after a
browser download.

| Job | Runs |
|---|---|
| `checks` | `manage.py check`, `check --deploy` under `DEBUG=0`, `makemigrations --check` |
| `rbac` | `pytest -m rbac` — split out and early |
| `backend` | `pytest --cov` **and** `manage.py test apps` |
| `frontend` | `tsc --noEmit`, `vitest run`, `vite build` |
| `e2e` | Playwright; needs backend + frontend |

`makemigrations --check` earns its place: a model change without a migration
passes every test and fails on the first deploy.

Playwright retries twice in CI and **zero times locally**. A test that only
passes on retry is flaky, and a flaky E2E suite gets ignored — worse than not
having one.

---

# §20 Performance

## 20.1 Caching

| Cache | TTL | Rationale |
|---|---|---|
| Graph topology | 60 s | Nodes and edges rarely change; rebuilding is hundreds of ms |
| Live edge state | 5 s | Speeds and closures change constantly; rebuilding is one query |
| `postgis_available()` | process lifetime | A capability, not a state |
| ML models | process lifetime | joblib load is expensive; the file does not change under a running process |
| Static assets | 1 year, immutable | Content-hashed filenames |
| `index.html` / `sw.js` | **no-store / no-cache** | Unhashed; a stale copy requests chunks that no longer exist |
| Layer preferences | localStorage | User preference, not server state |

**Why two graph tiers rather than one** is the single most important caching
decision: at a single TTL you either pay topology cost every five seconds or
route on minute-old congestion.

## 20.2 Database optimisation

**Index-first design.** Every index in §5.5 exists for a named query, not
speculatively.

**The bounding-box prefilter** is the SQLite spatial strategy:

```python
queryset.filter(latitude__range=..., longitude__range=...)   # index scan
# then haversine in Python over what survives
```

Without it, "hospitals within 25 km" is a full scan plus a haversine per row.

**`select_related` / `only`** on the hot paths — the trip list joins vehicle and
hospital in one query; the analytics loops use `.only()` to avoid loading
clinical text they never read.

**PostgreSQL tuning** in the production overlay:

```yaml
-c max_wal_size=4GB          # avoid checkpoint stalls during a surge
-c shared_buffers=1GB
-c synchronous_commit=off    # see below
```

`synchronous_commit=off` trades a small window of durable writes for a large
drop in fsync latency. Correct here because emergency telemetry is append-heavy
and every row is superseded by the next GPS fix a second later. **It would be
wrong for a bank's ledger**, and dispatch decisions themselves are ordinary
transactions unaffected by it.

**SQLite WAL** lets readers proceed during a write. Without it the simulator and
the web process fight over the file.

## 20.3 Query optimisation in the hot path

`on_vehicle_position()` runs once per vehicle per second and is ordered as a
cost gradient — cheapest exit first. A vehicle with no active trip (most of the
fleet, most of the time) costs one query.

`tick_etas()` broadcasts only when the ETA moved more than
`ETA_CHANGE_THRESHOLD_S = 15.0`. An ETA that wobbles by two seconds would
otherwise generate a broadcast storm.

## 20.4 Frontend — code splitting

| Chunk | Size | Loaded |
|---|---|---|
| `index` | 256 kB | always |
| `vendor` (react, router, zustand) | 51 kB | always |
| `leaflet` | 155 kB | always — every screen has a map |
| **`charts` (recharts)** | **437 kB** | **only when `/analytics` opens** |
| `AnalyticsPage` | 17 kB | only when `/analytics` opens |

Adding Recharts naively pushed the main chunk from 256 kB to **709 kB** —
meaning every operator opening the live map during an incident downloaded
charting code. A `manualChunks` entry plus `React.lazy` on the route fixed it.

## 20.5 Rendering

- **`preferCanvas`** on the map — 4,000 SVG polylines is unusable; canvas is not.
- **`SegmentsLayer` memoised on the collection** — re-projecting the network on
  every vehicle tick would dominate the frame budget.
- **`isAnimationActive={false}`** on every chart — an operational dashboard that
  re-animates each poll is distracting and costs frames.
- **`useShallow` on array selectors** — the alternative is an infinite render
  loop, not merely a slow one.
- **Keyed maps in `opsStore`** — the socket delivers individual updates; a list
  would need a scan per frame.

## 20.6 Network

- **Per-layer refresh intervals** — vehicles 3 s, accident heatmap 300 s. One
  timer would mean either stale vehicles or 100× unnecessary queries.
- **A 403 stops that layer's polling** — otherwise an anonymous visitor
  generates a 401 every three seconds forever.
- **Batched parallel fetch** on the analytics page — ten small aggregates in one
  `Promise.all` beats a waterfall.
- **`AbortController` everywhere** — navigating away cancels in flight.
- **WebSocket coalescing** — 20 vehicles at 1 Hz → at most ~1.1 frames/s each.
- **gzip at the edge** for JSON, GeoJSON and CSV.

## 20.7 WebSocket scaling

| Concern | Approach |
|---|---|
| Cross-process | Redis channel layer |
| Horizontal | N `web` replicas; **no sticky sessions needed** |
| Per-connection cost | Coalescing + subscription filters |
| Slow clients | Nginx absorbs them; `limit_conn 24` per IP on `/ws/` |
| Long-lived | `proxy_read_timeout 3600s` at Nginx **and** at the cloud LB |
| Fan-out volume | `MAX_FANOUT = 500` on notifications, logged when truncated |

## 20.8 Known limits

| Limit | Value | Consequence |
|---|---|---|
| `MAX_EXPANSIONS` | 250,000 | A pathological route request fails rather than hanging |
| `MAX_FANOUT` | 500 | Beyond this a notification is a broadcast and needs a queue |
| `road_network` layer | 4,000 features | `truncated: true` is reported, not silent |
| Worker replicas | **1** | Not horizontally scalable — needs leader election first |
| Push delivery | synchronous | 500 subscriptions × 6 s timeout is the worst case |

---

# §21 Security

## 21.1 Posture summary

| Control | Implementation |
|---|---|
| Authentication | JWT, 15-min access in memory, 7-day httpOnly refresh, rotation + blacklist |
| Authorisation | Six-role RBAC, machine-audited, 306 parametrised assertions |
| PHI | Serializer-level redaction, fail-closed, enforced over REST **and** WebSocket |
| Transport | TLS at the edge, HSTS, secure cookies, `X-Forwarded-Proto` |
| Injection | Django ORM; the only raw SQL is parameterised |
| XSS | React escaping, strict `script-src`, no `dangerouslySetInnerHTML` |
| CSRF | Django middleware for session paths; JWT paths are stateless by design |
| Secrets | Environment-only, required in production, never logged |
| Rate limiting | Nginx zones, tight on auth |
| Container | Non-root uid 10001, no build toolchain in the runtime layer |

## 21.2 JWT

Covered in [Part 5 §9](PART-5-AUTH-AI-CV-GIS.md). The security-relevant points:

- **Access token in memory only.** An XSS cannot exfiltrate a durable
  credential; the token dies with the tab. Asserted by an E2E test.
- **Refresh token httpOnly and path-scoped.** JS cannot read it; it is not
  attached to every request.
- **Rotation + blacklist.** A used refresh token cannot be replayed.
- **Roles are re-derived from the database** on `/auth/me/`, so a revoked role
  takes effect on the next call.

## 21.3 RBAC and the audit

The permission matrix is in [Part 5 §9.5](PART-5-AUTH-AI-CV-GIS.md) and is
**executable**. Two audits fail the build:

- `audit_api_permissions()` — an endpoint relying on DRF's global default, or
  public without a written reason in `PUBLIC_READ_ENDPOINTS`.
- `ws_policy.policy_summary()` — a consumer with no declared `ConsumerPolicy`.

Plus a staleness check: an allowlist entry for a deleted endpoint is a
permission waiting to be silently reused by the next thing with that URL name.

**Five security defects were found and fixed** — all by probing a running server
or by the matrix, none by reading code. See
[Part 5 §9.9](PART-5-AUTH-AI-CV-GIS.md).

## 21.4 CSRF

| Path | Protection |
|---|---|
| `/legacy/*`, `/admin/` | Django `CsrfViewMiddleware` — session cookies need it |
| `/api/v1/*` with JWT | **Stateless.** The bearer token is not sent ambiguously by the browser, so CSRF does not apply |
| `/api/v1/auth/jwt/refresh/` | The cookie is `SameSite=Lax` and path-scoped; a cross-site POST cannot carry it |

`SEVPS_CSRF_TRUSTED_ORIGINS` covers deployments where the console is served from
a different origin.

## 21.5 CORS

Development only. `django-cors-headers` is **first** in `MIDDLEWARE` so CORS
headers are present on error responses too. Production is same-origin — Nginx
serves the console and proxies the API under one host — so CORS is not in the
request path at all.

## 21.6 SQL injection

**The ORM is used throughout.** The only raw SQL is in `apps/core/spatial.py`
and it is parameterised:

```python
cursor.execute(
    f"SELECT {pk} FROM {table} WHERE ST_DWithin(geom, ST_MakePoint(%s,%s)::geography, %s)",
    [lon, lat, radius_m],
)
```

The table and column names are interpolated but come from
`POINT_TABLES` — a module-level constant listing 13 known models, never user
input. Coordinates and radius are bound parameters.

## 21.7 XSS

- **React escapes by default.** There is no `dangerouslySetInnerHTML` anywhere
  in the codebase.
- **CSP `script-src 'self'`** — no inline scripts, no CDNs.
- `style-src 'unsafe-inline'` is a real concession to Leaflet's runtime styling.
  It is the weakest directive in the policy and the one to revisit if Leaflet
  ever supports nonce-based styling.
- **Django template autoescaping** on the legacy screens.
- **`X-Content-Type-Options: nosniff`** — a JSON response cannot be coerced into
  executing as script.

## 21.8 Clinical data protection

Beyond the redaction mixin:

| Control | Detail |
|---|---|
| Minimisation | Only category, age, deterioration and free-text notes are stored. SEVPS is not an ePCR |
| Role gating | Traffic police are excluded **by design**, not by oversight |
| Fail-closed | No serializer context → redact |
| WebSocket parity | The hospital snapshot goes through the serializer |
| Push payloads | Carry no clinical data; `context` is dropped entirely |
| Analytics | Aggregate only. No export contains a patient field |
| Audit | `HospitalRecommendationLog` stores a JSON snapshot, reconstructable years later |

## 21.9 Secrets management

| Secret | Storage | Production requirement |
|---|---|---|
| `SEVPS_SECRET_KEY` | env | **Required** — `ImproperlyConfigured` if the dev key survives |
| `SEVPS_VAPID_PRIVATE_KEY` | env or `models/` volume | **Required** in the prod overlay |
| Database password | env | **Required** |
| `SEVPS_MAPBOX_TOKEN` | env | Optional. **Public by nature** — sent to the browser to fetch tiles |
| `SEVPS_FCM_CREDENTIALS` | file path | Optional |

Nothing is committed. `.dockerignore` excludes `.env`, `*.sqlite3` and `models/`
so no secret or database is baked into an image.

## 21.10 HTTPS

Terminate at the cloud LB or at Nginx, then `SEVPS_BEHIND_TLS_PROXY=1`. Without
it Django believes every request arrived over plain HTTP: `SECURE_SSL_REDIRECT`
loops and the secure cookie flags behave as though the site were insecure.

Leave `SEVPS_SECURE_SSL_REDIRECT=0` when something in front already redirects —
two redirectors plus one misconfigured forwarded header is an infinite loop that
presents as an application outage.

**Push requires HTTPS.** `localhost` is exempt for development; over plain HTTP
a service worker cannot register and push silently does not exist. The UI
reports this as `insecure` rather than as a generic failure.

## 21.11 Residual risks

Honest list of what is **not** addressed:

| Risk | Status | Mitigation path |
|---|---|---|
| Signal-controller authentication | The HTTP adapter supports an endpoint URL but no mutual TLS or signed commands | Required before any real municipal integration |
| Audit log immutability | `PriorityDirective` and `HospitalRecommendationLog` are append-only by convention, not enforced | Append-only table or external log shipping |
| Rate limiting is per-IP | A control room behind one NAT shares a bucket | Per-token limiting once the client population is known |
| No secrets rotation procedure | Rotating `SECRET_KEY` invalidates sessions; rotating VAPID invalidates push | Documented rotation runbook |
| No penetration test | Internal review and automated audits only | Third-party assessment before production |
| Django admin is broadly powerful | Administrators can edit any record | Per-model admin permissions if the admin population grows |

---

**Previous:** [Part 7 — Walkthrough & Flows](PART-7-WALKTHROUGH-AND-FLOWS.md)
**Next:** [Part 9 — Future Work, File Changes, Packages, Summary](PART-9-FUTURE-CHANGES-PACKAGES-SUMMARY.md)
