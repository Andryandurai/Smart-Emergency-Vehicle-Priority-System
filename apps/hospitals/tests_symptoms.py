"""Symptom-driven triage - the 'I am not sure what this is' path."""
from django.test import TestCase

from apps.core.enums import EmergencyCategory as EC
from apps.core.enums import HospitalFacility as F
from apps.core.enums import PatientSymptom as S
from apps.core.enums import PriorityLevel
from apps.hospitals.rules import resolve_rule, resolve_rule_for, seed_rules
from apps.hospitals.symptoms import assess, catalogue


class SymptomAssessmentTests(TestCase):
    def test_every_offered_symptom_has_a_clinical_profile(self):
        """The picker and the engine must not drift apart."""
        offered = {item["code"] for item in catalogue()}
        self.assertEqual(offered, set(S.values))

    def test_no_symptoms_asserts_nothing(self):
        reading = assess([])
        self.assertTrue(reading.is_empty)
        self.assertEqual(reading.required, set())
        # Crucially not "critical" - an empty assessment must not manufacture
        # urgency it has no evidence for.
        self.assertEqual(reading.priority, PriorityLevel.NON_CRITICAL)

    def test_facilities_are_unioned_across_symptoms(self):
        """A patient who is burned and bleeding needs both capabilities."""
        reading = assess([S.BURNS, S.BLEEDING])
        self.assertIn(F.BURN_UNIT, reading.required)
        self.assertIn(F.BLOOD_BANK, reading.required)

    def test_priority_is_the_most_urgent_symptom_not_an_average(self):
        """Averaging chest pain with fever would answer a heart attack with L3."""
        reading = assess([S.FEVER, S.CHEST_PAIN])
        self.assertEqual(reading.priority, PriorityLevel.CRITICAL)

    def test_a_combination_can_imply_more_than_its_parts(self):
        alone = assess([S.UNCONSCIOUS])
        together = assess([S.UNCONSCIOUS, S.BLEEDING])
        self.assertNotIn(F.TRAUMA_CENTER, alone.required)
        self.assertIn(F.TRAUMA_CENTER, together.required)

    def test_a_required_facility_is_never_also_merely_preferred(self):
        reading = assess([S.CHEST_PAIN, S.BREATHING_DIFFICULTY])
        self.assertFalse(reading.required & reading.preferred)

    def test_unknown_codes_are_ignored_rather_than_crashing(self):
        reading = assess([S.FEVER, "not_a_symptom"])
        self.assertEqual(reading.symptoms, [S.FEVER])


class SymptomRuleResolutionTests(TestCase):
    def setUp(self):
        seed_rules()

    def test_symptoms_add_to_a_known_category_rather_than_replacing_it(self):
        """Trauma plus burns needs a trauma centre AND a burn unit."""
        base = resolve_rule(EC.TRAUMA)
        combined = resolve_rule_for(EC.TRAUMA, [S.BURNS])
        self.assertTrue(base.required <= combined.required)
        self.assertIn(F.BURN_UNIT, combined.required)

    def test_symptoms_give_an_undetermined_category_something_to_match_on(self):
        """Undetermined requires only an ED, which excludes almost nothing.

        That is the correct rule - an unsure crew must not narrow the hospital
        list on a guess - but it means the recommendation is decided purely on
        distance. Observations are what turn it back into a clinical decision.
        """
        bare = resolve_rule_for(EC.UNKNOWN, [])
        led = resolve_rule_for(EC.UNKNOWN, [S.PARALYSIS])
        self.assertEqual(bare.required, {F.EMERGENCY_DEPT})
        self.assertIn(F.STROKE_UNIT, led.required)
        self.assertTrue(bare.required < led.required)

    def test_an_undetermined_category_is_renamed_by_its_findings(self):
        """'Undetermined' on a hospital board is a shrug; name the findings."""
        rule = resolve_rule_for(EC.UNKNOWN, [S.UNCONSCIOUS, S.BLEEDING])
        self.assertIn("Unconscious", rule.display_name)
        self.assertIn("Bleeding", rule.display_name)

    def test_symptoms_can_escalate_priority_but_never_soften_it(self):
        transfer = resolve_rule(EC.TRANSFER)
        escalated = resolve_rule_for(EC.TRANSFER, [S.CHEST_PAIN])
        self.assertEqual(escalated.priority_level, PriorityLevel.CRITICAL)

        cardiac = resolve_rule(EC.CARDIAC)
        with_fever = resolve_rule_for(EC.CARDIAC, [S.FEVER])
        self.assertLessEqual(with_fever.priority_level, cardiac.priority_level)

    def test_no_symptoms_leaves_the_category_rule_untouched(self):
        base = resolve_rule(EC.STROKE)
        same = resolve_rule_for(EC.STROKE, [])
        self.assertEqual(base.required, same.required)
        self.assertEqual(base.priority_level, same.priority_level)

    def test_clinical_guidance_carries_the_symptom_reasoning(self):
        rule = resolve_rule_for(EC.UNKNOWN, [S.CHEST_PAIN])
        self.assertIn("cardiac", rule.guidance.lower())


class SymptomApiTests(TestCase):
    def setUp(self):
        seed_rules()

    def test_the_catalogue_is_public(self):
        """A crew on an unauthenticated fallback device still records symptoms."""
        response = self.client.get("/api/v1/hospitals/symptoms/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["symptoms"]), len(S.values))

    def test_the_catalogue_says_what_each_choice_will_do(self):
        """A crew should see that 'Paralysis' routes to a stroke unit."""
        rows = self.client.get("/api/v1/hospitals/symptoms/").json()["symptoms"]
        paralysis = next(r for r in rows if r["code"] == S.PARALYSIS)
        self.assertIn(F.STROKE_UNIT, paralysis["required_facilities"])
        self.assertTrue(paralysis["note"])
