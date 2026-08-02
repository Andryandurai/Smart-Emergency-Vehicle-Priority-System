# Deployment (Phase 11)

## What ships

```
Dockerfile                    4-stage build; the image builds the console itself
docker/entrypoint.sh          role dispatcher: web | worker | migrate | simulate
docker/nginx/nginx.conf       edge: TLS, static, WebSocket upgrade, rate limits
docker/nginx/conf.d/          shared proxy headers
docker-compose.yml            base — no host ports, secure by default
docker-compose.override.yml   local only — auto-loaded, publishes ports
docker-compose.prod.yml       production — nginx, DEBUG off, replicas, limits
```

```bash
# Local
docker compose up --build

# Production
cp .env.example .env && $EDITOR .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

> **Not executed here.** Docker is not installed on the development machine, so
> `docker build` and `docker compose up` have not been run. What *has* been run
> is 30 automated tests that parse these files and assert their structure
> (`apps/core/tests_deployment.py`), `sh -n` on the entrypoint, and
> `manage.py check --deploy` under production settings. Two real defects were
> found that way — see *What the checks caught* below. Treat the first real
> `docker compose up` as the remaining verification step.

---

## Four decisions worth explaining

### 1. Liveness and readiness are different endpoints

The previous `HEALTHCHECK` used `/api/v1/health/`, which queries the database.
That makes it a **readiness** check wired into a **liveness** slot, and the
consequence is bad: when Postgres blips, Docker restarts every application
container. Restarting cannot fix a database outage — it only removes processes
that would have recovered on their own the moment the database came back, and
in a rolling restart it can take down every replica over a fault none of them
caused.

| Endpoint | Checks | Used by | On failure |
|---|---|---|---|
| `/api/v1/health/live/` | nothing | Docker `HEALTHCHECK`, k8s `livenessProbe` | restart the container |
| `/api/v1/health/ready/` | database, channel layer | load balancer, k8s `readinessProbe` | remove from the pool |
| `/api/v1/health/` | both, combined | existing monitors, the console | unchanged, kept for compatibility |

`test_liveness_touches_no_dependency` asserts liveness issues **zero** queries.

### 2. The base compose file publishes no ports

Host port mappings live in `docker-compose.override.yml`, which Compose loads
automatically for a bare `docker compose up` and **never** loads when files are
named with `-f`.

That ordering is the point. The obvious arrangement — ports in the base, an
overlay that removes them — fails *open*: a deploy that forgets `-f
docker-compose.prod.yml` puts an emergency service's PostgreSQL on a public
interface. This way, the same mistake produces a stack that is simply
unreachable. A visible outage beats a silent exposure.

(It also avoids the `!override` YAML tag, which needs Compose ≥ 2.24.)

### 3. Migrations are a separate service under an advisory lock

Previously `migrate` ran inline in the web service's command. Two problems:

- **Concurrency.** With `replicas: 2`, both replicas ran `migrate` at once.
  Django has no internal lock; concurrent migrations on one database race and
  can half-apply.
- **Ordering.** The worker started when the web *container* did, not when
  migrations finished — so it could query a table that did not exist yet and
  crash-loop until restart backoff hid the cause.

Now a one-shot `migrate` service owns schema changes, `web` and `worker` both
`depends_on: {migrate: {condition: service_completed_successfully}}`, and the
entrypoint wraps `migrate` in `pg_advisory_lock` so a second replica blocks,
then finds everything applied and does nothing.

The worker **never** migrates. Exactly one role owns schema.

### 4. The image builds the frontend

The old Dockerfile did `COPY . .` and relied on `frontend/dist` existing on the
host. That ships whatever a developer last built — last week's UI against this
week's API, with nothing visible from outside to say so. Now a `node:22-alpine`
stage runs `npm ci && npm run build`, which is `tsc --noEmit && vite build`, so
**a type error fails the image build** instead of failing in someone's browser.

No Node, no `build-essential`, no GDAL in the shipped layer
(`test_no_build_toolchain_ships_in_the_runtime_layer`).

---

## The worker is pinned to one replica

```yaml
worker:
  deploy:
    replicas: 1        # base and prod
