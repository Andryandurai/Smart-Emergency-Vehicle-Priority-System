# Testing (Phase 12)

## The four layers, and what each one can see

| Layer | Count | Runner | Catches what nothing below it can |
|---|---|---|---|
| Django unit / integration | 418 | pytest or `manage.py test` | Business logic: corridor timing, hospital scoring, redaction rules |
| RBAC matrix | 306 | pytest (`-m rbac`) | Authorisation, as a whole-product sweep rather than per-app guesses |
| Vitest | 33 | `npx vitest run` | Pure frontend logic: formatting, store reconciliation, base64 decoding |
| Playwright | 34 | `npx playwright test` | The seams — sockets, auth round trips, charts that render nothing |

```bash
pytest                          # 721 tests + 123 subtests, ~25s
pytest -m rbac                  # 306, ~8s
python manage.py test apps      # 418, still works
cd frontend && npx vitest run
cd frontend && npx playwright test
```

---

## Pytest is the runner, not a rewrite

The brief asked for Pytest. The recommendation delivered is to **adopt it as the
runner and keep every existing test**: pytest executes `django.test.TestCase`
classes natively, so all 418 were collected and passed on the first run with no
edits. A migration that required touching 418 working tests would have risked
more than it gained, and would have meant a large diff in which a genuine
behavioural change could hide.

Both runners are kept working, and CI runs both. `manage.py test` is what a
municipal operator with no pytest install reaches for, and a suite that only
runs one way quietly stops running the other.

What pytest actually adds is what unittest makes awkward:

- **Parametrisation over a matrix.** The RBAC sweep is 6 roles × ~40 endpoints,
  expressed once. In unittest it is a nested loop inside one test, where the
  first failure hides the rest.
- **Fixtures composed by dependency.** A test needing a signalised junction, an
  ambulance and a hospital asks for three fixtures rather than inheriting a
  base class that builds those three plus eight it does not need.
- **Markers.** `-m rbac` is its own CI job; `-m slow` can be excluded pre-commit.

### `--reuse-db` is deliberately not the default

SEVPS migrations create PostGIS generated columns and GiST indexes. A stale
reused database silently skips them, and the spatial tests then pass against
the wrong schema — the worst kind of green. Opt in with `pytest --reuse-db`
when iterating on one test.

### One fixture worth knowing about

```python
@pytest.fixture(autouse=True, scope="session")
def _fast_password_hashing():
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
```

Django's PBKDF2 does ~600,000 iterations — right in production, ruinous in a
suite creating seven users per test. The RBAC matrix took **over 20 minutes**
before this and **20 seconds** after. Safe because it applies only under
pytest and nothing asserts hash strength.

---

## The RBAC matrix

`apps/core/tests_rbac_matrix.py` is the centrepiece. Authorisation is the
property most likely to regress *silently*: a serializer change or a new mixin
can quietly open an endpoint and nothing fails — the feature still works, it
just also works for people it should not.

Four security defects were found in earlier phases by probing the running
server by hand. This is the machine-checked version of that probing.

```
PUBLIC_GETS            16 URLs × 8 callers   — must always be open
AUTHENTICATED_GETS     19 URLs              — denied anonymously, open to staff roles
ADMIN_ONLY_GETS         1 URL               — /auth/policy/, admins only
WRITE_CASES             5 actions × 8 callers — the command table
CLINICAL_FIELDS         redaction, per role
```

`outcome()` deliberately does **not** fold 404 and 405 into "denied". An
endpoint that 404s for an unauthorised caller leaks less than one that 403s,
but it is also not what the policy says — and treating them as equivalent
means a typo'd URL would pass every denial assertion.

### It found a fifth defect

`public_users` — a citizen with an account — could read **live ambulance
positions, active trips, emergency routes, accident heatmaps and the full
analytics history.**

The role registry had always said, in `RoleSpec.description`:

> "Registered road user. Receives driver alerts and reports incidents; **no
> operational or clinical access**."

But `IsAuthenticatedRole` had an empty `required_roles`, and
`BaseRolePermission` reads that as "anyone signed in". The documented contract
and the enforcement had disagreed since the role was introduced.

Live fleet tracking is not public data even in aggregate: it discloses, in near
real time, which streets an ambulance was dispatched to.

**The fix:** a new `OPERATIONAL_ROLES` constant (every role except
`public_users`), and `IsAuthenticatedRole.required_roles` set to it.
`/api/v1/auth/me/` deliberately keeps DRF's plain `IsAuthenticated`, so a
public user can still read their own account.

The pre-existing matrix in `tests_auth.py` listed `public` as allowed on three
rows. That was descriptive of the behaviour rather than an argued intent, so it
was corrected alongside — including `/alerts/driver-alerts/`, which is the full
alert table with trip ids, not the road-user lookup. Road users keep
`/alerts/nearby/`, `/alerts/boards/live/` and the public GIS layers.

