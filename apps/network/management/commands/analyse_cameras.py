"""One-shot computer-vision sweep over the camera estate (feature 4.5).

    python manage.py analyse_cameras
    python manage.py analyse_cameras --camera 12 --verbose
"""
from django.core.management.base import BaseCommand

from apps.network.models import CameraFeed
from apps.network.cv import pipeline as cv_pipeline
from apps.network.cv.backends import backend_name as cv_backend
from apps.network.cv.pipeline import analyse_camera, ingest


class Command(BaseCommand):
    help = "Analyse traffic camera feeds and ingest the results."

    def add_arguments(self, parser):
        parser.add_argument("--camera", type=int, action="append", help="Limit to camera id(s).")
        parser.add_argument("--verbose-output", action="store_true")

    def handle(self, *args, **options):
        cameras = CameraFeed.objects.filter(is_active=True).select_related("segment")
        if options["camera"]:
            cameras = cameras.filter(id__in=options["camera"])

        self.stdout.write(f"CV backend: {cv_backend()}")
        analysed = incidents = 0
        for camera in cameras:
            try:
                analysis = analyse_camera(camera)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"{camera.name}: {exc}"))
                continue
            _, events = ingest(camera, analysis)
            analysed += 1
            incidents += len(events)
            if options["verbose_output"]:
                self.stdout.write(
                    f"  {camera.name}: {analysis.vehicle_count} vehicles, "
                    f"{analysis.estimated_speed_kmh:.0f} km/h, {analysis.congestion_level}"
                    + (f" -> {len(events)} incident(s)" if events else "")
                )

        self.stdout.write(self.style.SUCCESS(
            f"Analysed {analysed} camera(s); {incidents} new road event(s) created."
        ))
