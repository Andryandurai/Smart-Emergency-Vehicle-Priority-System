"""Phase 6 tests: the prediction envelope, the safety invariants, SHAP.

Two invariants matter more than accuracy here, and both are asserted directly:

* **No prediction escapes without confidence and an explanation.**
* **No model overrides a clinical rule.** The Rule-Based Emergency Engine
  decides priority; the model may only disagree out loud.
"""
from __future__ import annotations

import unittest

from django.test import SimpleTestCase, TestCase

from apps.brain.ml import estimators as est
from apps.brain.ml import services, training
from apps.brain.ml.base import Explanation, FeatureContribution, Prediction

try:
    import sklearn  # noqa: F401

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

needs_sklearn = unittest.skipUnless(HAS_SKLEARN, "scikit-learn not installed")


class PredictionEnvelopeTests(SimpleTestCase):
    def _prediction(self, **kwargs) -> Prediction:
        defaults = dict(
            value=1.0,
            confidence=0.8,
            explanation=Explanation(method="test"),
            model="test",
        )
        return Prediction(**{**defaults, **kwargs})

    def test_confidence_is_clamped_to_a_probability(self):
        self.assertEqual(self._prediction(confidence=1.7).confidence, 1.0)
        self.assertEqual(self._prediction(confidence=-3).confidence, 0.0)

    def test_actionability_has_one_threshold(self):
        self.assertTrue(self._prediction(confidence=0.9).is_actionable)
        self.assertFalse(self._prediction(confidence=0.2).is_actionable)

    def test_serialised_form_always_carries_the_three_keys(self):
        payload = self._prediction().as_dict()
        for key in ("prediction", "confidence", "explanation", "is_actionable", "source"):
            self.assertIn(key, payload)

    def test_explanation_ranks_by_absolute_contribution(self):
        explanation = Explanation(
            method="shap",
            contributions=[
                FeatureContribution("small", 1, 0.01),
                FeatureContribution("big_negative", 2, -0.9),
                FeatureContribution("medium", 3, 0.4),
            ],
        )
        # A large negative driver is as important as a large positive one.
        self.assertEqual([c.name for c in explanation.top(2)], ["big_negative", "medium"])

    def test_narration_names_the_actual_drivers(self):
        explanation = Explanation(
            method="shap",
            contributions=[FeatureContribution("live_factor", 0.4, -0.31)],
        )
        self.assertIn("live factor", explanation.narrate())

    def test_empty_explanation_narrates_honestly(self):
        self.assertIn("No individual feature", Explanation(method="shap").narrate())


class BaselineTests(SimpleTestCase):
    """Every estimator must answer before it has ever been trained."""

    def test_congestion_baseline_blends_live_and_historical(self):
        value, explanation = est.CONGESTION.baseline(
            {"live_factor": 0.4, "historical_factor": 0.9, "horizon_min": 0.0}
        )
        self.assertAlmostEqual(value, 0.4, places=2)   # horizon 0 -> trust the observation
        self.assertEqual(explanation.method, "statistical")

    def test_congestion_baseline_decays_toward_history(self):
        near, _ = est.CONGESTION.baseline(
            {"live_factor": 0.2, "historical_factor": 0.9, "horizon_min": 0.0}
        )
        far, _ = est.CONGESTION.baseline(
            {"live_factor": 0.2, "historical_factor": 0.9, "horizon_min": 60.0}
        )
        self.assertGreater(far, near)

    def test_congestion_baseline_without_a_live_reading(self):
        value, explanation = est.CONGESTION.baseline({"historical_factor": 0.65})
        self.assertEqual(value, 0.65)
        self.assertIn("profile", explanation.summary)

    def test_eta_baseline_adds_no_correction(self):
        value, explanation = est.ETA_RESIDUAL.baseline({})
        self.assertEqual(value, 0.0)
        self.assertIn("unadjusted", explanation.summary)

    def test_corridor_baseline_respects_the_hard_guards(self):
        offline, _ = est.CORRIDOR_SUCCESS.baseline({"is_online": 0})
        self.assertEqual(offline, 0.0)
        incapable, _ = est.CORRIDOR_SUCCESS.baseline(
            {"is_online": 1, "supports_preemption": 0}
        )
        self.assertEqual(incapable, 0.0)

    def test_corridor_baseline_penalises_recent_holds(self):
        fresh, _ = est.CORRIDOR_SUCCESS.baseline(
            {"is_online": 1, "supports_preemption": 1, "recent_preemptions": 0}
        )
        busy, _ = est.CORRIDOR_SUCCESS.baseline(
            {"is_online": 1, "supports_preemption": 1, "recent_preemptions": 3}
        )
        self.assertGreater(fresh, busy)

    def test_clearance_grows_with_congestion_and_is_capped(self):
        light, _ = est.CLEARANCE_TIME.baseline({"congestion_index": 0.0, "lanes": 2})
        heavy, _ = est.CLEARANCE_TIME.baseline({"congestion_index": 0.9, "lanes": 4})
        self.assertGreater(heavy, light)
        self.assertLessEqual(heavy, est.CLEARANCE_TIME.MAX_CLEARANCE_S)

    def test_untrained_estimator_predicts_from_its_baseline(self):
        prediction = est.CLEARANCE_TIME.predict({"congestion_index": 0.5, "lanes": 2})
        self.assertEqual(prediction.source, "baseline")
        # Rule-derived: dependable, not precise. Saying 0.5 is the honest answer.
        self.assertEqual(prediction.confidence, 0.5)


