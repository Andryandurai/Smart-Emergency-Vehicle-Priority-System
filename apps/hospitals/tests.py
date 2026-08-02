"""Rule-Based Emergency Engine and hospital recommendation tests.

The central guarantee under test: capability is a *hard* filter.  No amount of
proximity, spare beds or reputation may put a patient in a hospital that
cannot treat them.
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase

from apps.core.enums import HospitalFacility as HF
from apps.core.enums import PriorityLevel
from apps.core.geo import Point
from apps.hospitals.models import Hospital, HospitalCapability, HospitalCapacity
from apps.hospitals.recommender import recommend_hospital
from apps.hospitals.rules import resolve_rule, seed_rules

ORIGIN = Point(13.06, 80.25)


def make_hospital(code, *, facilities, offset=0.01, ed_free=10, icu_free=5, quality=0.8, **kwargs):
    hospital = Hospital.objects.create(
        code=code,
        name=f"{code} Hospital",
        latitude=ORIGIN.lat + offset,
        longitude=ORIGIN.lon,
        quality_index=quality,
        **kwargs,
    )
    HospitalCapability.objects.bulk_create(
        [HospitalCapability(hospital=hospital, facility=f) for f in facilities]
    )
    HospitalCapacity.objects.create(
        hospital=hospital,
        emergency_beds_total=20, emergency_beds_available=ed_free,
        icu_beds_total=10, icu_beds_available=icu_free,
        patients_waiting=2, doctors_on_duty=4,
    )
    return hospital


class RuleEngineTests(TestCase):
    def setUp(self):
        seed_rules()

    def test_problem_statement_mapping_is_honoured(self):
        """The five rows of the specification's table, exactly."""
        expected = {
            "cardiac": {HF.CARDIAC_ICU, HF.CATH_LAB},
            "stroke": {HF.NEUROLOGY, HF.CT_SCAN},
            "burn": {HF.BURN_UNIT},
            "trauma": {HF.TRAUMA_CENTER},
            "poisoning": {HF.TOXICOLOGY, HF.ICU},
        }
        for category, facilities in expected.items():
            self.assertEqual(resolve_rule(category).required, facilities, category)

    def test_seeding_is_idempotent(self):
        first = resolve_rule("cardiac")
        seed_rules()
        self.assertEqual(resolve_rule("cardiac").required, first.required)

    def test_unknown_category_falls_back_safely(self):
        rule = resolve_rule("not-a-real-category")
        self.assertIn(HF.EMERGENCY_DEPT, rule.required)

    def test_rule_resolves_without_a_seeded_database(self):
        from apps.hospitals.models import EmergencyRule

        EmergencyRule.objects.all().delete()
        rule = resolve_rule("cardiac")
        self.assertEqual(rule.required, {HF.CARDIAC_ICU, HF.CATH_LAB})
        self.assertEqual(rule.priority_level, PriorityLevel.CRITICAL)


