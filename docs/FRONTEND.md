# SEVPS Console — React 19 + TypeScript + Vite (Phase 4)

## What changed

The server-rendered screens are **kept, not deleted**. They now live under `/legacy/` and
remain the fallback for kiosk and embedded displays with no JavaScript build pipeline —
and they keep SEVPS usable when the React bundle has not been built. The React console is
the new primary at `/`.

Zero API endpoints were removed. The console consumes 20 of the existing 199.

## Running it

```bash
# Development — Vite on 5173, proxying API and WebSockets to Django on 8000
npm --prefix frontend install
npm --prefix frontend run dev
python manage.py runserver

# Production — build once, Django serves the bundle
npm --prefix frontend run build
python manage.py runserver          # http://127.0.0.1:8000/
```

| Script | Does |
|---|---|
| `npm run dev` | Vite dev server with HMR |
| `npm run build` | `tsc --noEmit` then a production build |
| `npm run typecheck` | Types only |
| `npm run test` | Vitest |

## Why the dev server proxies instead of using CORS

The JWT refresh token is an `httpOnly` cookie scoped to `/api/v1/auth/`. Cross-origin, that
cookie needs `SameSite=None; Secure`, which does not work over plain `http://localhost`.
Proxying `/api` and `/ws` through Vite makes dev same-origin, so the cookie behaves exactly
as it will in production and there is no parallel dev-only auth path to maintain.

## Token handling

Mirrors the Phase 2 server decisions:

- **Access token in memory only** (module scope in `src/api/client.ts`). It dies with the
  tab. Not in `localStorage`, not in a store, not in devtools' Application tab.
- **Refresh token in the httpOnly cookie** the browser holds and JS cannot read. Reload
  recovers the session via `/auth/jwt/refresh/` with no token in hand.

Consequences worth knowing: there is no "remember me", and a hard refresh briefly shows a
"Restoring session…" splash while the silent refresh runs. The router deliberately does not
evaluate guards until that resolves — otherwise every reload bounces an authenticated
operator to the login screen.

`client.ts` retries **once** after a refresh, and only on 401. A 403 means the token is
valid and the *role* is insufficient; refreshing cannot fix that. Concurrent 401s are
coalesced into a single refresh (a dashboard fires four polls at once).

## WebSockets

`useSocket` passes the access token on the query string, because a browser cannot set an
`Authorization` header on a WebSocket handshake. That is precisely why access tokens are
15 minutes server-side — query strings land in access logs.

Every snapshot carries the `viewer` block added in Phase 2. The console renders a
**"socket anonymous"** badge when the server did not authenticate the connection, so a
broken handshake is visible rather than silently degrading to redacted data.

## Why the console polls as well as subscribing

Not belt-and-braces for its own sake. With the default in-memory channel layer, events
published by `sevps_worker` or `simulate` never reach a socket held by the web process —
the cross-process limitation found in Phase 1 verification. So:

- **polling is the correctness floor** (2–4 s),
- **the socket is the latency improvement**.

Set `SEVPS_REDIS_URL` and the socket carries everything. The Settings screen says which
channel layer is live, and explains this inline when it is in-memory.

`usePolling` stops permanently on a 401/403 rather than hammering a protected endpoint from
a signed-out tab.

## Clinical redaction in the UI

The server blanks `patient_age`, `patient_notes`, `caller_number` and `incident_address` for
roles without clinical clearance, and sets `clinical_data_redacted: true`. The console
renders an explicit **"Patient details withheld for your role"** note wherever that flag
appears. A blank notes field would otherwise read as *"no clinical information recorded"* —
a materially different and dangerous message.

Sign in as `police` and the Operations board shows every trip with its priority, position
and ETA, and no diagnosis. That is the Phase 2 policy made visible.

## Structure

```
frontend/src/
  api/         client.ts (JWT + refresh)  endpoints.ts (all 20 URLs)  types.ts
  app/         App.tsx (router)  AppShell.tsx  RequireAuth.tsx
  components/  MapCanvas.tsx (Leaflet)  ui.tsx (shared primitives, formatters)
  hooks/       useSocket.ts  usePolling.ts
  pages/       Operations  Paramedic  Hospital  Analytics  Driver  Boards  Settings  Login
  stores/      authStore.ts  opsStore.ts
```

Components never build URLs — every endpoint lives in `endpoints.ts`, so the API surface
the console depends on is greppable in one file. That is what makes it possible to tell,
before changing a serializer, whether the console cares.

Route guards are a **usability** feature, not a security boundary: every route is also
enforced server-side by the Phase 2 permission classes. Their value is not showing an
operator a screen that will only 403 on them.

## Coordinate convention

SEVPS APIs return `[lat, lon]` everywhere **except** the GeoJSON endpoints, which follow the
spec and return `[lon, lat]`. Leaflet wants `[lat, lon]`. `SegmentsLayer` is the only place
that swaps, and it is the only place that should.

## Two config files, on purpose

Vitest 2 bundles its own copy of Vite. A single config importing `defineConfig` from
`vitest/config` while `@vitejs/plugin-react` resolves against the top-level Vite produces
two distinct `Plugin` type identities and a wall of unassignable-type errors. `vite.config.ts`
and `vitest.config.ts` are separate so each resolves cleanly.

`base` is `/static/` in production so the hashed asset URLs baked into `index.html` match
where Django serves them, and `/` in dev where Vite serves the app itself.

## Verified

```
typecheck   tsc --noEmit, strict + noUncheckedIndexedAccess     clean
tests       19 vitest tests (client refresh semantics, stores)  pass
build       110 modules → 468 kB raw / 145 kB gzipped           pass
Django      162 tests                                          pass
routing     /login /login/ → React,  /legacy/login/ → Django    unambiguous
assets      4/4 served by Django from /static/assets/           200
legacy      /legacy/{,analytics,boards,driver}                  200
isolation   /api/, /admin/ not swallowed by the catch-all       correct
JWT         create → bearer call → cookie-only refresh → rotate works
```

## Not done in this phase

- **Charts** — the Analytics screen renders stat tiles and tables. Recharts is Phase 10 in
  the plan; the data shapes are already chart-ready, so that phase is a rendering change
  rather than new endpoints.
- **Playwright** — Phase 12.
- **Dispatcher trip creation UI** — the API exists (`POST /api/v1/dispatch/trips/`,
  dispatcher role) but no screen drives it yet; trips still come from the simulator or the
  API. This was a gap in the legacy console too.