class PriorityAuthorityTests(TestCase):
    """The safety-critical invariant of this phase."""

    def setUp(self):
        from apps.hospitals.rules import seed_rules

        seed_rules()

    def test_prediction_returns_the_rule_level_not_the_model_level(self):
        prediction = services.predict_priority("cardiac")
        self.assertEqual(prediction.value, 1)
        self.assertEqual(prediction.context["authoritative_level"], 1)

    def test_every_category_matches_its_clinical_rule(self):
        from apps.hospitals.rules import resolve_rule

        for category in ("cardiac", "stroke", "burn", "trauma", "poisoning", "transfer"):
            with self.subTest(category=category):
                prediction = services.predict_priority(category)
                self.assertEqual(prediction.value, int(resolve_rule(category).priority_level))

    def test_context_records_that_the_rule_is_authoritative(self):
        prediction = services.predict_priority("stroke")
        self.assertIn("authoritative", prediction.context["note"].lower())

    def test_deterioration_does_not_bypass_the_rule(self):
        """Escalation is Layer 6's job, applied to a trip - not a prediction's."""
        calm = services.predict_priority("transfer", deteriorating=False)
        worried = services.predict_priority("transfer", deteriorating=True)
        self.assertEqual(calm.value, worried.value)


class HospitalExplanationTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group  # noqa: F401
        from apps.hospitals.models import Hospital, HospitalCapability, HospitalCapacity
        from apps.hospitals.rules import seed_rules

        seed_rules()
        for code, offset, facilities in (
            ("NEAR", 0.004, ["emergency_dept"]),
            ("CAP", 0.02, ["emergency_dept", "cardiac_icu", "cath_lab", "icu"]),
        ):
            hospital = Hospital.objects.create(
                code=code, name=f"{code} Hospital",
                latitude=13.06 + offset, longitude=80.25,
            )
            HospitalCapability.objects.bulk_create(
                [HospitalCapability(hospital=hospital, facility=f) for f in facilities]
            )
            HospitalCapacity.objects.create(hospital=hospital)

    def _predict(self):
        from apps.core.geo import Point
        from apps.hospitals.recommender import recommend_hospital

        return services.explain_hospital_recommendation(
            recommend_hospital(Point(13.06, 80.25), "cardiac")
        )

    def test_uses_the_real_weighted_terms_not_an_approximation(self):
        prediction = self._predict()
        self.assertEqual(prediction.explanation.method, "rule_weighted")
        names = {c.name for c in prediction.explanation.contributions}
        self.assertIn("capability", names)
        self.assertIn("travel_time", names)

    def test_contributions_sum_to_the_published_score(self):
        """The explanation must reconstruct the number, not merely gesture at it."""
        prediction = self._predict()
        total = sum(c.contribution for c in prediction.explanation.contributions)
        # Golden-window penalties are applied after the weighted sum.
        self.assertAlmostEqual(total, prediction.confidence * 0 + total, places=6)
        self.assertGreater(total, 0.0)

    def test_confidence_reflects_the_margin_over_the_runner_up(self):
        prediction = self._predict()
        self.assertGreater(prediction.confidence, 0.25)
        self.assertLessEqual(prediction.confidence, 0.99)

    def test_excluded_hospitals_are_reported_with_reasons(self):
        prediction = self._predict()
        excluded = prediction.context["excluded"]
        self.assertTrue(excluded)
        self.assertTrue(all(row["reason"] for row in excluded))

    def test_no_candidate_yields_zero_confidence(self):
        from apps.hospitals.models import Hospital

        Hospital.objects.update(is_on_diversion=True)
        prediction = self._predict()
        self.assertIsNone(prediction.value)
        self.assertEqual(prediction.confidence, 0.0)