class RecommendationTests(TestCase):
    def setUp(self):
        seed_rules()
        # Very close, but cannot treat a cardiac case.
        self.near_incapable = make_hospital(
            "NEAR", facilities=[HF.EMERGENCY_DEPT], offset=0.002
        )
        # Further away, fully capable.
        self.far_capable = make_hospital(
            "FAR",
            facilities=[HF.EMERGENCY_DEPT, HF.CARDIAC_ICU, HF.CATH_LAB, HF.ICU],
            offset=0.03,
        )

    def test_capability_beats_proximity(self):
        result = recommend_hospital(ORIGIN, "cardiac")
        self.assertEqual(result.recommended, self.far_capable)

    def test_incapable_hospital_is_excluded_with_a_reason(self):
        result = recommend_hospital(ORIGIN, "cardiac")
        excluded = {c.hospital.code: c for c in result.candidates if not c.eligible}
        self.assertIn("NEAR", excluded)
        self.assertIn("cardiac_icu", excluded["NEAR"].exclusion_reason)

    def test_diversion_removes_a_hospital(self):
        self.far_capable.is_on_diversion = True
        self.far_capable.diversion_reason = "ED at capacity"
        self.far_capable.save()

        result = recommend_hospital(ORIGIN, "cardiac")
        # Nothing capable is left, so the engine relaxes - and says so.
        self.assertTrue(result.relaxed)
        self.assertIn("Relaxed", result.relaxation_note)
        self.assertNotEqual(result.recommended, self.far_capable)

    def test_relaxation_keeps_the_reason_as_a_warning(self):
        """A relaxed recommendation must never look like a clean one."""
        self.far_capable.delete()
        result = recommend_hospital(ORIGIN, "cardiac")

        self.assertTrue(result.relaxed)
        self.assertEqual(result.recommended, self.near_incapable)
        candidate = next(c for c in result.candidates if c.hospital == self.near_incapable)
        self.assertTrue(candidate.eligible)
        self.assertTrue(candidate.warnings, "the original exclusion must survive as a warning")
        self.assertIn("cardiac_icu", candidate.warnings[0])

    def test_capability_is_relaxed_before_capacity(self):
        """Given a choice, give up the nice-to-have before the hard limit."""
        HospitalCapacity.objects.filter(hospital=self.far_capable).update(
            emergency_beds_available=0
        )
        result = recommend_hospital(ORIGIN, "cardiac")

        self.assertTrue(result.relaxed)
        # NEAR lacks the facilities but has beds; FAR has facilities but no bed.
        # Tier 1 relaxes capability only, so NEAR is the one re-admitted.
        self.assertEqual(result.recommended, self.near_incapable)
        far = next(c for c in result.candidates if c.hospital == self.far_capable)
        self.assertFalse(far.eligible)

    def test_capacity_is_relaxed_only_as_a_last_resort(self):
        self.near_incapable.delete()
        HospitalCapacity.objects.filter(hospital=self.far_capable).update(
            emergency_beds_available=0
        )
        result = recommend_hospital(ORIGIN, "cardiac")

        self.assertTrue(result.relaxed)
        self.assertEqual(result.recommended, self.far_capable)
        self.assertIn("over capacity", result.relaxation_note)
        candidate = next(c for c in result.candidates if c.hospital == self.far_capable)
        self.assertTrue(candidate.warnings)

    def test_declared_diversion_is_never_overridden(self):
        """An algorithm must not reverse a hospital's own refusal to accept."""
        Hospital.objects.update(is_on_diversion=True, diversion_reason="mass casualty")
        result = recommend_hospital(ORIGIN, "cardiac")

        self.assertIsNone(result.recommended)
        self.assertIn("escalate", result.relaxation_note.lower())
        self.assertTrue(all(not c.eligible for c in result.candidates))

    def test_no_beds_excludes_a_hospital(self):
        HospitalCapacity.objects.filter(hospital=self.far_capable).update(
            emergency_beds_available=0
        )
        result = recommend_hospital(ORIGIN, "cardiac")
        excluded = {c.hospital.code: c for c in result.candidates if not c.eligible}
        self.assertIn("FAR", excluded)
        self.assertIn("no emergency beds", excluded["FAR"].exclusion_reason)

    def test_icu_requirement_is_enforced(self):
        HospitalCapacity.objects.filter(hospital=self.far_capable).update(icu_beds_available=0)
        result = recommend_hospital(ORIGIN, "cardiac")
        codes = {c.hospital.code for c in result.candidates if c.eligible}
        self.assertNotIn("FAR", codes)

    def test_between_two_capable_hospitals_the_nearer_wins(self):
        nearer = make_hospital(
            "NEARCAP",
            facilities=[HF.EMERGENCY_DEPT, HF.CARDIAC_ICU, HF.CATH_LAB, HF.ICU],
            offset=0.005,
        )
        result = recommend_hospital(ORIGIN, "cardiac")
        self.assertEqual(result.recommended, nearer)

    def test_workload_breaks_a_tie(self):
        busy = make_hospital(
            "BUSY",
            facilities=[HF.EMERGENCY_DEPT, HF.CARDIAC_ICU, HF.CATH_LAB, HF.ICU],
            offset=0.03, ed_free=1,
        )
        HospitalCapacity.objects.filter(hospital=busy).update(
            patients_waiting=40, doctors_on_duty=1
        )
        result = recommend_hospital(ORIGIN, "cardiac")
        # Same distance band as FAR, but saturated - must not be preferred.
        self.assertEqual(result.recommended, self.far_capable)

    def test_every_candidate_carries_its_scores(self):
        result = recommend_hospital(ORIGIN, "cardiac")
        eligible = [c for c in result.candidates if c.eligible]
        self.assertTrue(eligible)
        for candidate in eligible:
            self.assertIn("capability", candidate.factors)
            self.assertIn("travel_time", candidate.factors)
            self.assertIn("bed_availability", candidate.factors)

    def test_burn_case_requires_a_burn_unit(self):
        burn_centre = make_hospital(
            "BURN", facilities=[HF.EMERGENCY_DEPT, HF.BURN_UNIT, HF.ICU], offset=0.05
        )
        result = recommend_hospital(ORIGIN, "burn")
        self.assertEqual(result.recommended, burn_centre)
        self.assertFalse(result.relaxed)


