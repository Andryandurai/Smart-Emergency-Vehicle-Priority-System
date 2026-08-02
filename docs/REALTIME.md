# SEVPS Real-Time Layer (Phase 5)

## Why this phase existed

Phase 2 gave the WebSocket layer *authentication*. Probing the running server showed it
never got *authorisation*, and two of the gaps were live:

```
$ wscat ws://127.0.0.1:8000/ws/hospital/APOLLO/     # no credentials
  inbound patients visible: 2
    SEV-...-0015  category='stroke'  age=48  notes='suspected stroke'
    SEV-...-0014  category='cardiac' age=55  notes='suspected cardiac'
```

**1. The hospital socket served PHI to anonymous connections.** Its snapshot built the
inbound list from `EmergencyTrip.as_hospital_payload()` — a model method that predates the
Phase 2 clinical redaction and therefore bypassed it entirely. The REST path was fixed;
this one was never routed through the serializer.

**2. The signals bridge accepted commands from anonymous clients.** `phase_report` writes
`TrafficSignal.current_phase`, `is_online` and `last_heartbeat`. Anyone could mark
controllers online or set their phase.

The failure mode is specific to sockets: a REST view without a permission class is obvious
in review, a consumer has no equivalent, and its snapshot method often bypasses the
serializer that would have redacted the payload.

## The fix: declared socket policies

Every consumer declares a `ConsumerPolicy` ([apps/core/ws_policy.py](apps/core/ws_policy.py))
and the base class enforces it before accepting. A consumer with no policy **fails closed**.

| Socket | anon | police | hospital | paramedic | dispatcher |
|---|:--:|:--:|:--:|:--:|:--:|
| `/ws/ops/` | ✗ | ✓ | ✓ | ✓ | ✓ |
| `/ws/hospital/<code>/` | ✗ | **✗** | ✓ | ✓ | ✓ |
| `/ws/vehicle/<callsign>/` | ✗ | ✓ | ✗ | ✓ | ✓ |
| `/ws/signals/` | ✗ | ✓ | ✗ | ✗ | ✗ |
| `/ws/drivers/` | **✓** | ✓ | ✓ | ✓ | ✓ |

Verified live, not asserted. Two entries are deliberate and worth reading twice:

- **Traffic police cannot open the hospital feed.** Running a green corridor needs a
  vehicle's priority and position, never a diagnosis. Same data-minimisation rule as the
  REST redaction, applied at connection time because the hospital group broadcast is
  unredacted by design — every member holds clinical clearance.
- **`/ws/drivers/` stays anonymous.** Layer 4's core promise. It carries a warning, an ETA
  and a bearing — no patient or vehicle identity — and a road user must not need an account
  to be told an ambulance is coming.

Refusals use `4401` (sign in) and `4403` (wrong role) so a client can tell them apart. The
console renders them as *"sign in required"* / *"not permitted"* and **stops reconnecting** —
a backoff loop against a socket that will always refuse is just noise in the server log.

### The audit

`tests_ws_policy.py` fails the build if any consumer:

- is registered without a policy,
- carries clinical data while allowing anonymous access *(the exact defect found)*,
- accepts commands while allowing anonymous access (drivers is the recorded exception),
- allows a non-clinical role onto the hospital socket,
- is public without a written reason.

## Transport improvements

**Sequencing.** Every frame carries a monotonic `seq` and a `ts`. A client that sees a gap
knows it missed frames; the console logs *"missed live frames — resyncing"* and refetches
rather than rendering a board that quietly stopped updating.

**Coalescing.** `vehicle_position` collapses to the latest value **per callsign** inside a
0.9 s window. Nine vehicles at 2 s is nothing; a city fleet at 1 s across fifty dashboards
is not. Per-key, so one vehicle never suppresses another.

**Selective subscription.** `{"type": "subscribe", "filters": {"event": [...], "callsign": [...]}}`
narrows a socket. Filtering at the consumer rather than the publisher keeps fan-out simple
and means one client's preferences cannot affect another's.

