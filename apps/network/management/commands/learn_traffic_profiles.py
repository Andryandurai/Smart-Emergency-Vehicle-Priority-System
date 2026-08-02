"""Learn per-segment weekday/hour speed profiles from observation history.

These profiles are the "historical traffic patterns" input to the AI engine.
Storing a *speed factor* (observed / free-flow) rather than an absolute speed
means the profile transfers cleanly if a road is re-classified, and makes the
learned pattern directly comparable across roads of different design speeds.

    python manage.py learn_traffic_profiles --days 30
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.network.models import RoadSegment, TrafficObservation, TrafficProfile


class Command(BaseCommand):
    help = "Recompute historical traffic profiles from observations."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="History window.")
        parser.add_argument("--min-samples", type=int, default=3, help="Minimum samples per cell.")

    def handle(self, *args, **options):
        since = timezone.now() - timezone.timedelta(days=options["days"])
        design = {
            s.id: s.design_speed_kmh
            for s in RoadSegment.objects.all().only("id", "free_flow_kmh", "road_class")
        }

        buckets: dict[tuple[int, int, int], list[float]] = defaultdict(list)
        queryset = (
            TrafficObservation.objects.filter(observed_at__gte=since)
            .only("segment_id", "observed_at", "speed_kmh")
            .iterator(chunk_size=5000)
        )
        scanned = 0
        for observation in queryset:
            free_flow = design.get(observation.segment_id)
            if not free_flow:
                continue
            local = timezone.localtime(observation.observed_at)
            buckets[(observation.segment_id, local.weekday(), local.hour)].append(
                min(1.3, observation.speed_kmh / free_flow)
            )
            scanned += 1

        written = 0
        for (segment_id, weekday, hour), values in buckets.items():
            if len(values) < options["min_samples"]:
                continue
            TrafficProfile.objects.update_or_create(
                segment_id=segment_id,
                weekday=weekday,
                hour=hour,
                defaults={
                    "speed_factor": round(sum(values) / len(values), 4),
                    "sample_count": len(values),
                },
            )
            written += 1

        self.stdout.write(self.style.SUCCESS(
            f"Scanned {scanned} observations -> {written} profile cells "
            f"({len(buckets) - written} cells below the sample threshold)."
        ))
