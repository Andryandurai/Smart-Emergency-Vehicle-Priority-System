# Notifications & Push (Phase 9)

## The gap this phase closed

Before Phase 9 a notification was a WebSocket message and nothing else. Two
consequences, both bad for an emergency platform:

1. **No open tab, no notification.** A hospital away from the desk at 02:14 was
   never told about an inbound Level 1. The message was broadcast into a
   channel nobody was listening on and ceased to exist.
2. **No record.** Nobody could answer "was the hospital told?" afterwards —
   not during a shift handover and not during an incident review.

Phase 9 adds a second delivery path and a durable record, without touching the
first. Every existing `notification` socket event still fires, in the same
shape, at the same moment.

---

## Architecture

```
                        apps/core/notifications.py
                                publish()
                                    │
                ┌───────────────────┴───────────────────┐
                │                                       │
        WebSocket fan-out                    apps/notify/service.py
        (unchanged, instant)                 publish_and_deliver()
                │                                       │
        open dashboards                    ┌────────────┴────────────┐
                                           │                         │
                                  NotificationRecord         resolve audience
                                  (durable history)           by ROLE
                                                                     │
                                                          ┌──────────┴──────────┐
                                                     Web Push               FCM
                                                     (primary)          (optional)
                                                          │                  │
                                                          └────────┬─────────┘
                                                          NotificationDelivery
                                                            (receipts)
```

The push hop is **additive and fail-soft**. `publish()` wraps it in a
`try/except`, and `publish_and_deliver()` swallows its own failures. A dispatch
decision cannot fail because a push service is down.

---

## Web Push is primary; FCM is an adapter

The brief listed **FCM + Web Push**. The recommendation delivered here — and
implemented — is to make **Web Push (VAPID) the default** and keep FCM as an
optional adapter. Three reasons:

**1. It removes a single vendor from the delivery path of emergency alerts.**
Web Push is delivered by whichever push service the *browser* already uses —
Mozilla's for Firefox, Apple's for Safari, Google's for Chrome. With FCM as the
transport, one vendor's outage or policy change takes alerting offline for
every user at once. Spreading that across three independent services is the
single biggest availability win in this phase.

**2. For browsers, FCM duplicates what SEVPS already has.** FCM's web SDK is a
wrapper over the same Web Push transport, plus a JS SDK, plus a Google Cloud
project. The SEVPS console is a browser application. Nothing is gained.

**3. Setup cost differs by an order of magnitude.** Web Push:
`python manage.py generate_vapid_keys`. FCM: a Google Cloud project, a Firebase
app, a service-account key, and a credentials file on every server.

**Where FCM genuinely wins** is the case Web Push cannot serve: a **native
Android driver app**, which holds an FCM registration token rather than a W3C
subscription. That is a real target for Layer 4 driver alerts, so the adapter
exists and shares all the delivery, retry and receipt machinery. Set
`SEVPS_FCM_CREDENTIALS` and native tokens deliver alongside browsers with no
other change.

> **Verdict: integrate, do not replace.** Web Push stays primary. FCM is a
> transport a subscription can *declare*, chosen per row, not globally.

---

## Setup

```bash
pip install pywebpush
python manage.py generate_vapid_keys      # writes models/vapid_private.pem
```

That is the whole setup. For a containerised deployment with a read-only
filesystem:

```bash
python manage.py generate_vapid_keys --print-only
# then set SEVPS_VAPID_PRIVATE_KEY / SEVPS_VAPID_PUBLIC_KEY in the environment
```

| Setting | Default | Purpose |
|---|---|---|
| `SEVPS_VAPID_PRIVATE_KEY` | — | PEM, overrides the key file. `\n` escapes are handled. |
| `SEVPS_VAPID_PUBLIC_KEY` | derived | Only needed if the private key is withheld. |
| `SEVPS_VAPID_KEY_PATH` | `models/vapid_private.pem` | Key file location. |
| `SEVPS_VAPID_SUBJECT` | `mailto:ops@sevps.local` | RFC 8292 contact URI. **Change this.** |
| `SEVPS_FCM_CREDENTIALS` | — | Path to a Firebase service-account JSON. Enables FCM. |
| `SEVPS_PUSH_DRIVER_ALERTS` | `1` | Push Layer 4 warnings to road-user devices. |

