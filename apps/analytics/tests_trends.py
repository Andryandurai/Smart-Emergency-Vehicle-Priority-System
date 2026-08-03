"""Phase 10 tests: chart-shaped series, profiles, distributions and exports.

The properties worth pinning are the ones that make a chart *lie* rather than
break: a gap-skipping series that compresses the x-axis, a null measurement
plotted as zero, a demand profile keyed in the wrong timezone, and a trend
arrow that points the wrong way for cost metrics. None of those raise; they
just draw something confidently wrong.
"""
from __future__ import annotations

import csv
import io

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from apps.analytics import exports, trends
from apps.analytics.models import DailyMetric
from apps.core.enums import EmergencyCategory, PreemptionState, TripStage
from apps.core.roles import Role


def make_trip(*, created_at=None, response_s=None, category=EmergencyCategory.CARDIAC,
              level=1, stage=TripStage.ARRIVED, hospital=None, overridden=False):
    from apps.dispatch.models import EmergencyTrip
    from apps.fleet.models import EmergencyVehicle

    vehicle = EmergencyVehicle.objects.first() or EmergencyVehicle.objects.create(
        callsign="TREND-1", latitude=13.0, longitude=80.0
    )
    when = created_at or timezone.now()
    trip = EmergencyTrip.objects.create(
        vehicle=vehicle, emergency_category=category, priority_level=level,
        stage=stage, destination_hospital=hospital, hospital_was_overridden=overridden,
    )
    # created_at is auto_now_add, so backdating needs an update.
    EmergencyTrip.objects.filter(pk=trip.pk).update(created_at=when)
    if response_s is not None:
        EmergencyTrip.objects.filter(pk=trip.pk).update(
            dispatched_at=when,
            arrived_scene_at=when + timezone.timedelta(seconds=response_s),
        )
    trip.refresh_from_db()
    return trip


class DailySeriesTests(TestCase):
    def test_series_is_contiguous_even_with_no_activity(self):
        """A skipped quiet day compresses the axis and rewrites the story."""
        series = trends.daily_series(14)
        self.assertEqual(len(series["points"]), 14)
        dates = [point["date"] for point in series["points"]]
        self.assertEqual(sorted(dates), dates)
        self.assertEqual(len(set(dates)), 14)

    def test_counts_default_to_zero_and_measures_to_null(self):
        """Plotting an unmeasured response time as 0 reads as a perfect day."""
        point = trends.daily_series(3)["points"][0]
        self.assertEqual(point["trips_total"], 0)
        self.assertIsNone(point["avg_response_time_s"])

    def test_window_is_clamped(self):
        self.assertEqual(trends.daily_series(0)["window_days"], 1)
        self.assertEqual(
            trends.daily_series(9999)["window_days"], trends.MAX_WINDOW_DAYS
        )

    def test_materialised_rollups_are_preferred_over_live_computation(self):
        yesterday = timezone.localdate() - timezone.timedelta(days=1)
        DailyMetric.objects.create(date=yesterday, city="Chennai", trips_total=42)

        series = trends.daily_series(3)
        point = next(p for p in series["points"] if p["date"] == yesterday.isoformat())
        self.assertEqual(point["trips_total"], 42)
        self.assertEqual(series["materialised_days"], 1)
        self.assertEqual(series["computed_live_days"], 2)

    def test_live_days_are_reported_not_hidden(self):
        """A dashboard computing 90 days live means the rollup is not running."""
        series = trends.daily_series(30)
        self.assertEqual(
            series["materialised_days"] + series["computed_live_days"], 30
        )

    def test_reading_the_series_does_not_write_a_rollup(self):
        """A partial day materialised by a page view would block the real one."""
        trends.daily_series(7)
        self.assertEqual(DailyMetric.objects.count(), 0)

    def test_todays_activity_appears_without_a_rollup(self):
        make_trip(response_s=300)
        point = trends.daily_series(1)["points"][0]
        self.assertEqual(point["trips_total"], 1)
        self.assertEqual(point["avg_response_time_s"], 300.0)

    def test_every_declared_series_key_exists_on_every_point(self):
        """A chart selecting a key the points lack renders a flat line at zero."""
        series = trends.daily_series(3)
        for point in series["points"]:
            for spec in series["series"]:
                self.assertIn(spec["key"], point)

    def test_series_catalogue_declares_kind_and_colour(self):
        for spec in trends.daily_series(1)["series"]:
            self.assertIn(spec["kind"], ("count", "measure"))
            self.assertTrue(spec["colour"].startswith("#"))
            self.assertTrue(spec["label"])

    def test_default_series_share_no_conflicting_units_by_accident(self):
        """The picker plots one unit at a time; the defaults should look sane."""
        series = trends.daily_series(1)
        units = {
            spec["unit"] for spec in series["series"]
            if spec["key"] in series["default_series"]
        }
        self.assertGreaterEqual(len(units), 1)