---

## Playwright

Real browser, real Django, real WebSocket. Playwright starts both servers, so
`npx playwright test` is one command with no setup ritual.

**Its own database.** `SEVPS_SQLITE_PATH` (added this phase) points the E2E
server at `e2e.sqlite3`. An E2E run against the working database either
destroys a developer's hand-built setup or passes because of it.

**Sign-in goes through the real form**, never by injecting a token. The token
strategy — access token in memory, refresh token in an httpOnly cookie — is
split across the API client, the auth store and Django. A test that injected a
token would keep passing while real users could not sign in.

**Console errors fail the test.** A React component that throws inside an
effect renders an empty region, and the assertion below it then fails with
"element not found" — sending whoever is debugging to the selector rather than
to the stack trace that explains it.

### It found two real bugs

**1. An infinite render loop on the Operations console.**

```
Maximum update depth exceeded.
The result of getSnapshot should be cached to avoid an infinite loop.
```

`selectTripList`, `selectVehicleList` and `selectOpenPreemptions` each build a
new array per call. Zustand 5 sits on React's `useSyncExternalStore`, which
compares snapshots with `Object.is` — a fresh array is never equal to the last
one, so the component re-renders, the selector runs again, and the page locks
up. **Blank screen.** Fixed with `useShallow` at every call site, and the
constraint is now documented where the selectors are defined.

This is precisely the class of failure only a browser can see. Vitest tests the
selectors as functions and they are correct as functions.

**2. Every CSV export returned 401.**

The export cards were `<a href download>`. SEVPS authenticates with a bearer
token held in memory, and a browser navigation carries no such header — the
refresh cookie is scoped to `/api/v1/auth/` and is not a session. So clicking
an export saved a file called `daily.csv` containing an error page.

Fixed by `charts.downloadExport()`: fetch with the Authorization header, then
hand the browser a blob, honouring the server's `Content-Disposition` filename.

### Two Windows-specific setup issues, for the next person

- **Vite binds to `localhost`**, which resolves to `::1`, while Playwright
  probes `127.0.0.1` and reports the server as never having started. Fixed with
  `--host 127.0.0.1` and a `url:` health check instead of `port:`.
- **`package.json` has `"type": "module"`**, so `__dirname` does not exist in
  `playwright.config.ts`. Uses `fileURLToPath(import.meta.url)`.

### Three assertions that were wrong about the app, not the app being wrong

Worth recording, because each was a temptation to "fix" working code:

- The ops console has **no page heading** — it is a full-bleed map, and screen
  space goes to the situation rather than to a title.
- The **event log is empty by default**, so it has zero height and is "hidden".
  Asserting visibility would make the test pass only when something had gone
  wrong somewhere in the city. Now asserts `toBeAttached`.
- **Basemap tiles load before a listener can attach.** Watching for the network
  request races the thing it watches. Now asserts on the rendered tile `src`,
  which also checks no API key is in the URL.

---

## Coverage

```bash
pytest --cov=apps --cov-report=term-missing --cov-report=html
```

**No `fail_under` threshold, deliberately.** A number that must be met gets met
by covering whatever is cheapest — serializers and `__str__` methods — while
corridor timing arithmetic and redaction rules stay untested because they are
harder. Coverage here is a map of what has not been looked at, not a gate.

---

## CI

`.github/workflows/ci.yml`, ordered cheapest-first so a broken build fails in
seconds rather than after a browser download.

| Job | Runs |
|---|---|
| `checks` | `manage.py check`, `check --deploy` under production settings, `makemigrations --check` |
| `rbac` | `pytest -m rbac` — split out and early; an authorisation regression must never reach a review queue unnoticed |
| `backend` | `pytest --cov` **and** `manage.py test apps` |
| `frontend` | `tsc --noEmit`, `vitest run`, `vite build` |
| `e2e` | Playwright, needs backend + frontend |

`makemigrations --check` earns its place: a model change without a migration
passes every test and fails on the first deploy. `check --deploy` runs with
`DEBUG=0` so a setting that only breaks in production breaks in CI instead.

Playwright retries twice in CI and **zero times locally**. A test that only
passes on retry is flaky, and a flaky E2E suite gets ignored — which is worse
than not having one.

---

## Verification

```
pytest                             721 passed, 3 skipped, 123 subtests
pytest -m rbac                     306 passed, 418 deselected
python manage.py test apps         418 tests, OK (3 skipped)
npx vitest run                     33 passed
npx playwright test                34 passed, 2 skipped
npx tsc --noEmit && npx vite build clean
manage.py makemigrations --check   no changes detected
```

The two skipped Playwright tests are KPI-tile assertions that skip when the
seeded window has too little history to compare halves — the same guard the
`/analytics/trends/summary/` endpoint applies.