### Rotating keys invalidates every subscription

A push service binds a subscription to the key that created it. Regenerating
means every controller, hospital and crew must re-grant notification
permission — and they discover this only when an alert fails to arrive. So
`generate_vapid_keys` refuses to overwrite without `--force`, and prints a
warning when forced.

### Push requires HTTPS

`localhost` is exempt for development. In production, a console served over
plain HTTP cannot register a service worker and push silently does not exist.
The UI reports this as `insecure` rather than as a generic failure.

---

## What is stored, and why

| Model | Answers | Why it exists |
|---|---|---|
| `PushSubscription` | *Where* a person can be reached | One row per browser/device. A controller with a desk machine and a phone has two. |
| `NotificationRecord` | *What* was said | Survives the socket. Powers the inbox and any incident review. |
| `NotificationDelivery` | *Whether it arrived* | A notification that silently failed is worse than one never sent — everyone believes it got through. |
| `NotificationPreference` | *What someone opted out of* | A mute list, not a subscribe list. |

### The endpoint is the identity

`PushSubscription.endpoint` is unique, and subscribe uses `update_or_create`.
Without that, a user who reloads with permission already granted collects a new
row per reload and receives one copy of every notification per reload.

The endpoint is also a **bearer capability** — anyone holding it can push to
that browser — so it never leaves the server after registration. The
subscriptions API returns `endpoint_hint` (last 12 characters), enough to tell
two devices apart. This is asserted by `test_endpoint_is_never_returned_in_full`.

### 404/410 retires a subscription immediately

That status is the push service saying the endpoint no longer exists — the user
cleared site data or uninstalled. It is authoritative, so the row is
deactivated at once. Transient failures (timeouts, 5xx) tolerate five
consecutive attempts, because the fan-out is synchronous and every dead
endpoint costs a request with its timeout on every notification.

---

## Push payloads carry no clinical data

A push payload travels through a third-party relay and is readable by anyone
holding the unlocked device. So it carries only enough to decide whether to
act: **who, how urgent, and where to look.**

```json
{
  "id": "…", "title": "Inbound cardiac arrest",
  "body": "AMB-3 en route to Apollo. Priority level 1.",
  "severity": "critical", "category": "inbound_patient",
  "link": "/hospital/APL", "dedupe_key": "inbound:412",
  "issued_at": "2026-08-02T02:14:09+05:30"
}
```

`context` is dropped entirely — it holds trip internals, and on the
hospital-prepare path it is the field carrying patient identifiers.
`test_push_payload_carries_no_clinical_or_internal_context` asserts this, and
`test_driver_alert_push_stays_anonymous` asserts the road-user path never
leaks the trip reference either.

---

## Audience resolution: by role, never by user list

```python
Notification(audience=(Role.HOSPITAL, Role.DISPATCHER))
```

Who holds those roles is resolved **at send time** through Django groups, so a
nurse added to the group this morning is reached this afternoon with no resync
step. Legacy group names still work: `group_names_for()` (added to
`apps/core/roles.py` in this phase) is the inverse of `canonical()`, so a
deployment still on the `operators` group keeps receiving traffic-police
alerts.

Three deliberate rules:

- **An empty audience means every operational role** — the same meaning it
  already had for the WebSocket fan-out.
- **It never includes anonymous devices.** A road user must not receive
  "corridor preemption failed at TSC-114". Driver alerts are a separate,
  geographic path.
- **Superusers receive everything**, matching `user_roles()`.

`MAX_FANOUT = 500` caps recipients per notification. A city-wide hazard alert
could otherwise turn one API call into ten thousand inline HTTPS requests.
Truncation is logged and reported in the result — silent truncation reads as
"everyone was told".

---

## Categories and preferences

Severity says *how loud*; category says *what subject*. People mute by subject.

Existing call sites did not have to declare a category: `infer_category()`
derives it from the `dedupe_key` prefixes the notification helpers already set
(`inbound:` → inbound patient, `corridor-fail:` → corridor, `escalation:` →
priority, `reroute:` → route, `no-hospital:` → dispatch).

**Preferences are a mute list, not a subscribe list.** A category added by a
later release reaches everyone by default. A subscribe list fails silent, which
for operational alerting is the wrong direction.