class TrendComparisonTests(TestCase):
    def test_short_windows_report_that_they_are_not_comparable(self):
        self.assertFalse(trends.trend(2)["comparable"])

    def test_demand_volume_gets_no_verdict(self):
        """More emergencies is not SEVPS performing worse."""
        today = timezone.localdate()
        for offset, count in ((5, 2), (0, 20)):
            DailyMetric.objects.create(
                date=today - timezone.timedelta(days=offset), city="Chennai", trips_total=count
            )
        metric = next(m for m in trends.trend(10)["metrics"] if m["key"] == "trips_total")
        self.assertGreater(metric["change_pct"], 0)
        self.assertIsNone(metric["improving"])
        self.assertIsNone(metric["higher_is_better"])

    def test_a_rise_in_a_cost_metric_is_not_an_improvement(self):
        """`total_hold_seconds` up means road users waited longer."""
        today = timezone.localdate()
        for offset, hold in ((5, 100.0), (0, 400.0)):
            DailyMetric.objects.create(
                date=today - timezone.timedelta(days=offset),
                city="Chennai", total_hold_seconds=hold,
            )
        metric = next(
            m for m in trends.trend(10)["metrics"] if m["key"] == "total_hold_seconds"
        )
        self.assertGreater(metric["change_pct"], 0)
        self.assertFalse(metric["improving"])

    def test_a_rise_in_completions_is_an_improvement(self):
        today = timezone.localdate()
        for offset, done in ((5, 2), (0, 8)):
            DailyMetric.objects.create(
                date=today - timezone.timedelta(days=offset),
                city="Chennai", trips_completed=done,
            )
        metric = next(
            m for m in trends.trend(10)["metrics"] if m["key"] == "trips_completed"
        )
        self.assertTrue(metric["improving"])

    def test_no_data_yields_no_comparison_rather_than_zero_change(self):
        """'No change' and 'never measured' must not look the same."""
        metric = next(
            m for m in trends.trend(10)["metrics"] if m["key"] == "avg_response_time_s"
        )
        self.assertIsNone(metric["change_pct"])
        self.assertIsNone(metric["improving"])

    def test_counts_sum_and_measures_average_across_the_half(self):
        today = timezone.localdate()
        for offset in (0, 1):
            DailyMetric.objects.create(
                date=today - timezone.timedelta(days=offset), city="Chennai",
                trips_total=5, avg_response_time_s=300.0,
            )
        metrics = {m["key"]: m for m in trends.trend(4)["metrics"]}
        self.assertEqual(metrics["trips_total"]["current"], 10)      # summed
        self.assertEqual(metrics["avg_response_time_s"]["current"], 300.0)  # averaged


class DemandProfileTests(TestCase):
    def test_all_24_hours_and_7_weekdays_are_present(self):
        profile = trends.demand_profile(7)
        self.assertEqual(len(profile["hours"]), 24)
        self.assertEqual(len(profile["weekdays"]), 7)

    def test_hours_are_local_time_not_utc(self):
        """'The evening peak' is a claim about Chennai, not about UTC."""
        local = timezone.localtime().replace(hour=21, minute=30)
        make_trip(created_at=local)
        profile = trends.demand_profile(2)
        self.assertEqual(profile["hours"][21]["trips"], 1)
        self.assertIn("Kolkata", profile["timezone"])

    def test_peak_hour_is_none_when_nothing_happened(self):
        self.assertIsNone(trends.demand_profile(7)["peak_hour"])

    def test_response_time_is_reported_per_hour(self):
        local = timezone.localtime().replace(hour=9, minute=0)
        make_trip(created_at=local, response_s=240)
        hour = trends.demand_profile(2)["hours"][9]
        self.assertEqual(hour["trips"], 1)
        self.assertEqual(hour["avg_response_s"], 240.0)

    def test_hours_with_no_trips_report_null_response_not_zero(self):
        profile = trends.demand_profile(7)
        self.assertTrue(all(h["avg_response_s"] is None for h in profile["hours"]))

    def test_shares_sum_to_one_when_there_is_activity(self):
        for hour in (3, 3, 15):
            make_trip(created_at=timezone.localtime().replace(hour=hour, minute=10))
        profile = trends.demand_profile(2)
        self.assertAlmostEqual(sum(h["share"] for h in profile["hours"]), 1.0, places=3)


