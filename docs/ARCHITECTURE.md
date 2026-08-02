# SEVPS Architecture

## Layer map

```
                        ┌──────────────────────────────────────────┐
   Paramedic app  ─────►│  LAYER 1  Emergency Vehicle Tracking     │
   Onboard unit         │  apps/fleet                              │
                        │  GPS · speed · heading · status · ETA    │
                        └────────────────┬─────────────────────────┘
                                         │ every fix
                                         ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  LAYER 2   AI Traffic Intelligence Engine     apps/brain (stateless)│
   │                                                                     │
   │   graph.py       cached NetworkX topology + live edge state         │
   │   congestion.py  live ⊕ historical blend, horizon-decayed  ◄── ML   │
   │   router.py      time-dependent A* / Dijkstra                       │
   │   eta.py         route projection, per-intersection arrival times   │
   │   corridor.py    which signals to hold, and exactly when            │
   │   rerouting.py   blocked / off-route / better-path detection        │
   │   priority.py    ranking when vehicles compete                      │
   └───┬──────────────┬───────────────┬──────────────┬───────────────────┘
       │              │               │              │
       ▼              ▼               ▼              ▼
 ┌───────────┐  ┌───────────┐  ┌────────────┐  ┌──────────────┐
 │  LAYER 3  │  │  LAYER 4  │  │  LAYER 5   │  │  LAYER 6     │
 │  Signal   │  │  Driver   │  │  Hospital  │  │  Siren &     │
 │  control  │  │  alerts   │  │  engine    │  │  lights      │
 │           │  │           │  │            │  │              │
 │ dispatch/ │  │ alerts/   │  │ hospitals/ │  │ dispatch/    │
 │ corridor  │  │dispatcher │  │ rules.py   │  │ siren.py     │
 │controllers│  │           │  │recommender │  │              │
 └─────┬─────┘  └─────┬─────┘  └──────┬─────┘  └──────┬───────┘
       │              │               │               │
       ▼              ▼               ▼               ▼
  Traffic       Mobile app       Hospital        Onboard
  controllers   Nav apps         dashboard       light/siren
  (NTCIP/REST/  VMS boards       (4.7)           controller
   WS bridge)   City displays
       │              │               │               │
       └──────────────┴───────┬───────┴───────────────┘
                              ▼
              ┌───────────────────────────────────┐
              │  Emergency Operations Dashboard   │  4.11
              │  AI Traffic Analytics + Hotspots  │  4.8 / 4.9
              └───────────────────────────────────┘

   World model (apps/network): intersections · road segments · signals ·
   cameras · traffic observations · learned profiles · road events · accidents
```

## Request paths

### The hot path — one GPS fix

`POST /api/v1/fleet/vehicles/{id}/telemetry/` → `orchestrator.on_vehicle_position()`

1. `EmergencyVehicle.record_position()` — derives speed/heading if the device omitted them,
   writes the breadcrumb.
2. `brain.eta.compute_progress()` — projects the fix onto the planned route polyline; flags
   off-route beyond 90 m.
3. `brain.eta.eta_for_trip()` — remaining time from per-step timings, not average speed.
4. `dispatch.corridor.sync_corridor()` — upsert plan, resolve junction conflicts, send
   green commands that are due, release junctions already cleared.
5. `alerts.dispatcher.broadcast_driver_alerts()` — place alerts ahead on the route, fan out
   by geohash cell.
6. `brain.rerouting.evaluate_trip()` — replan if blocked, off-route, or materially faster.
7. `dispatch.siren.apply_priority()` — re-derive level from stage and clinical condition.
8. Geofence arrival check → stage transition; hospital dashboard push.

### The decision path — paramedic assessment

`POST /api/v1/dispatch/trips/{id}/assess/` → `orchestrator.assign_hospital()`

1. `hospitals.rules.resolve_rule(category)` — mandatory facilities, priority level, golden
   window, crew guidance.
2. `hospitals.recommender.recommend_hospital()` — hard filter → route the shortlist →
   weighted score → explain.
3. `HospitalRecommendationLog` — full candidate list and scores persisted for governance.
4. `siren.apply_priority()` — Layer 6 sets lights, siren and signal entitlement.
5. `orchestrator.plan_route_to()` — new route from the current position.
6. `orchestrator.notify_hospital()` — durable pre-arrival alert + live socket push.
7. `sync_corridor()` — green corridor arms against the new route.

## Data ownership

| App | Owns | Read by |
|---|---|---|
| `core` | Geodesy, enums, base models, realtime helpers, permissions | everything |
| `network` | Intersections, segments, signals, cameras, observations, profiles, events, accidents | `brain`, `dispatch`, `analytics` |
| `fleet` | Vehicles, stations, telemetry | `dispatch`, `brain`, `analytics` |
| `brain` | *(nothing — stateless by design)* | — |
| `hospitals` | Hospitals, capabilities, capacity, rule base, alerts, recommendation log | `dispatch`, `dashboards` |
| `dispatch` | Trips, route plans, signal preemptions, priority directives | `brain`, `alerts`, `analytics` |
| `alerts` | Display boards, driver devices, driver alerts | `dashboards` |
| `analytics` | Daily metrics, hotspots | `dashboards` |
| `dashboards` | *(nothing — renders other apps' state)* | — |

The dependency graph is acyclic at the data layer. Where behaviour genuinely is cyclic
(`dispatch` calls `brain`, `brain.rerouting` calls back into `dispatch.orchestrator`), the
call is a deferred import inside the function, documented at the call site.

## Caching

| Cache | Lifetime | Invalidated by |
|---|---|---|
| Graph topology (`brain.graph`) | 60 s | `graph.invalidate()` after network edits/import |
| Live edge state (`brain.graph`) | 5 s | TTL only — this is the fast-moving part |
| Congestion profiles | per request | rebuilt per forecaster |

Topology changes when an operator edits the network; traffic state changes every few
seconds. Splitting the two means a route query costs one small state read rather than a full
graph rebuild.

## Failure behaviour

| Failure | Response |
|---|---|
| Signal controller unreachable | Preemption marked `failed`, junction keeps normal timing, controller marked offline |
| Vehicle loses GPS mid-corridor | `tick_corridors()` force-releases the hold at its cap |
| Route cannot be computed | `route_failed` on the ops dashboard; the trip keeps its previous plan |
| No capable hospital in range | Tiered relaxation with visible warnings; diversion never overridden |
| Every hospital on diversion | No recommendation — escalated to the control room, not forced |
| Channel layer down | `broadcast()` swallows and logs; dispatch decisions still commit |
| Event raised in another process (in-memory layer) | Not pushed — dashboards poll every 2–4 s as fallback; set `SEVPS_REDIS_URL` for instant cross-process push |
| Concurrent writers on SQLite | WAL mode + 20 s busy timeout; contention becomes a wait, not a `database is locked` failure |
| Congestion model unloadable | Falls back to the statistical predictor, reported by `/api/v1/info/` |
| Camera feed unreadable | Falls back to simulated analysis for that camera only |
