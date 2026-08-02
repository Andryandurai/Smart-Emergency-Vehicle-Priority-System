# SEVPS — Technical Handover Documentation

**Smart Emergency Vehicle Priority System**
Document set version 1.0 · Covers the platform as of the completion of the
twelve-phase modernisation.

---

## Who this is for

A development team that has never seen this project. Everything needed to
maintain, operate and extend the platform is in these nine parts. No prior
context is assumed.

If you read only one thing before touching code, read **Part 1 §2 (System
Architecture)** and **Part 7 §16 (Code Walkthrough)** — between them they
explain both the shape of the system and the order in which execution moves
through it.

---

## Document set

| Part | File | Sections | Subject |
|---|---|---|---|
| 0 | *this file* | — | Index, conventions, quick start |
| 1 | [PART-1-OVERVIEW-AND-ARCHITECTURE.md](PART-1-OVERVIEW-AND-ARCHITECTURE.md) | §1–2 | Purpose, scope, users, and the complete architecture with diagrams |
| 2 | [PART-2-STACK-STRUCTURE-DATABASE.md](PART-2-STACK-STRUCTURE-DATABASE.md) | §3–5 | Technology stack, folder structure, database and ERD |
| 3 | [PART-3-BACKEND.md](PART-3-BACKEND.md) | §6 | Every Django app, model, serializer, view, service, command, consumer |
| 4 | [PART-4-FRONTEND-AND-API.md](PART-4-FRONTEND-AND-API.md) | §7–8 | Every page, component, hook, store; complete API reference |
| 5 | [PART-5-AUTH-AI-CV-GIS.md](PART-5-AUTH-AI-CV-GIS.md) | §9–12 | Authentication and RBAC, the AI module, computer vision, GIS |
| 6 | [PART-6-REALTIME-NOTIFICATIONS-DASHBOARDS.md](PART-6-REALTIME-NOTIFICATIONS-DASHBOARDS.md) | §13–15 | WebSockets, notifications, every dashboard and widget |
| 7 | [PART-7-WALKTHROUGH-AND-FLOWS.md](PART-7-WALKTHROUGH-AND-FLOWS.md) | §16–17, §23 | File-by-file walkthrough, traced request flows, full execution flow |
| 8 | [PART-8-DEPLOYMENT-TESTING-PERFORMANCE-SECURITY.md](PART-8-DEPLOYMENT-TESTING-PERFORMANCE-SECURITY.md) | §18–21 | Docker, Nginx, cloud, test strategy, performance, security |
| 9 | [PART-9-FUTURE-CHANGES-PACKAGES-SUMMARY.md](PART-9-FUTURE-CHANGES-PACKAGES-SUMMARY.md) | §22, §24–26 | Roadmap, file change report, package report, handover guides |

### Existing subject documents

Written during development and still current. The handover parts supersede
none of them; they go deeper on single subjects.

| File | Subject |
|---|---|
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | Original six-layer design |
| [../AUTHENTICATION.md](../AUTHENTICATION.md) | JWT and RBAC detail |
| [../DATABASE.md](../DATABASE.md) | Schema and PostGIS migration |
| [../REALTIME.md](../REALTIME.md) | Channels and consumer detail |
| [../AI_AND_VISION.md](../AI_AND_VISION.md) | ML estimators and the CV pipeline |
| [../GIS.md](../GIS.md) | Layer registry and basemaps |
| [../NOTIFICATIONS.md](../NOTIFICATIONS.md) | Web Push and FCM |
| [../ANALYTICS.md](../ANALYTICS.md) | Charts, series and exports |
| [../DEPLOYMENT.md](../DEPLOYMENT.md) | Containers, Nginx and cloud |
| [../TESTING.md](../TESTING.md) | Test layers and what each catches |
| [../FRONTEND.md](../FRONTEND.md) | Console structure |
| [../TECHNOLOGY_STACK.md](../TECHNOLOGY_STACK.md) | Stack rationale |

---

## Quick start

```bash
# 1. Backend, zero configuration — SQLite, in-memory channel layer
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_users          # creates one account per role
python manage.py seed_demo           # Chennai road network, hospitals, fleet
python manage.py runserver           # ASGI via Daphne; http://127.0.0.1:8000/

# 2. Console (separate terminal)
cd frontend && npm install && npm run dev      # http://127.0.0.1:5173/

# 3. Optional: make something happen
python manage.py simulate --trips 3            # moving ambulances
python manage.py sevps_worker                  # corridor sweep, CV, rollups
```

**Demo accounts** (`seed_users`; passwords are in the source and must be
changed before any reachable deployment):

| Username | Password | Role |
|---|---|---|
| `admin` | `sevps-admin` | Administrator (superuser, Django admin) |
| `police` | `sevps-police` | Traffic Police |
| `dispatcher` | `sevps-dispatcher` | Emergency Dispatcher |
| `paramedic` | `sevps-paramedic` | Ambulance Driver |
| `hospital` | `sevps-hospital` | Hospital Staff |
| `public` | `sevps-public` | Public User |
| `operator` | `sevps-operator` | Legacy alias → Traffic Police |

---

## Conventions used throughout

**Coordinates.** SEVPS APIs return `[lat, lon]` everywhere **except** GeoJSON
endpoints, which follow RFC 7946 and return `[lon, lat]`. Leaflet wants
`[lat, lon]`. Exactly one function converts —
`toLatLng()` in `frontend/src/components/map/layers.ts`. Getting this wrong
renders an empty map rather than a visibly wrong one.

**Roles.** Never hard-code a group name. Import from `apps.core.roles`:
`Role.TRAFFIC_POLICE`, not `"traffic_police"`. Legacy group names
(`operators`, `paramedics`, `hospital`) are aliased, not renamed.

**Permissions.** Every DRF view declares `permission_classes` explicitly.
`apps/core/api_policy.py` audits this at test time and fails the build on an
endpoint that relies on the global default or that is public without an
allowlist entry carrying a written reason.

**Fail-soft fan-out.** Publishing to WebSockets or push must never abort a
dispatch decision. Both paths are wrapped and log rather than raise.

**Times.** Server timezone is `Asia/Kolkata` by default. Analytics profiles are
keyed in **local** time; a UTC-keyed demand profile shifts every peak by 5½
hours and still looks plausible.

---

## Current state, honestly

| | |
|---|---|
| Backend tests | 721 passing + 123 subtests (3 skipped: PostGIS-only) |
| Frontend unit tests | 33 passing |
| End-to-end tests | 34 passing, 2 conditionally skipped |
| Type checking | `tsc --noEmit` clean |
| Production build | clean; main chunk 256 kB, charts lazy at 437 kB |
| Django deploy check | clean except one deliberate HSTS-preload warning |

**Two things have never been executed**, because they need software that was
not installed on the development machine and installing it was not authorised:

1. `docker compose build` / `up`. The deployment artifacts are validated by 30
   automated tests that parse them, which found two real defects, but no image
   has been built.
2. The PostgreSQL/PostGIS cutover. `manage.py migrate_to_postgres` is written
   and its dump path verified against fixtures; it has never run against a
   live PostgreSQL server.

Both are the first items on the roadmap in Part 9.