class DistributionTests(TestCase):
    def test_zero_count_categories_are_omitted_from_the_donut(self):
        """A zero slice is a legend entry with no wedge."""
        make_trip(category=EmergencyCategory.CARDIAC)
        distribution = trends.category_distribution(7)
        keys = [row["key"] for row in distribution["categories"]]
        self.assertEqual(keys, ["cardiac"])

    def test_every_category_has_a_colour(self):
        for value, _ in EmergencyCategory.choices:
            self.assertIn(value, trends.CATEGORY_COLOURS)

    def test_all_four_priority_levels_are_always_present(self):
        """Unlike categories, a zero level is meaningful: nothing was critical."""
        levels = trends.category_distribution(7)["levels"]
        self.assertEqual(len(levels), 4)
        self.assertTrue(all(level["value"] == 0 for level in levels))

    def test_returned_as_an_ordered_list_not_a_mapping(self):
        distribution = trends.category_distribution(7)
        self.assertIsInstance(distribution["categories"], list)
        self.assertIsInstance(distribution["levels"], list)


class CorridorOutcomeTests(TestCase):
    def setUp(self):
        from apps.dispatch.models import SignalPreemption
        from apps.network.models import Intersection, TrafficSignal

        node = Intersection.objects.create(latitude=13.0, longitude=80.0, is_signalised=True)
        self.signal = TrafficSignal.objects.create(intersection=node, controller_id="TSC-T")
        self.trip = make_trip()
        self.model = SignalPreemption

    def _preemption(self, **kwargs):
        now = timezone.now()
        return self.model.objects.create(
            trip=self.trip, signal=self.signal,
            planned_green_at=now, planned_release_at=now + timezone.timedelta(seconds=30),
            hold_duration_s=30.0, **kwargs
        )

    def test_a_yielded_preemption_is_not_counted_as_cancelled(self):
        """Yielding is the contention rule working, not a fault."""
        higher = self._preemption(state=PreemptionState.ACTIVE, activated_at=timezone.now())
        self._preemption(state=PreemptionState.CANCELLED, yielded_to=higher)

        point = trends.corridor_outcomes(1)["points"][-1]
        self.assertEqual(point["yielded"], 1)
        self.assertEqual(point["cancelled"], 0)

    def test_failed_controllers_are_counted_separately(self):
        self._preemption(state=PreemptionState.FAILED)
        point = trends.corridor_outcomes(1)["points"][-1]
        self.assertEqual(point["failed"], 1)

    def test_activation_is_detected_by_timestamp_not_current_state(self):
        """A released preemption still activated; it must not read as pending."""
        self._preemption(state=PreemptionState.RELEASED, activated_at=timezone.now())
        point = trends.corridor_outcomes(1)["points"][-1]
        self.assertEqual(point["activated"], 1)
        self.assertEqual(point["pending"], 0)

    def test_series_is_contiguous(self):
        self.assertEqual(len(trends.corridor_outcomes(10)["points"]), 10)

    def test_legend_keys_all_exist_on_the_points(self):
        outcomes = trends.corridor_outcomes(3)
        for entry in outcomes["legend"]:
            self.assertIn(entry["key"], outcomes["points"][0])


class ResponseDistributionTests(TestCase):
    def test_buckets_partition_without_overlap(self):
        for seconds in (30, 250, 400, 500, 700, 1100, 1500, 2400):
            make_trip(response_s=seconds)
        distribution = trends.response_distribution(2)
        self.assertEqual(sum(b["count"] for b in distribution["buckets"]), 8)
        self.assertEqual(distribution["samples"], 8)

    def test_the_target_threshold_is_reported(self):
        make_trip(response_s=200)   # under 8 min
        make_trip(response_s=900)   # over
        distribution = trends.response_distribution(2)
        self.assertEqual(distribution["target_minutes"], 8)
        self.assertEqual(distribution["within_target"], 1)
        self.assertEqual(distribution["within_target_share"], 0.5)

    def test_no_samples_reports_null_share_not_zero(self):
        """0% within target and 'nothing measured' are different claims."""
        distribution = trends.response_distribution(7)
        self.assertEqual(distribution["samples"], 0)
        self.assertIsNone(distribution["within_target_share"])

    def test_the_final_bucket_is_open_ended(self):
        make_trip(response_s=6000)
        distribution = trends.response_distribution(2)
        self.assertIsNone(distribution["buckets"][-1]["high_min"])
        self.assertEqual(distribution["buckets"][-1]["count"], 1)