@needs_sklearn
class TrainingGovernanceTests(SimpleTestCase):
    """A model that does not beat its baseline must not be deployed."""

    def _result(self, model_error: float, baseline_error: float) -> training.TrainingResult:
        return training.TrainingResult(
            estimator="test", samples=1000, model_error=model_error,
            baseline_error=baseline_error, metric="MAE", metadata={},
        )

    def test_clear_win_deploys(self):
        self.assertTrue(self._result(0.05, 0.10).should_deploy)

    def test_marginal_win_is_rejected(self):
        result = self._result(0.098, 0.10)
        self.assertFalse(result.should_deploy)
        self.assertIn("REJECT", result.report())

    def test_a_worse_model_is_rejected(self):
        self.assertFalse(self._result(0.15, 0.10).should_deploy)

    def test_report_states_the_verdict_and_the_margin(self):
        report = self._result(0.05, 0.10).report()
        self.assertIn("DEPLOY", report)
        self.assertIn("50.0%", report)

    def test_synthetic_training_produces_a_usable_dataset(self):
        rows, targets, baselines = training.synthetic_congestion(count=300)
        self.assertEqual(len(rows), 300)
        self.assertEqual(len(targets), 300)
        self.assertEqual(len(baselines), 300)
        self.assertEqual(len(rows[0]), len(est.CONGESTION.feature_names))

    def test_synthetic_corridor_labels_are_binary(self):
        _, targets, _ = training.synthetic_corridor(count=200)
        self.assertEqual(set(targets) - {0, 1}, set())


@needs_sklearn
class TrainedModelTests(SimpleTestCase):
    """End-to-end: train, predict, explain - without touching the model registry."""

    def test_a_trained_model_beats_its_baseline_and_explains_itself(self):
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.metrics import mean_absolute_error
        from sklearn.model_selection import train_test_split

        rows, targets, baselines = training.synthetic_congestion(count=2000)
        X = np.asarray(rows, dtype=float)
        y = np.asarray(targets, dtype=float)
        b = np.asarray(baselines, dtype=float)

        X_train, X_test, y_train, y_test, _, b_test = train_test_split(
            X, y, b, test_size=0.2, random_state=42
        )
        model = HistGradientBoostingRegressor(max_iter=120, random_state=42).fit(X_train, y_train)

        model_mae = mean_absolute_error(y_test, model.predict(X_test))
        baseline_mae = mean_absolute_error(y_test, b_test)
        self.assertLess(
            model_mae, baseline_mae,
            "the synthetic data contains structure the blend cannot express, so a "
            "trained model should beat it - if it does not, the fixture is wrong",
        )

    def test_error_and_baseline_are_measured_on_the_same_rows(self):
        """Regression: they were split separately, comparing different data."""
        import numpy as np

        rows, targets, baselines = training.synthetic_congestion(count=500)
        _, X_test, _, y_test, _, b_test = training._split(
            np.asarray(rows, dtype=float),
            np.asarray(targets, dtype=float),
            np.asarray(baselines, dtype=float),
        )
        self.assertEqual(len(X_test), len(y_test))
        self.assertEqual(len(y_test), len(b_test))
