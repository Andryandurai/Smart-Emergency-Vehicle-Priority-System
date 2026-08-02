"""Phase 9 tests: push registration, audience resolution, delivery receipts.

The tests that matter most are the negative ones. Push has a long failure
chain and every link fails quietly, so the properties worth pinning are:
a muted preference never suppresses a critical alert, a dead endpoint is
retired rather than retried forever, a push payload carries no clinical data,
and a notification a role should not see is not delivered to it.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.notifications import Notification, Severity, publish
from apps.core.roles import Role, group_names_for
from apps.notify import service, vapid
from apps.notify.backends.base import PushResult
from apps.notify.models import (
    DeliveryState,
    NotificationCategory,
    NotificationDelivery,
    NotificationPreference,
    NotificationRecord,
    PushBackend,
    PushSubscription,
)

ENDPOINT = "https://push.example.org/send/abc123"


def make_user(username: str, role: str | None = None) -> User:
    user = User.objects.create_user(username, password="pw")
    if role:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


def make_subscription(user=None, endpoint=ENDPOINT, **kwargs) -> PushSubscription:
    return PushSubscription.objects.create(
        user=user, endpoint=endpoint, p256dh="key", auth="auth", **kwargs
    )


class FakeBackend:
    """A push transport that records what it was asked to send."""

    name = "fake"

    def __init__(self, result: PushResult | None = None):
        self.result = result or PushResult.success()
        self.sent: list[tuple] = []

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def send(self, subscription, payload, *, ttl, urgency):
        self.sent.append((subscription, payload, ttl, urgency))
        return self.result


class VapidTests(TestCase):
    def test_generated_keypair_round_trips(self):
        private, public = vapid.generate_keypair()
        self.assertIn("BEGIN PRIVATE KEY", private)
        self.assertEqual(vapid.public_key_from_private(private), public)

    def test_public_key_is_unpadded_urlsafe_base64(self):
        _, public = vapid.generate_keypair()
        self.assertNotIn("=", public)
        self.assertNotIn("+", public)
        self.assertNotIn("/", public)
        # Uncompressed P-256 point: 65 bytes -> 87 base64 characters.
        self.assertEqual(len(public), 87)

    def test_settings_key_wins_over_the_file(self):
        private, public = vapid.generate_keypair()
        config = {**self.settings_dict(), "VAPID_PRIVATE_KEY": private, "VAPID_PUBLIC_KEY": ""}
        with override_settings(SEVPS=config):
            self.assertEqual(vapid.public_key(), public)
            self.assertTrue(vapid.is_configured())

    def test_escaped_newlines_from_an_env_var_are_restored(self):
        """A PEM in an environment variable arrives with literal backslash-n."""
        private, public = vapid.generate_keypair()
        config = {**self.settings_dict(), "VAPID_PRIVATE_KEY": private.replace("\n", "\\n")}
        with override_settings(SEVPS=config):
            self.assertEqual(vapid.public_key(), public)

    def test_missing_key_is_reported_not_raised(self):
        config = {**self.settings_dict(), "VAPID_PRIVATE_KEY": "", "VAPID_KEY_PATH": "/nonexistent"}
        with override_settings(SEVPS=config):
            self.assertFalse(vapid.is_configured())
            self.assertEqual(vapid.public_key(), "")

    def settings_dict(self) -> dict:
        from django.conf import settings

        return {**settings.SEVPS, "VAPID_PUBLIC_KEY": ""}


class SubscribeEndpointTests(TestCase):
    def test_anonymous_road_user_may_subscribe(self):
        """Layer 4 depends on this: a driver's phone has no account."""
        response = self.client.post(
            "/api/v1/notify/subscribe/",
            {"endpoint": ENDPOINT, "keys": {"p256dh": "k", "auth": "a"},
             "device_id": "phone-1", "latitude": 13.06, "longitude": 80.25},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        subscription = PushSubscription.objects.get()
        self.assertIsNone(subscription.user)
        self.assertTrue(subscription.geohash)
        self.assertIsNotNone(subscription.device)

    def test_resubscribing_updates_rather_than_duplicates(self):
        """A reload with permission already granted must not add a row."""
        body = {"endpoint": ENDPOINT, "keys": {"p256dh": "k", "auth": "a"}}
        first = self.client.post("/api/v1/notify/subscribe/", body, content_type="application/json")
        second = self.client.post("/api/v1/notify/subscribe/", body, content_type="application/json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(PushSubscription.objects.count(), 1)

    def test_resubscribing_revives_a_retired_subscription(self):
        make_subscription(is_active=False, failure_count=9)
        self.client.post(
            "/api/v1/notify/subscribe/",
            {"endpoint": ENDPOINT, "keys": {"p256dh": "k", "auth": "a"}},
            content_type="application/json",
        )
        subscription = PushSubscription.objects.get()
        self.assertTrue(subscription.is_active)
        self.assertEqual(subscription.failure_count, 0)

    def test_signed_in_subscription_is_attached_to_the_user(self):
        user = make_user("controller", Role.TRAFFIC_POLICE)
        self.client.force_login(user)
        self.client.post(
            "/api/v1/notify/subscribe/",
            {"endpoint": ENDPOINT, "keys": {"p256dh": "k", "auth": "a"}},
            content_type="application/json",
        )
        self.assertEqual(PushSubscription.objects.get().user, user)

    def test_webpush_without_keys_is_rejected(self):
        """Stored without p256dh/auth it could never deliver - fail loudly now."""
        response = self.client.post(
            "/api/v1/notify/subscribe/", {"endpoint": ENDPOINT}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("keys", response.json())

    def test_half_a_position_is_rejected(self):
        response = self.client.post(
            "/api/v1/notify/subscribe/",
            {"endpoint": ENDPOINT, "keys": {"p256dh": "k", "auth": "a"}, "latitude": 13.0},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_unsubscribe_needs_no_credentials(self):
        make_subscription()
        response = self.client.post(
            "/api/v1/notify/unsubscribe/", {"endpoint": ENDPOINT}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PushSubscription.objects.get().is_active)

    def test_vapid_public_key_is_public(self):
        response = self.client.get("/api/v1/notify/vapid-key/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("public_key", response.json())

    def test_endpoint_is_never_returned_in_full(self):
        """It is a bearer capability: anyone holding it can push to that browser."""
        user = make_user("op1", Role.DISPATCHER)
        make_subscription(user)
        self.client.force_login(user)
        body = self.client.get("/api/v1/notify/subscriptions/").json()
        serialised = str(body)
        self.assertNotIn(ENDPOINT, serialised)
        self.assertIn("...", body["subscriptions"][0]["endpoint_hint"])

    def test_subscriptions_are_scoped_to_the_caller(self):
        other = make_user("other", Role.DISPATCHER)
        make_subscription(other, endpoint="https://push.example.org/send/other")
        mine = make_user("mine", Role.DISPATCHER)
        make_subscription(mine)

        self.client.force_login(mine)
        body = self.client.get("/api/v1/notify/subscriptions/").json()
        self.assertEqual(len(body["subscriptions"]), 1)


class AudienceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hospital = make_user("nurse", Role.HOSPITAL)
        cls.police = make_user("police", Role.TRAFFIC_POLICE)
        make_subscription(cls.hospital, endpoint="https://push.example.org/hospital")
        make_subscription(cls.police, endpoint="https://push.example.org/police")

    def test_audience_selects_only_the_named_roles(self):
        targets = service.resolve_audience({"audience": [Role.HOSPITAL]})
        self.assertEqual([t.user for t in targets], [self.hospital])

    def test_empty_audience_means_every_operational_role(self):
        targets = service.resolve_audience({"audience": []})
        self.assertEqual(len(targets), 2)

    def test_legacy_group_names_still_resolve(self):
        """A deployment upgrading from `operators` must keep receiving alerts."""
        self.assertIn("operators", group_names_for(Role.TRAFFIC_POLICE))
        legacy = make_user("legacy_op", "operators")
        make_subscription(legacy, endpoint="https://push.example.org/legacy")

        targets = service.resolve_audience({"audience": [Role.TRAFFIC_POLICE]})
        self.assertIn(legacy, [t.user for t in targets])

    def test_superusers_receive_everything(self):
        admin = User.objects.create_superuser("root", password="pw")
        make_subscription(admin, endpoint="https://push.example.org/root")
        targets = service.resolve_audience({"audience": [Role.HOSPITAL]})
        self.assertIn(admin, [t.user for t in targets])

    def test_anonymous_devices_are_never_in_a_role_audience(self):
        """A road user must not receive 'corridor preemption failed at TSC-114'."""
        make_subscription(None, endpoint="https://push.example.org/anon", geohash="tf3b2k")
        targets = service.resolve_audience({"audience": []})
        self.assertTrue(all(t.user_id is not None for t in targets))

    def test_retired_subscriptions_are_not_targeted(self):
        PushSubscription.objects.filter(user=self.police).update(is_active=False)
        targets = service.resolve_audience({"audience": [Role.TRAFFIC_POLICE]})
        self.assertEqual(targets, [])

    def test_driver_audience_is_matched_by_cell(self):
        make_subscription(None, endpoint="https://push.example.org/d1", geohash="tf3b2k")
        make_subscription(None, endpoint="https://push.example.org/d2", geohash="zzzzzz")
        targets = service.resolve_driver_audience(["tf3b2k"])
        self.assertEqual(len(targets), 1)


class DeliveryTests(TestCase):
    def setUp(self):
        self.user = make_user("dispatcher", Role.DISPATCHER)
        self.subscription = make_subscription(self.user)
        self.record = NotificationRecord.objects.create(
            title="Inbound Level 1", body="Ambulance en route.",
            severity=Severity.CRITICAL, category=NotificationCategory.INBOUND_PATIENT,
            audience=[Role.DISPATCHER],
        )

    def deliver_with(self, backend):
        with patch("apps.notify.service.get_backend", return_value=backend):
            return service.deliver(self.record)

    def test_successful_delivery_is_recorded(self):
        backend = FakeBackend()
        result = self.deliver_with(backend)
        self.assertEqual(result.delivered, 1)
        self.assertEqual(
            NotificationDelivery.objects.get().state, DeliveryState.SENT
        )
        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.last_success_at)

    def test_a_gone_endpoint_is_retired_immediately(self):
        """404/410 is authoritative; retrying it forever is the bug this prevents."""
        backend = FakeBackend(PushResult.failure("gone", 410, gone=True))
        result = self.deliver_with(backend)

        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.is_active)
        self.assertEqual(result.failed, 1)
        self.assertEqual(NotificationDelivery.objects.get().state, DeliveryState.EXPIRED)

    def test_transient_failures_retire_only_after_a_threshold(self):
        backend = FakeBackend(PushResult.failure("timeout", 500))
        for _ in range(4):
            self.deliver_with(backend)
        self.subscription.refresh_from_db()
        self.assertTrue(self.subscription.is_active)

        self.deliver_with(backend)
        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.is_active)

    def test_success_clears_the_failure_counter(self):
        self.subscription.record_failure("blip")
        self.deliver_with(FakeBackend())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.failure_count, 0)

    def test_counts_are_rolled_up_onto_the_record(self):
        self.deliver_with(FakeBackend())
        self.record.refresh_from_db()
        self.assertEqual(self.record.delivered_count, 1)

    def test_critical_notifications_use_a_shorter_ttl_and_high_urgency(self):
        """A corridor warning that arrives after the ambulance passed is noise."""
        backend = FakeBackend()
        self.deliver_with(backend)
        _, _, ttl, urgency = backend.sent[0]
        self.assertEqual(ttl, 180)
        self.assertEqual(urgency, "high")

    def test_fanout_is_capped(self):
        for index in range(3):
            make_subscription(
                make_user(f"bulk{index}", Role.DISPATCHER),
                endpoint=f"https://push.example.org/bulk{index}",
            )
        with patch.object(service, "MAX_FANOUT", 2):
            result = self.deliver_with(FakeBackend())
        self.assertTrue(result.truncated)
        self.assertEqual(result.attempted, 2)

    def test_a_console_backend_reports_why_nothing_was_delivered(self):
        from apps.notify.backends.console import ConsoleBackend

        result = self.deliver_with(ConsoleBackend("no VAPID key"))
        self.assertEqual(result.delivered, 0)
        self.assertIn("VAPID", result.backend_note)


class PayloadTests(TestCase):
    def test_push_payload_carries_no_clinical_or_internal_context(self):
        """The relay is a third party and the device may be unlocked."""
        record = NotificationRecord.objects.create(
            title="Inbound", body="Priority 1", severity=Severity.CRITICAL,
            context={"patient_age": 61, "patient_notes": "confidential", "trip_id": 7},
        )
        payload = service.push_payload(record)
        serialised = str(payload)
        self.assertNotIn("61", serialised)
        self.assertNotIn("confidential", serialised)
        self.assertNotIn("context", payload)
        self.assertIn("title", payload)

    def test_dedupe_key_defaults_to_the_uuid(self):
        """The service worker collapses on tag; an empty tag would merge
        unrelated notifications into one banner."""
        record = NotificationRecord.objects.create(title="X", dedupe_key="")
        self.assertEqual(service.push_payload(record)["dedupe_key"], str(record.uuid))


class PreferenceTests(TestCase):
    def setUp(self):
        self.user = make_user("nurse2", Role.HOSPITAL)
        self.subscription = make_subscription(self.user)
        self.preference = NotificationPreference.objects.create(
            user=self.user, muted_categories=[NotificationCategory.CORRIDOR]
        )

    def test_a_muted_category_is_skipped(self):
        record = NotificationRecord.objects.create(
            title="Corridor failed", severity=Severity.WARNING,
            category=NotificationCategory.CORRIDOR, audience=[Role.HOSPITAL],
        )
        with patch("apps.notify.service.get_backend", return_value=FakeBackend()):
            result = service.deliver(record)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.attempted, 0)
        self.assertEqual(NotificationDelivery.objects.get().state, DeliveryState.SKIPPED)

    def test_critical_alerts_ignore_a_mute(self):
        """A hospital cannot mute 'inbound Level 1'."""
        record = NotificationRecord.objects.create(
            title="Corridor failed", severity=Severity.CRITICAL,
            category=NotificationCategory.CORRIDOR, audience=[Role.HOSPITAL],
        )
        with patch("apps.notify.service.get_backend", return_value=FakeBackend()):
            result = service.deliver(record)
        self.assertEqual(result.delivered, 1)

    def test_critical_alerts_ignore_push_disabled_entirely(self):
        self.preference.push_enabled = False
        self.preference.save()
        self.assertTrue(self.preference.allows("anything", Severity.CRITICAL))
        self.assertFalse(self.preference.allows("anything", Severity.WARNING))

    def test_quiet_hours_across_midnight(self):
        self.preference.quiet_hours_start = 22
        self.preference.quiet_hours_end = 6
        now = timezone.localtime().replace(hour=2)
        self.assertTrue(self.preference.in_quiet_hours(now))
        self.assertFalse(self.preference.in_quiet_hours(now.replace(hour=12)))

    def test_a_new_category_is_not_muted_by_default(self):
        """Mute-list rather than subscribe-list, so new alerts fail loud."""
        self.assertTrue(self.preference.allows("a_category_added_next_year", Severity.WARNING))

    def test_unknown_categories_are_rejected_by_the_api(self):
        self.client.force_login(self.user)
        response = self.client.patch(
            "/api/v1/notify/preferences/",
            {"muted_categories": ["not_a_category"]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_preferences_state_the_critical_rule(self):
        self.client.force_login(self.user)
        body = self.client.get("/api/v1/notify/preferences/").json()
        self.assertTrue(body["critical_always_delivered"])
        self.assertIn("Critical", body["note"])


class IntegrationTests(TestCase):
    """`core.notifications.publish` must keep working, plus now persist and push."""

    def test_publish_still_returns_a_payload_and_now_persists_it(self):
        payload = publish(
            Notification(
                title="Signal preemption failed at TSC-9",
                body="Junction stays on normal timing.",
                severity=Severity.WARNING,
                audience=(Role.TRAFFIC_POLICE,),
                dedupe_key="corridor-fail:1:TSC-9",
            )
        )
        self.assertEqual(payload["title"], "Signal preemption failed at TSC-9")

        record = NotificationRecord.objects.get()
        self.assertEqual(record.category, NotificationCategory.CORRIDOR)
        # The returned id is the stored one, so a client can mark it read.
        self.assertEqual(payload["id"], str(record.uuid))

    def test_publish_survives_a_broken_push_layer(self):
        """A dispatch decision must never fail because push is down."""
        with patch("apps.notify.service.record_notification", side_effect=RuntimeError("db gone")):
            payload = publish(Notification(title="Still published"))
        self.assertEqual(payload["title"], "Still published")

    def test_category_is_inferred_from_the_existing_dedupe_keys(self):
        cases = {
            "inbound:12": NotificationCategory.INBOUND_PATIENT,
            "corridor-fail:3:TSC-1": NotificationCategory.CORRIDOR,
            "escalation:7": NotificationCategory.PRIORITY,
            "reroute:7": NotificationCategory.ROUTE,
            "no-hospital:7": NotificationCategory.DISPATCH,
            "something-else": NotificationCategory.SYSTEM,
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(service.infer_category({"dedupe_key": key}), expected)

    def test_driver_alert_push_stays_anonymous(self):
        """A road user is warned, not told which trip or which patient."""
        from apps.core.enums import TripStage
        from apps.alerts.models import DriverAlert
        from apps.dispatch.models import EmergencyTrip
        from apps.fleet.models import EmergencyVehicle

        vehicle = EmergencyVehicle.objects.create(callsign="AMB-9", latitude=13.0, longitude=80.0)
        trip = EmergencyTrip.objects.create(
            vehicle=vehicle, stage=TripStage.TO_SCENE, patient_age=44, patient_notes="Secret"
        )
        alert = DriverAlert.objects.create(
            trip=trip, message="Ambulance approaching", instruction="Move left",
            eta_seconds=40, priority_level=1, latitude=13.0, longitude=80.0,
            geohash="tf3b2k", expires_at=timezone.now() + timezone.timedelta(seconds=60),
        )
        make_subscription(None, endpoint="https://push.example.org/road", geohash="tf3b2k")

        backend = FakeBackend()
        with patch("apps.notify.service.get_backend", return_value=backend):
            result = service.deliver_driver_alert(alert, ["tf3b2k"])

        self.assertEqual(result.delivered, 1)
        _, payload, _, _ = backend.sent[0]

        # Checked against the human-readable fields rather than the whole
        # serialised payload: that includes a random UUID, and asserting a
        # two-digit age is absent from a UUID fails about one run in twelve.
        text = " ".join(
            str(payload.get(field, "")) for field in ("title", "body", "link", "dedupe_key")
        )
        for leaked in ("Secret", "44", trip.reference):
            self.assertNotIn(leaked, text)
        # And the clinical fields must not be present as keys at all.
        for field in ("patient_age", "patient_notes", "context", "trip_id"):
            self.assertNotIn(field, payload)


class InboxTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.nurse = make_user("nurse3", Role.HOSPITAL)
        cls.police = make_user("police3", Role.TRAFFIC_POLICE)
        NotificationRecord.objects.create(title="For hospitals", audience=[Role.HOSPITAL])
        NotificationRecord.objects.create(title="For police", audience=[Role.TRAFFIC_POLICE])
        NotificationRecord.objects.create(title="For everyone", audience=[])

    def test_inbox_filters_by_role(self):
        self.client.force_login(self.nurse)
        titles = [n["title"] for n in self.client.get("/api/v1/notify/inbox/").json()["notifications"]]
        self.assertIn("For hospitals", titles)
        self.assertIn("For everyone", titles)
        self.assertNotIn("For police", titles)

    def test_inbox_requires_authentication(self):
        self.assertIn(self.client.get("/api/v1/notify/inbox/").status_code, (401, 403))

    def test_administrators_see_everything(self):
        admin = User.objects.create_superuser("root2", password="pw")
        self.client.force_login(admin)
        titles = [n["title"] for n in self.client.get("/api/v1/notify/inbox/").json()["notifications"]]
        self.assertEqual(len(titles), 3)

    def test_marking_read_reduces_the_unread_count(self):
        self.client.force_login(self.nurse)
        before = self.client.get("/api/v1/notify/inbox/").json()["unread"]
        self.assertGreater(before, 0)

        self.client.post("/api/v1/notify/read/")
        self.assertEqual(self.client.get("/api/v1/notify/inbox/").json()["unread"], 0)

    def test_read_state_is_per_user(self):
        self.client.force_login(self.nurse)
        self.client.post("/api/v1/notify/read/")
        self.client.force_login(self.police)
        self.assertGreater(self.client.get("/api/v1/notify/inbox/").json()["unread"], 0)

    def test_old_notifications_fall_out_of_the_window(self):
        stale = NotificationRecord.objects.create(title="Yesterday", audience=[])
        NotificationRecord.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=30)
        )
        self.client.force_login(self.nurse)
        titles = [n["title"] for n in self.client.get("/api/v1/notify/inbox/").json()["notifications"]]
        self.assertNotIn("Yesterday", titles)


class HealthAndTestSendTests(TestCase):
    def setUp(self):
        self.user = make_user("op9", Role.DISPATCHER)
        self.client.force_login(self.user)

    def test_health_reports_unconfigured_push_as_such(self):
        """Reporting 'healthy' with zero reach is the failure this prevents."""
        from django.conf import settings

        config = {**settings.SEVPS, "VAPID_PRIVATE_KEY": "", "VAPID_PUBLIC_KEY": "",
                  "VAPID_KEY_PATH": "/nonexistent"}
        with override_settings(SEVPS=config):
            body = self.client.get("/api/v1/notify/health/").json()
        self.assertFalse(body["webpush_configured"])
        self.assertEqual(body["status"], "push not configured")
        self.assertIn("generate_vapid_keys", body["hint"])

    def test_test_send_without_a_subscription_says_so(self):
        response = self.client.post("/api/v1/notify/test/")
        self.assertEqual(response.status_code, 409)

    def test_test_send_only_reaches_the_caller(self):
        make_subscription(self.user)
        other = make_user("other9", Role.DISPATCHER)
        make_subscription(other, endpoint="https://push.example.org/other9")

        backend = FakeBackend()
        with patch("apps.notify.service.get_backend", return_value=backend):
            body = self.client.post("/api/v1/notify/test/").json()

        self.assertEqual(body["attempted"], 1)
        self.assertEqual(backend.sent[0][0].user, self.user)

    def test_health_requires_authentication(self):
        self.client.logout()
        self.assertIn(self.client.get("/api/v1/notify/health/").status_code, (401, 403))


class PruneTests(TestCase):
    def test_long_silent_subscriptions_are_retired(self):
        user = make_user("stale", Role.DISPATCHER)
        subscription = make_subscription(user)
        PushSubscription.objects.filter(pk=subscription.pk).update(
            last_success_at=timezone.now() - timezone.timedelta(days=120)
        )
        self.assertEqual(service.prune_dead_subscriptions(60), 1)
        subscription.refresh_from_db()
        self.assertFalse(subscription.is_active)

    def test_recently_active_subscriptions_survive(self):
        user = make_user("fresh", Role.DISPATCHER)
        subscription = make_subscription(user)
        subscription.record_success()
        self.assertEqual(service.prune_dead_subscriptions(60), 0)

    def test_a_never_delivered_subscription_is_not_pruned(self):
        """A browser that subscribed an hour ago has no success timestamp yet."""
        make_subscription(make_user("new", Role.DISPATCHER))
        self.assertEqual(service.prune_dead_subscriptions(60), 0)


class BackendSelectionTests(TestCase):
    def test_unconfigured_webpush_falls_back_to_console_not_an_exception(self):
        from django.conf import settings
        from apps.notify.backends import get_backend

        config = {**settings.SEVPS, "VAPID_PRIVATE_KEY": "", "VAPID_PUBLIC_KEY": "",
                  "VAPID_KEY_PATH": "/nonexistent"}
        with override_settings(SEVPS=config):
            backend = get_backend(PushBackend.WEBPUSH)
        self.assertEqual(backend.name, "console")
        self.assertIn("VAPID", backend.unavailable_reason())

    def test_fcm_without_credentials_falls_back_and_says_it_is_normal(self):
        from apps.notify.backends import get_backend

        backend = get_backend(PushBackend.FCM)
        self.assertEqual(backend.name, "console")
        self.assertIn("normal", backend.unavailable_reason())

    def test_missing_vapid_subject_counts_as_unconfigured(self):
        """Push services reject a VAPID JWT with no contactable `sub`."""
        from django.conf import settings
        from apps.notify.backends.webpush import WebPushBackend

        private, _ = vapid.generate_keypair()
        config = {**settings.SEVPS, "VAPID_PRIVATE_KEY": private, "VAPID_SUBJECT": ""}
        with override_settings(SEVPS=config):
            backend = WebPushBackend()
            self.assertFalse(backend.is_available())
            self.assertIn("SUBJECT", backend.unavailable_reason())