class HospitalLoadTests(TestCase):
    def setUp(self):
        from apps.hospitals.models import Hospital

        self.hospital = Hospital.objects.create(
            code="LOAD", name="Load Hospital", latitude=13.0, longitude=80.0
        )

    def test_override_share_is_computed_per_hospital(self):
        make_trip(hospital=self.hospital, overridden=True)
        make_trip(hospital=self.hospital, overridden=False)
        row = trends.hospital_load(7)["hospitals"][0]
        self.assertEqual(row["trips"], 2)
        self.assertEqual(row["overrides"], 1)
        self.assertEqual(row["override_share"], 0.5)

    def test_trips_without_a_hospital_are_excluded(self):
        make_trip(hospital=None)
        self.assertEqual(trends.hospital_load(7)["total_routed"], 0)

    def test_truncation_is_reported(self):
        from apps.hospitals.models import Hospital

        for index in range(4):
            hospital = Hospital.objects.create(
                code=f"H{index}", name=f"H{index}", latitude=13.0, longitude=80.0
            )
            make_trip(hospital=hospital)
        self.assertTrue(trends.hospital_load(7, limit=2)["truncated"])


class ExportTests(TestCase):
    def setUp(self):
        make_trip(response_s=300)

    def test_every_dataset_streams_and_matches_its_declared_columns(self):
        for key, dataset in exports.DATASETS.items():
            with self.subTest(dataset=key):
                response = exports.stream_csv(key, 7)
                text = b"".join(response.streaming_content).decode("utf-8")
                rows = list(csv.reader(io.StringIO(text)))
                # Three comment lines, then the header.
                self.assertEqual(rows[3], dataset.headers)
                for row in rows[4:]:
                    self.assertEqual(len(row), len(dataset.headers))

    def test_the_file_carries_its_window_and_generation_time(self):
        """An unattributable CSV in a shared drive gets compared with the wrong one."""
        text = b"".join(exports.stream_csv("daily", 14).streaming_content).decode("utf-8")
        self.assertIn("window: last 14 days", text)
        self.assertIn("generated:", text)

    def test_filename_carries_the_dataset_and_date(self):
        response = exports.stream_csv("daily", 7)
        disposition = response["Content-Disposition"]
        self.assertIn("sevps-daily-", disposition)
        self.assertIn(".csv", disposition)

    def test_daily_export_has_one_row_per_day(self):
        text = b"".join(exports.stream_csv("daily", 5).streaming_content).decode("utf-8")
        rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].startswith("#")]
        self.assertEqual(len(rows), 6)  # header + 5 days

    def test_catalogue_lists_every_dataset_with_a_url(self):
        catalogue = exports.catalogue()
        self.assertEqual(len(catalogue), len(exports.DATASETS))
        for entry in catalogue:
            self.assertTrue(entry["url"].endswith(".csv"))
            self.assertTrue(entry["columns"])


class ChartEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("analyst", password="pw")
        group, _ = Group.objects.get_or_create(name=Role.ADMIN)
        cls.user.groups.add(group)

    ENDPOINTS = (
        "/api/v1/analytics/trends/",
        "/api/v1/analytics/trends/summary/",
        "/api/v1/analytics/demand/",
        "/api/v1/analytics/distribution/",
        "/api/v1/analytics/corridor-outcomes/",
        "/api/v1/analytics/response-distribution/",
        "/api/v1/analytics/hospital-load/",
        "/api/v1/analytics/export/",
    )

    def test_all_chart_endpoints_require_authentication(self):
        """Daily emergency volume is operational intelligence, not public data."""
        for url in self.ENDPOINTS:
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (401, 403))

    def test_all_chart_endpoints_serve_a_signed_in_role(self):
        self.client.force_login(self.user)
        for url in self.ENDPOINTS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_csv_export_requires_authentication(self):
        self.assertIn(
            self.client.get("/api/v1/analytics/export/daily.csv").status_code, (401, 403)
        )

    def test_csv_export_streams_for_a_signed_in_role(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/analytics/export/daily.csv?days=5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])

    def test_unknown_dataset_is_404_not_an_empty_file(self):
        """A report pipeline given an empty CSV reports zero incidents."""
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/analytics/export/nonsense.csv")
        self.assertEqual(response.status_code, 404)
        self.assertIn("daily", response.json()["available"])

    def test_days_parameter_is_honoured_and_clamped(self):
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get("/api/v1/analytics/trends/?days=5").json()["window_days"], 5
        )
        self.assertEqual(
            self.client.get("/api/v1/analytics/trends/?days=99999").json()["window_days"], 365
        )

    def test_a_junk_days_parameter_falls_back_rather_than_erroring(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/analytics/trends/?days=lots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["window_days"], 30)