### Critical alerts cannot be muted

```python
if severity == Severity.CRITICAL:
    return True   # before push_enabled, before categories, before quiet hours
```

A hospital cannot mute "inbound Level 1"; a controller cannot mute "corridor
failed". Muting exists so the important messages stay visible, not so they can
be switched off. The API says so explicitly (`critical_always_delivered: true`)
and the settings UI prints the rule — a checkbox that silently refuses to take
effect is worse than no checkbox.

---

## Endpoints

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /api/v1/notify/vapid-key/` | public | Public key; required before the permission prompt can be shown |
| `POST /api/v1/notify/subscribe/` | public | Register a browser or road-user device |
| `POST /api/v1/notify/unsubscribe/` | public | Retire a subscription |
| `GET /api/v1/notify/subscriptions/` | role | The caller's own devices |
| `GET/PATCH /api/v1/notify/preferences/` | role | Mutes and quiet hours |
| `GET /api/v1/notify/inbox/` | role | Last 24 h, filtered by the caller's roles |
| `POST /api/v1/notify/read/[<uuid>/]` | role | Mark one or all read |
| `POST /api/v1/notify/test/` | role | Test push to the caller's own devices only |
| `GET /api/v1/notify/health/` | role | Whether push can actually deliver |

### A new permission class: `PublicDeviceRegistration`

Subscribe and unsubscribe are POSTs, and `PublicRead` is read-only. Rather than
reach for `AllowAny` — which the policy audit cannot distinguish from a
forgotten declaration — Phase 9 adds a named class to
`apps/core/permissions.py`:

> Public, and deliberately writable — **device self-registration only.** The
> caller can only ever describe *itself*: a push endpoint the browser just
> minted, and a coarse position. It names no other subject, reads nothing back,
> and grants no access to platform state.

It is included in the audit's public set, so both endpoints still require an
allowlist entry in `api_policy.py` with a written reason. Verified live:

```
POST /notify/subscribe/    anonymous → 201     (Layer 4 promise held)
POST /notify/subscribe/    repeat    → 200     (idempotent, no duplicate row)
GET  /notify/subscribe/    anonymous → 405     (write-only, not readable)
GET  /notify/inbox/        anonymous → 401
GET  /notify/preferences/  anonymous → 401
GET  /notify/health/       anonymous → 401
GET  /notify/subscriptions/ anonymous → 401
```

### Why `/notify/health/` is separate from `/api/v1/health/`

The answers differ in kind. The platform can be perfectly healthy while push
has been silently undeliverable since the day nobody generated a key.
Reporting zero configured keys as "healthy" is exactly the failure this
endpoint exists to prevent — so it reports `webpush_configured`, active
subscription counts and a 24-hour delivery rate.

---

## The service worker

`static/js/sw.js`, served by Django at **`/sw.js`** (see
`apps/dashboards/views.py:service_worker`).

It has to be at the root: **a service worker's scope cannot rise above its own
URL.** Bundled as a Vite asset it would live under `/static/` and control
`/static/` — the one part of the site with no pages in it — and would never
receive a push for the console. `Service-Worker-Allowed: /` and
`Cache-Control: no-cache` are set for the same class of reason: a stale worker
keeps delivering with old logic long after a deploy, with no visible symptom.
In dev, Vite proxies `/sw.js` to Django, so there is one copy and dev push
behaves like production.

What it does:

- **Collapses on `dedupe_key`.** Three reroutes of one trip are one story. Without
  collapsing, a crew gets three banners for one situation and learns to swipe.
- **`requireInteraction` for critical only.** A Level 1 alert stays on screen
  until acted on; everything else follows the platform's dismissal timing.
- **`postMessage`s open tabs**, so the in-app notification centre updates
  immediately rather than at its next poll.
- **Focuses an existing tab** on click rather than opening a fourth console.
- **Handles `pushsubscriptionchange`** by re-subscribing and re-registering.
  Without it a push service invalidation silently stops all alerts while the
  browser still shows permission granted.

It deliberately **does not cache the application**. SEVPS shows live
operational state; a stale cached dashboard that looks current is worse than
one that fails to load.

---

## Frontend

```
src/lib/push.ts                       subscribe / unsubscribe, typed failure states
src/hooks/usePushNotifications.ts     worker + subscription + store, wired
src/stores/notifyStore.ts             three sources reconciled into one list
src/components/NotificationCentre.tsx bell, panel, push toggle (in the shell)
src/components/NotificationSettings.tsx preferences and devices (on /settings)
```

**Every push function returns a typed state, not a boolean.** The failure chain
is long — secure context, worker registration, permission, VAPID key,
subscribe, server registration — and each link has a different remedy. "Push is
off" is not actionable; "notifications are blocked for this site in browser
settings, and the page cannot ask again once blocked" is.

**Three sources feed one list**, which is why a store exists rather than
component state: the REST inbox (history), the WebSocket `notification` event
(instant, tab open), and `postMessage` from the service worker (tab
backgrounded). All three can carry the same notification; they are reconciled
on `uuid` then collapsed on `dedupe_key`.

**Subscribe rolls back on server rejection.** If the browser holds a
subscription the server does not know about, the UI reads "enabled" and nothing
is ever delivered. **Unsubscribe tells the server first**, for the mirror
reason.

The notification centre lives in `AppShell`, not on a page — the point of a
notification is that it reaches someone looking at something else. It is
rendered only for signed-in users; road users get alerts through `/driver`.

---

## Layer 4: push for road users

`broadcast_driver_alerts()` now also pushes, via `_push_to_subscribed_drivers()`.

Two constraints:

- **Only newly issued alerts push.** A refresh updates the ETA for an existing
  cell; the socket carries that to anyone watching, but buzzing a phone every
  few seconds for one approaching ambulance is how a safety channel gets muted
  permanently.
- **The message is anonymous.** A road user is told an ambulance is approaching
  and which lane to leave — never which trip, which hospital, or which patient.

Targeting is by geohash cell (~1.2 km), the same sharding the WebSocket
driver groups already use. Only the cell is stored: delivering a warning needs
a neighbourhood, not a location history.

---

## What changed in existing files

| File | Change | Risk |
|---|---|---|
| `apps/core/notifications.py` | `publish()` also persists + pushes | none — wrapped in try/except, return shape unchanged plus `id`/`category` |
| `apps/core/roles.py` | new `group_names_for()` | none — additive |
| `apps/core/permissions.py` | new `PublicDeviceRegistration` | none — additive |
| `apps/core/api_policy.py` | audit recognises the new class; 3 allowlist entries | none — audit still fails on undeclared endpoints |
| `apps/alerts/dispatcher.py` | driver alerts also push | none — fail-soft, returns an extra `pushed` key |
| `apps/dashboards/views.py`,`urls.py` | `/sw.js` route | none — precedes the SPA catch-all |
| `sevps/settings.py` | `apps.notify`, 6 SEVPS keys | none |
| `sevps/api_urls.py` | `notify/` include | none |
| `frontend/.../AppShell.tsx` | notification bell | additive |
| `frontend/.../SettingsPage.tsx` | notification settings section | additive |
| `frontend/.../OperationsPage.tsx` | socket notifications also feed the store | additive |
| `frontend/vite.config.ts` | `/sw.js` proxy | dev only |

**No existing notification, socket event, endpoint or serializer changed shape.**
`publish()` returns the same dict it always did, with two keys added.

---

## Verification

```
python manage.py test apps.notify         # 59 tests
python manage.py test apps                # 338 tests, OK (3 skipped)
cd frontend && npx vitest run             # 27 tests
cd frontend && npx tsc --noEmit && npx vite build
```

Live probes (server on :8011, admin JWT) confirmed: VAPID key served, `/sw.js`
with correct scope header, anonymous subscribe 201 / idempotent 200 / GET 405,
all four role-gated endpoints 401 anonymously, a real corridor notification
persisted with `category=corridor` inferred, the returned `id` matching the
stored UUID, mark-read clearing the badge, and a **hospital-role user's inbox
correctly excluding a traffic-police notification**.

## Operations

```bash
python manage.py push_sweep --dry-run     # what would be retired
python manage.py push_sweep --days 60     # retire silent subscriptions
```

Worth scheduling. Dead endpoints are not free: the fan-out is synchronous, so
each retired browser still in the table adds a doomed request, with its
timeout, to every notification it would have matched.
