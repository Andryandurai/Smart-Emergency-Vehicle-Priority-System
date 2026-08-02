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
from apps.core.live import tick_all as tick_live
from apps.dispatch.corridor import tick_corridors
from apps.network.cv import pipeline as cv_pipeline
from apps.network.cv.backends import backend_name as cv_backend


class Command(BaseCommand):
    help = "Run SEVPS background maintenance (corridor sweep, CV, board expiry)."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=5.0, help="Seconds between sweeps.")
        parser.add_argument("--vision-every", type=int, default=6, help="Run CV every N sweeps.")
        parser.add_argument("--no-vision", action="store_true")
        parser.add_argument(
            "--no-live", action="store_true",
            help="Skip the ETA/traffic/fleet live push sweep.",
        )
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

                # Live pushes that no GPS fix would trigger: ETA decay, traffic
                # changes from the CV sweep, and vehicles that have gone silent.
                if not options["no_live"]:
                    live = tick_live()
                    eta = live.get("eta", {})
                    if eta.get("eta_pushed"):
                        self.stdout.write(
                            f"  live: {eta['eta_pushed']} ETA update(s)"
                            + (f", {eta['stalled']} stalled" if eta.get("stalled") else "")
                        )
                    if live.get("traffic", {}).get("segments_changed"):
                        self.stdout.write(
                            f"  live: {live['traffic']['segments_changed']} segment(s) changed"
                        )
                    if live.get("fleet", {}).get("stale_vehicles"):
                        self.stdout.write(
                            self.style.WARNING(
                                f"  live: {live['fleet']['stale_vehicles']} vehicle(s) silent"
                            )
                        )

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
        """One pass over the camera estate; a bad feed never stops the loop."""
        result = cv_pipeline.sweep()
        if not result["cameras_analysed"] and not result["cameras_failed"]:
            return

        message = f"  vision sweep: {result['cameras_analysed']} camera(s)"
        if result["events_created"]:
            message += f", {result['events_created']} new incident(s)"
        if result["findings"]:
            message += f", findings={result['findings']}"
        self.stdout.write(message)

        if result["cameras_failed"]:
            self.stdout.write(
                self.style.WARNING(f"  {result['cameras_failed']} camera(s) unreadable")
            )
        for sighting in result["emergency_sightings"]:
            if sighting["visually_corroborated"]:
                self.stdout.write(
                    f"  camera confirms {sighting['callsign']} "
                    f"({sighting['distance_m']} m, conf {sighting['confidence']})"
                )
