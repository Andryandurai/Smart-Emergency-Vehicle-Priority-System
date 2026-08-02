# SEVPS Authentication & RBAC (Phase 2)

## What changed and why

Before this phase SEVPS authenticated with DRF `TokenAuthentication` and had three
ad-hoc groups. Two problems made the change necessary rather than merely desirable:

1. **`/api/v1/dispatch/trips/` served patient data to anonymous callers.** `patient_age`,
   `patient_notes`, `incident_address` and `caller_number` were all readable with no
   credentials. That is a PHI/PII exposure, not a styling issue.
2. **Two of the three permission groups referenced in code had never been created**, so
   the role split existed only in comments.

Phase 2 replaces the token scheme with JWT, formalises six roles, protects every
endpoint with a *declared* policy, and adds a build-failing audit so the property stays
true as the API grows.

## The six roles

Roles are Django `Group` rows. `apps/core/roles.py` is the single source of truth,
shared by the permission classes, `seed_users`, and the JWT claims.

| Role (group) | Clinical data | Traffic control | Dispatch |
|---|:--:|:--:|:--:|
| `administrators` | ✅ | ✅ | ✅ |
| `traffic_police` | ❌ | ✅ | ❌ |
| `hospital_staff` | ✅ | ❌ | ❌ |
| `ambulance_drivers` | ✅ | ❌ | ❌ |
| `dispatchers` | ✅ | ❌ | ✅ |
| `public_users` | ❌ | ❌ | ❌ |

**Traffic police deliberately have no clinical clearance.** Running a green corridor
needs a vehicle's priority and position; it never needs the diagnosis. Data
minimisation, not distrust — and it is enforced, not documented: `ClinicalRedactionMixin`
blanks `PHI_FIELDS` and sets `clinical_data_redacted: true` for any caller without
clearance, on REST *and* on the WebSocket.

A superuser implicitly holds every role: an administrator locked out of their own
platform mid-incident is a worse failure than an over-broad grant.

### Backward compatibility

The pre-RBAC group names still work. `operators` → `traffic_police` and `paramedics` →
`ambulance_drivers` are resolved as aliases, so an upgraded deployment authorises
correctly *before* anyone re-runs `seed_users`. Running `seed_users` then migrates members
onto the canonical groups and leaves the old (now empty) groups in place so a rollback is
possible.

## Demo accounts

```bash
python manage.py seed_users
```

| Username | Password | Role |
|---|---|---|
| `admin` | `sevps-admin` | Administrator |
| `police` | `sevps-police` | Traffic Police |
| `dispatcher` | `sevps-dispatcher` | Emergency Dispatcher |
| `paramedic` | `sevps-paramedic` | Ambulance Driver |
| `hospital` | `sevps-hospital` | Hospital Staff |
| `public` | `sevps-public` | Public User |
| `operator` | `sevps-operator` | Traffic Police *(legacy account, retained)* |

Change these before any deployment reachable beyond your own machine. The command refuses
to run with `DEBUG=0` unless given `--force`.

## JWT

```
POST /api/v1/auth/jwt/create/    {username, password} -> {access, refresh, user}
POST /api/v1/auth/jwt/refresh/   {refresh} or httpOnly cookie -> {access, refresh}
POST /api/v1/auth/jwt/verify/    {token} -> 200 / 401
POST /api/v1/auth/jwt/logout/    blacklists the refresh token, clears the cookie
GET  /api/v1/auth/me/            identity + capabilities (re-derived, not from claims)
GET  /api/v1/auth/roles/         role catalogue
GET  /api/v1/auth/policy/        live endpoint-protection audit (admin only)
```

**Token storage.** The refresh token is set as an `httpOnly`, `SameSite=Lax` cookie scoped
to `/api/v1/auth/`, and is also returned in the body for clients that cannot use cookies
(native apps, onboard units). The access token is returned in the body only and is meant
to live in memory. A refresh token in `localStorage` is readable by any XSS and amounts to
persistent account takeover; an in-memory access token dies with the tab.

Access tokens are short (15 min) because they are also passed on the WebSocket query
string, where they land in access logs. Refresh tokens rotate and the old one is
blacklisted on use.

**Roles are embedded as a claim** so a client can render its own UI without a second round
trip — but the claim is advisory. The server re-derives roles from the database on every
request, so a revoked role takes effect immediately rather than at token expiry.