**Explicit read-only rejection.** A command sent to a read-only socket returns
`{"event": "error", "data": {"detail": "this socket is read-only"}}` instead of being
silently dropped, so a client is not left wondering why nothing happened.

## Live streams (the Phase 5 feature list)

| Requirement | How |
|---|---|
| Live ambulance tracking | `vehicle_position`, coalesced per callsign |
| **Live ETA** | **new** — `eta_update` from the worker's sweep |
| **Live traffic** | **new** — `traffic_update`, changed segments only |
| Signal updates | `signal_preempted` / `signal_released` |
| Hospital status | `inbound_update`, `capacity_updated`, `hospital_alert` |
| **Live notifications** | **new** — unified `notification` event |

Three things change without any vehicle moving, and previously only reached a screen on the
next telemetry tick — or never ([apps/core/live.py](apps/core/live.py)):

**ETA decays continuously.** A vehicle stopped in traffic has a *worse* ETA every second,
and that is exactly when a hospital most wants to know. Waiting for the next GPS fix means
the number a hospital reads is optimistic precisely when it matters. Pushed only when it
moves by more than 15 s — a one-second drift on a twelve-minute run is noise, and noise
costs bandwidth on every open dashboard.

**Traffic changes on the worker's schedule**, not the fleet's. Only *changed* segments are
published; broadcasting the whole network every few seconds would swamp the channel layer
to tell every client what it already knows.

**Silence is information.** A unit that stops reporting mid-corridor is holding signals
green for a position nobody can confirm. The corridor sweep releases the hold on its own
timer; `fleet_health` makes the cause visible to the control room.

### Notifications vs. state events

Two different things were being conflated ([apps/core/notifications.py](apps/core/notifications.py)):

- **State events** — the map moved, the ETA changed. Frequent, disposable, only meaningful
  to a screen already rendering that state.
- **Notifications** — a hospital must prepare a bay; a corridor failed. Infrequent,
  individually important, worth surfacing even to a screen showing something else.

Only the second kind goes on the notification stream, routed by role and interest so the
publisher never needs to know which dashboards are open. Each carries a `dedupe_key` so a
client can supersede rather than stack — three reroutes of one trip is one story, not three.

## Redis

The in-memory channel layer is per-process, so events published by `sevps_worker` or
`simulate` never reach a socket held by the web process. The console polls as the
correctness floor and the socket is the latency improvement.

```bash
pip install channels-redis==4.2.0
export SEVPS_REDIS_URL=redis://127.0.0.1:6379/0
```

Setting `SEVPS_REDIS_URL` without the driver now **raises at startup** rather than falling
back silently — a deployment that looks configured for multi-process fan-out while every
worker event vanishes is worse than one that refuses to boot.

Layer capacity is bounded (`SEVPS_CHANNEL_CAPACITY`, default 500). A dashboard that falls
behind must not stall the publisher; dropping frames is recoverable precisely because
clients detect the sequence gap and resync.

`docker compose up -d redis` provides it. `GET /api/v1/info/` reports which layer is live,
and the Settings screen explains the consequence inline when it is in-memory.

## Verified

```
185 backend tests (21 new)                                  pass
19 frontend tests, strict typecheck, production build       pass

Live probe against the running server:
  anonymous → ops / hospital / vehicle / signals            all REFUSED
  anonymous → drivers                                       open, 9 geohash cells
  police    → hospital feed                                 REFUSED (data minimisation)
  hospital  → hospital feed                                 open, patient age visible
  frame envelope                                            seq 1 → 2, ts present
  subscription filter                                       acknowledged
  read-only socket sent a command                           "this socket is read-only"
  SEVPS_REDIS_URL set without the driver                    refuses to boot
```

## Not done in this phase

- **Resume-from-sequence.** Clients detect a gap and refetch. Replaying missed frames would
  need a per-group ring buffer in Redis; refetching is simpler and, for state that is
  already snapshot-shaped, equivalent.
- **Presence.** No "who is watching" view. Straightforward with Redis, no value without it.
- **Per-socket rate limiting on inbound commands.** Telemetry is authenticated and bounded
  by the fleet size; worth adding before any public write path exists.
