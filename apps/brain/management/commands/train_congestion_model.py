"""Train the optional gradient-boosting congestion model.

SEVPS ships with a statistical predictor that needs no dependencies and works
from day one.  This command trains a scikit-learn model on accumulated
observations for deployments where enough history exists to beat it - and
reports the comparison honestly, so the ML model is only adopted if it wins.

    pip install scikit-learn numpy joblib
    python manage.py train_congestion_model --days 60 --out models/congestion.joblib

Then set ``SEVPS_CONGESTION_MODEL_PATH=models/congestion.joblib``.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.brain.congestion import _ROAD_CLASS_INDEX, default_speed_factor
from apps.network.models import RoadSegment, TrafficObservation, TrafficProfile


class Command(BaseCommand):
    help = "Train a scikit-learn congestion (speed factor) model from observation history."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=60)
        parser.add_argument("--out", type=str, default="models/congestion.joblib")
        parser.add_argument("--horizon-min", type=int, default=15, help="Prediction horizon.")
        parser.add_argument("--min-samples", type=int, default=500)

    def handle(self, *args, **options):
        try:
            import joblib
            import numpy as np
            from sklearn.ensemble import HistGradientBoostingRegressor
            from sklearn.metrics import mean_absolute_error
            from sklearn.model_selection import train_test_split
        except ImportError as exc:
            raise CommandError(
                "scikit-learn, numpy and joblib are required:\n"
                "    pip install scikit-learn numpy joblib"
            ) from exc

        since = timezone.now() - timezone.timedelta(days=options["days"])
        segments = {
            s.id: s
            for s in RoadSegment.objects.all().only("id", "free_flow_kmh", "road_class", "lanes")
        }
        profiles = {
            (p.segment_id, p.weekday, p.hour): p.speed_factor
            for p in TrafficProfile.objects.all().only("segment_id", "weekday", "hour", "speed_factor")
        }

        # Build (features at time t) -> (speed factor at t + horizon) pairs.
        horizon = timezone.timedelta(minutes=options["horizon_min"])
        by_segment: dict[int, list] = {}
        for observation in (
            TrafficObservation.objects.filter(observed_at__gte=since)
            .only("segment_id", "observed_at", "speed_kmh")
            .order_by("segment_id", "observed_at")
            .iterator(chunk_size=5000)
        ):
            by_segment.setdefault(observation.segment_id, []).append(observation)

        features, targets = [], []
        for segment_id, rows in by_segment.items():
            segment = segments.get(segment_id)
            if segment is None:
                continue
            design = segment.design_speed_kmh
            for index, current in enumerate(rows):
                future = self._find_future(rows, index, current.observed_at + horizon)
                if future is None:
                    continue
                local = timezone.localtime(current.observed_at)
                historical = profiles.get(
                    (segment_id, local.weekday(), local.hour), default_speed_factor(local)
                )
                features.append(
                    [
                        local.weekday(), local.hour, local.minute,
                        _ROAD_CLASS_INDEX.get(segment.road_class, 3),
                        design, segment.lanes,
                        min(1.3, current.speed_kmh / design),
                        options["horizon_min"],
                        historical,
                    ]
                )
                targets.append(min(1.3, future.speed_kmh / design))

        if len(features) < options["min_samples"]:
            raise CommandError(
                f"Only {len(features)} training samples (need {options['min_samples']}). "
                "Let the simulator or live feed run longer, then retry."
            )

        X = np.asarray(features, dtype=float)
        y = np.asarray(targets, dtype=float)
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

        model = HistGradientBoostingRegressor(
            max_iter=300, learning_rate=0.08, max_depth=6, random_state=42
        )
        model.fit(X_train, y_train)

        model_mae = mean_absolute_error(y_test, model.predict(X_test))
        # Baseline the built-in predictor actually uses: persist the live value.
        baseline_mae = mean_absolute_error(y_test, X_test[:, 6])
        # And the pure historical-profile baseline.
        historical_mae = mean_absolute_error(y_test, X_test[:, 8])

        out = Path(options["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, out)

        improvement = (1 - model_mae / baseline_mae) * 100 if baseline_mae else 0.0
        self.stdout.write(self.style.SUCCESS(f"\nModel written to {out.resolve()}"))
        self.stdout.write(
            "\n".join(
                [
                    f"  samples                {len(features)}",
                    f"  MAE (this model)       {model_mae:.4f}",
                    f"  MAE (persistence)      {baseline_mae:.4f}",
                    f"  MAE (historical only)  {historical_mae:.4f}",
                    f"  improvement vs persistence  {improvement:+.1f}%",
                    "",
                ]
            )
        )
        if model_mae >= min(baseline_mae, historical_mae):
            self.stdout.write(self.style.WARNING(
                "The trained model does NOT beat the built-in predictor on this data. "
                "Leave SEVPS_CONGESTION_MODEL_PATH unset and collect more history."
            ))
        else:
            self.stdout.write(
                f"Enable it with:  SEVPS_CONGESTION_MODEL_PATH={out}"
            )

    @staticmethod
    def _find_future(rows, start_index: int, target_time):
        """Nearest observation at or after ``target_time``, within 10 minutes."""
        for row in rows[start_index + 1 :]:
            if row.observed_at >= target_time:
                if (row.observed_at - target_time).total_seconds() <= 600:
                    return row
                return None
        return None