```

Not a scaling oversight. The worker sweeps corridors and expires signal holds.
Two instances would both decide a junction should be released and issue
duplicate controller commands to real traffic infrastructure. Scaling the
worker requires leader election or partitioned work assignment first; until
then the constraint is enforced in both compose files and asserted by a test.

`web` scales freely — every replica shares group state through Redis, so a
dashboard connected to replica 2 receives an event raised by a request served
by replica 1. **No sticky sessions are needed**, because the access token is
held in memory by the client and the refresh token is an httpOnly cookie
validated by signature, not by server-side session state.

---

## Nginx

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

Without the two headers the handshake returns 200 instead of 101, the browser
falls back to polling, and the console still works — just seconds behind, with
nothing on screen to say so. And `proxy_read_timeout` must exceed the default
60 s, because a dashboard holds one socket open all shift; at the default it is
cut every minute and the symptom is a console that quietly reconnects and
misses frames.

### Rate limiting, and what is deliberately not limited

| Zone | Rate | Why |
|---|---|---|
| `auth` | 12 r/**min** | Credential stuffing. A real user signs in once; a script tries thousands. |
| `api` | 60 r/**sec** | Backstop only. The console legitimately polls several endpoints every 2–3 s. |
| health | none | A probe tripping the limiter takes a healthy instance out of the pool — the limiter causing the outage it exists to prevent. |

**Telemetry is not rate-limited.** Twenty ambulances reporting position at 1 Hz
during a mass-casualty incident, behind one control-room NAT, is exactly the
pattern a naive per-IP limit mistakes for an attack — at the moment the system
matters most.

### Caching

Vite emits content-hashed filenames, so `/static/` is `immutable` for a year.
`index.html` is **not** hashed and is served `no-store`: cache it and a browser
keeps requesting chunk names that no longer exist after a deploy, and the app
404s its own JavaScript. `/sw.js` is `no-cache` for the same reason — a stale
service worker keeps handling pushes with old logic long after a deploy.

---

## Secrets and state that must survive a redeploy

| Item | Consequence of losing it |
|---|---|
| `SEVPS_SECRET_KEY` | every session and CSRF token invalidated |
| **`SEVPS_VAPID_PRIVATE_KEY`** | **every push subscription invalidated** — every controller, hospital and crew must re-grant notification permission, and they discover this only when an alert fails to arrive |
| `postgres_data` | everything |
| `media_files` | uploaded camera frames |

The VAPID keypair gets its own named volume (`vapid_keys`) mounted into all
three application roles, and the production overlay makes it a **required**
variable (`${SEVPS_VAPID_PRIVATE_KEY:?...}`). The entrypoint deliberately does
**not** generate one when missing — a key regenerated on every container start
would silently break push for the whole fleet — it warns instead.

`docker-compose.prod.yml` uses `${VAR:?message}` for every required secret, so
a missing value fails the deploy with a readable error rather than booting on
`dev-insecure-key-do-not-use-in-production`.

---

## Cloud

The compose stack is the portable unit; each cloud needs the same four pieces.
Everything below is the mapping and the specific gotchas — not IaC, because
untested Terraform for three providers is a liability rather than a deliverable.

| Piece | AWS | Azure | GCP |
|---|---|---|---|
| Images | ECR | ACR | Artifact Registry |
| Containers | ECS Fargate / EKS | Container Apps / AKS | Cloud Run / GKE |
| Database | RDS PostgreSQL + PostGIS | Database for PostgreSQL Flexible Server | Cloud SQL for PostgreSQL |
| Channel layer | ElastiCache Redis | Azure Cache for Redis | Memorystore |
| Edge | ALB (+ CloudFront) | Application Gateway / Front Door | HTTPS LB (+ Cloud CDN) |
| Secrets | Secrets Manager | Key Vault | Secret Manager |

### The gotcha that bites every cloud: WebSocket idle timeout

Each provider's default load-balancer idle timeout is **shorter than a shift**,
and the failure looks like flaky sockets rather than a configuration problem:

- **AWS ALB** — default 60 s. Raise `idle_timeout.timeout_seconds` (max 4000).
- **Azure Application Gateway** — default 4 min request timeout. Raise it, and
  ensure WebSocket support is on (v2 SKU).
- **GCP HTTPS LB** — backend service `timeoutSec` defaults to 30 s and applies
  to WebSockets as the *maximum connection duration*. Raise it explicitly.

The nginx config already sets 3600 s; the cloud LB in front of it must agree,
or it caps whatever nginx allows.

### Other per-cloud notes

**PostGIS.** RDS and Cloud SQL both support it, but it is not enabled by
default: `CREATE EXTENSION postgis;` must run once, and the SEVPS spatial
migration does exactly that — so the migration will fail loudly on a database
where the extension is unavailable, which is the correct outcome. Azure
Flexible Server requires PostGIS to be added to `azure.extensions` first.

**Cloud Run.** Attractive for the web tier, wrong for the **worker**: Cloud Run
scales to zero and does not guarantee a single always-on instance, and the
corridor safety sweep must run continuously. Run `web` on Cloud Run if you
like; run `worker` on GKE, a Compute Engine VM, or an always-on Cloud Run
service with `min-instances=1, max-instances=1`.

**Fargate + the worker.** Set `desiredCount: 1` and, importantly,
`deploymentConfiguration.maximumPercent: 100` — the default of 200 starts a
replacement task *before* draining the old one, which is precisely the
duplicate-worker situation the replica pin exists to prevent.

**Redis is not optional above one process.** With the in-memory channel layer,
an event raised by the worker never reaches a dashboard connected to the web
tier. The console appears to work — it polls as a fallback — and is simply
always stale. `/api/v1/info/` reports the active channel layer, and the
Settings screen shows a warning when it is in-memory.

**Managed Postgres and the entrypoint wait.** There is no compose healthcheck
to depend on, and a failover can take the endpoint away for tens of seconds
mid-deploy. `wait_for_db` retries for `SEVPS_DB_WAIT_ATTEMPTS × 2` seconds
(default 120 s) rather than crash-looping.

### TLS

Terminate at the cloud load balancer or at nginx, then set
`SEVPS_BEHIND_TLS_PROXY=1` so Django reads `X-Forwarded-Proto`. Without it,
Django believes every request arrived over plain HTTP: `SECURE_SSL_REDIRECT`
loops infinitely and the secure cookie flags behave as though the site were
insecure.

Leave `SEVPS_SECURE_SSL_REDIRECT=0` when something in front already redirects.
Two redirectors plus one misconfigured forwarded header is an infinite loop
that presents as an application outage.

### HSTS preload is off by default, on purpose

`manage.py check --deploy` warns about this, and the warning is expected.
Preload submission is close to irreversible: removal takes months to propagate
through browser releases, and until it does, **every subdomain is unreachable
over plain HTTP**. A municipal deployment may still have a roadside display
controller or a legacy signal bridge on HTTP — exactly the equipment this
platform exists to drive. Set `SEVPS_HSTS_PRELOAD=1` once the whole estate is
known to be TLS.

---

## What the checks caught

Two real defects, both found by parsing rather than by review:

1. **A YAML syntax error.** `${SEVPS_VAPID_SUBJECT:?... mailto: or https: URI}`
   — the unquoted colons inside the error message made the file unparseable.
   Compose would have failed to start the stack.
2. **`node_modules` in the build context.** `.dockerignore` excluded the SQLite
   database but not `frontend/node_modules` or `frontend/dist`, so `COPY . .`
   would have copied hundreds of megabytes in and could have shadowed the
   freshly built console with a stale host build.

Both are now asserted by tests.

---

## Verification

```
python manage.py test apps.core.tests_deployment    # 30 tests
python manage.py test apps                          # 418 tests, OK (3 skipped)
sh -n docker/entrypoint.sh                          # POSIX syntax
SEVPS_DEBUG=0 ... python manage.py check --deploy   # 1 expected warning (HSTS preload)
```

Live: `/health/live/` → `{"status":"alive"}`, `/health/ready/` →
`{"status":"ready","checks":{"database":"ok","channel_layer":"..."}}`, and the
original `/health/` still 200.

**Still to do on a machine with Docker:** `docker compose build`, `up`, and a
WebSocket smoke test through nginx (a 101 on `/ws/ops/`, not a 200).
