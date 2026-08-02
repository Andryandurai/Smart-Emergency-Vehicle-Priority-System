"""Background maintenance loop.

Three jobs that must happen on a timer rather than in response to a request:

* **Corridor safety sweep** - force-release any signal hold that outlived its
  window.  Without this, a vehicle losing GPS mid-corridor would leave a
  junction green indefinitely.
* **Computer-vision sweep** - analyse camera feeds and feed the results back
  into segment speeds and road events.
* **Display board expiry** - blank signs whose message has lapsed.

In production this is a Celery beat schedule; run as a management command it
needs no broker, which keeps a pilot deployment to two processes.

    python manage.py sevps_worker
    python manage.py sevps_worker --no-vision --interval 5
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.alerts.dispatcher import clear_expired_boards
from apps.dispatch.corridor import tick_corridors
from apps.network.models import CameraFeed
from apps.network.vision import analyse_camera, cv_backend, ingest_camera_analysis


class Command(BaseCommand):
    help = "Run SEVPS background maintenance (corridor sweep, CV, board expiry)."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=5.0, help="Seconds between sweeps.")
        parser.add_argument("--vision-every", type=int, default=6, help="Run CV every N sweeps.")
        parser.add_argument("--no-vision", action="store_true")
        parser.add_argument("--rollup-hour", type=int, default=1, help="Hour to roll up metrics.")

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(
            f"SEVPS worker started (interval {options['interval']}s, CV backend: {cv_backend()})"
        ))
        sweep = 0
        last_rollup_date = None

        try:
            while True:
                sweep += 1
                corridor = tick_corridors()
                if corridor["released"] or corridor["expired"]:
                    self.stdout.write(
                        f"  corridor sweep: released {corridor['released']}, "
                        f"expired {corridor['expired']}"
                    )

                cleared = clear_expired_boards()
                if cleared:
                    self.stdout.write(f"  cleared {cleared} display board(s)")

                if not options["no_vision"] and sweep % options["vision_every"] == 0:
                    self._run_vision()

                now = timezone.localtime()
                if now.hour == options["rollup_hour"] and last_rollup_date != now.date():
                    from apps.analytics.services import (
                        identify_accident_hotspots,
                        rollup_daily_metrics,
                    )

                    metric = rollup_daily_metrics(now.date() - timezone.timedelta(days=1))
                    hotspots = identify_accident_hotspots()
                    last_rollup_date = now.date()
                    self.stdout.write(self.style.SUCCESS(
                        f"  daily rollup for {metric.date}: {metric.trips_total} trips, "
                        f"{len(hotspots)} accident hotspots"
                    ))

                time.sleep(options["interval"])
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nWorker stopped."))

    def _run_vision(self) -> None:
        cameras = CameraFeed.objects.filter(is_active=True).select_related("segment")
        analysed = incidents = 0
        for camera in cameras:
            try:
                analysis = analyse_camera(camera)
            except Exception as exc:  # pragma: no cover - a bad feed must not stop the loop
                self.stderr.write(f"  camera {camera.name} failed: {exc}")
                continue
            _, events = ingest_camera_analysis(camera, analysis)
            analysed += 1
            incidents += len(events)
        if analysed:
            message = f"  vision sweep: {analysed} camera(s)"
            if incidents:
                message += f", {incidents} new incident(s)"
            self.stdout.write(message)