class HospitalStaffPermissionTests(TestCase):
    """Who may write to a hospital's live capacity and diversion status."""

    def setUp(self):
        seed_rules()
        self.hospital = make_hospital("PERM", facilities=[HF.EMERGENCY_DEPT])
        self.other = make_hospital("PERM2", facilities=[HF.EMERGENCY_DEPT], offset=0.02)

        self.staff_group = Group.objects.create(name="hospital_staff")
        self.operators = Group.objects.create(name="operators")

        self.nurse = User.objects.create_user("nurse", password="x")
        self.nurse.groups.add(self.staff_group)
        self.operator = User.objects.create_user("op", password="x")
        self.operator.groups.add(self.operators)
        self.admin = User.objects.create_user("root", password="x", is_staff=True, is_superuser=True)

    def _diversion(self, user):
        self.client.force_login(user)
        return self.client.post(
            f"/api/v1/hospitals/hospitals/{self.hospital.id}/diversion/",
            data={"is_on_diversion": True, "reason": "test"},
            content_type="application/json",
        ).status_code

    def test_hospital_staff_may_declare_diversion(self):
        """Regression: an unset staff_group used to lock out every hospital user."""
        self.assertEqual(self._diversion(self.nurse), 200)

    def test_traffic_operator_may_not_declare_diversion(self):
        """Diversion is a clinical decision, not a traffic-control one."""
        self.assertEqual(self._diversion(self.operator), 403)

    def test_administrator_may_declare_diversion(self):
        self.assertEqual(self._diversion(self.admin), 200)

    def test_anonymous_may_not_declare_diversion(self):
        self.client.logout()
        response = self.client.post(
            f"/api/v1/hospitals/hospitals/{self.hospital.id}/diversion/",
            data={"is_on_diversion": True}, content_type="application/json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_staff_group_scopes_a_user_to_their_own_hospital(self):
        """With tenancy configured, one hospital cannot act on another."""
        own_group = Group.objects.create(name="perm-ed")
        self.hospital.staff_group = own_group
        self.hospital.save(update_fields=["staff_group"])

        # The nurse is in `hospital_staff` but not in this hospital's own group.
        self.assertEqual(self._diversion(self.nurse), 403)

        self.nurse.groups.add(own_group)
        self.assertEqual(self._diversion(self.nurse), 200)

    def test_capacity_updates_follow_the_same_rule(self):
        self.client.force_login(self.nurse)
        response = self.client.patch(
            f"/api/v1/hospitals/hospitals/{self.hospital.id}/capacity/",
            data={"emergency_beds_available": 3}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["emergency_beds_available"], 3)

    def test_reads_stay_open(self):
        """Dashboards and display boards must work without credentials."""
        self.client.logout()
        self.assertEqual(
            self.client.get(f"/api/v1/hospitals/hospitals/{self.hospital.id}/").status_code, 200
        )