### Migration path for existing clients

All three schemes are accepted simultaneously:

```python
DEFAULT_AUTHENTICATION_CLASSES = [
    "rest_framework_simplejwt.authentication.JWTAuthentication",   # target
    "rest_framework.authentication.TokenAuthentication",           # legacy, retained
    "rest_framework.authentication.SessionAuthentication",         # dashboards
]
```

`/api/v1/auth/token/` still issues legacy tokens, and `/api/v1/auth/me/` reports which
scheme authenticated the call (`jwt` / `legacy_token` / `session`). Retire the legacy path
once every client reports `jwt`.

## WebSocket authentication

This is the step a JWT migration usually misses. Channels' `AuthMiddlewareStack`
authenticates from the **session cookie**, so a client holding only a bearer token
connects as `AnonymousUser` while its REST calls beside it succeed. Nothing errors —
sockets simply stop knowing who is on them, and any role check fails open.

`apps/core/ws_auth.py` resolves identity from, in order: an `Authorization: Bearer`
header, a `?token=` query parameter (browsers cannot set headers on a WS handshake), then
the session.

**Nesting order matters.** `AuthMiddlewareStack` resolves `scope["user"]` *before*
delegating inward, so it must be the outer layer and JWT the inner one:

```python
AuthMiddlewareStack(JWTAuthMiddleware(inner))   # correct
JWTAuthMiddleware(AuthMiddlewareStack(inner))   # session clobbers the JWT user
```

Every socket snapshot now includes a `viewer` block stating whether the server
authenticated the connection, which role(s) it resolved and by which method — so a broken
handshake is visible instead of silently degrading to redacted data.

```json
{"event": "snapshot", "data": {"viewer": {"authenticated": true, "username": "paramedic",
 "roles": ["ambulance_drivers"], "clinical_access": true, "auth_method": "jwt"}, "...": "..."}}
```

## "Protect every API", made checkable

`apps/core/api_policy.py` walks the live URL conf and fails the build if any endpoint:

- relies on the DRF global default instead of declaring its own policy, or
- is publicly readable without an entry in `PUBLIC_READ_ENDPOINTS`.

Making an endpoint public therefore requires a deliberate edit to an allowlist, with a
written reason, visible in a diff. The global default was also changed from
`ReadOnlyOrAuthenticated` to `IsAuthenticatedRole`, so an endpoint that forgets a policy
fails closed rather than leaking.

DRF's auto-generated router index is subclassed (`apps/core/routers.py`) rather than
exempted, so the audit needs no carve-outs for framework internals.

### Endpoints that are public on purpose

Health probe, service info, token issuance/refresh/verify/logout, role catalogue, router
index, road-network GeoJSON, route planning and congestion forecast, hospital
recommendation and clinical rule lookup, Layer 6 priority profiles, roadside display board
feed, and the driver-alert lookup a road user's phone performs. All read-only; none carries
patient-identifying data. The driver-alert path is the one that matters most — a road user
must receive an approaching-ambulance warning without holding an account.

## Impact on existing behaviour

| Behaviour | Before | After |
|---|---|---|
| Anonymous read of patient data | **allowed** | denied |
| Anonymous read of trips / preemptions / directives | allowed | denied |
| Anonymous read of road network, boards, alerts, routing | allowed | unchanged |
| Traffic police reading `patient_notes` | allowed | redacted |
| Legacy `operators` / `paramedics` groups | authorised | still authorised (aliased) |
| Legacy `/auth/token/` clients | worked | still work |
| Server-rendered dashboards | session auth | unchanged, plus a sign-in banner on 401 |

The ops, analytics and hospital dashboards now require sign-in to show operational data.
They degrade with one clear banner and stop their pollers rather than showing a silently
stale map.

## Test coverage

33 new tests (137 total):

- role resolution, legacy aliases, superuser implication, clinical clearance
- JWT obtain/refresh/verify/logout, blacklist-on-reuse, httpOnly cookie, role claims
- all three auth schemes still working side by side
- table-driven RBAC matrix over (endpoint × 6 roles + anonymous)
- PHI redaction per role, over REST and WebSocket, and fail-closed with no user
- WebSocket JWT via header and query string, bad token, anonymous, middleware order
- policy audit: no undeclared endpoint, no undocumented public endpoint
