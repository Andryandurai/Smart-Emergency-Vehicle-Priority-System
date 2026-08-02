"""Prove a database migration lost nothing.

A dump/load that "looked fine" is not evidence. This command produces a
fingerprint of the data that is independent of the backend - row counts plus
a stable checksum over each table's primary keys and a few semantically
important columns - so the same fingerprint taken before and after a
SQLite -> PostgreSQL cutover can be compared field by field.

    # before, on SQLite
    python manage.py verify_migration --snapshot pre.json

    # after, pointed at PostgreSQL
    python manage.py verify_migration --compare pre.json

Exit status is non-zero on any mismatch, so it can gate a deployment script.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.apps import apps as app_registry
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

#: Columns whose values are compared, per model, beyond the primary key.
#: Chosen for semantic weight rather than completeness - a checksum over
#: every column would flag harmless representational differences (float
#: formatting, timestamp precision) as data loss.
FINGERPRINT_FIELDS: dict[str, tuple[str, ...]] = {
    "fleet.EmergencyVehicle": ("callsign", "vehicle_type", "status"),
    "fleet.Station": ("code", "name"),
    "fleet.VehicleTelemetry": ("vehicle_id",),
    "network.Intersection": ("name", "is_signalised"),
    "network.RoadSegment": ("from_node_id", "to_node_id", "road_class", "is_open"),
    "network.TrafficSignal": ("controller_id", "supports_preemption"),
    "network.CameraFeed": ("name",),
    "network.RoadEvent": ("event_type", "is_active"),
    "network.AccidentRecord": ("severity", "casualties"),
    "network.TrafficObservation": ("segment_id",),
    "network.TrafficProfile": ("segment_id", "weekday", "hour"),
    "hospitals.Hospital": ("code", "name", "is_active", "is_on_diversion"),
    "hospitals.HospitalCapability": ("hospital_id", "facility"),
    "hospitals.HospitalCapacity": ("hospital_id",),
    "hospitals.EmergencyRule": ("category", "is_active"),
    "hospitals.HospitalAlert": ("hospital_id", "emergency_category"),
    "hospitals.HospitalRecommendationLog": ("trip_id", "emergency_category"),
    "dispatch.EmergencyTrip": ("reference", "stage", "emergency_category", "priority_level"),
    "dispatch.RoutePlan": ("trip_id", "is_active", "algorithm"),
    "dispatch.SignalPreemption": ("trip_id", "signal_id", "state"),
    "dispatch.PriorityDirective": ("trip_id", "priority_level", "siren_mode"),
    "alerts.DisplayBoard": ("code", "name"),
    "alerts.DriverDevice": ("device_id",),
    "alerts.DriverAlert": ("trip_id", "channel"),
    "analytics.DailyMetric": ("date", "city"),
    "analytics.Hotspot": ("kind", "label"),
}

#: Models carrying coordinates - checked for spatial integrity as well.
GEO_MODELS = (
    "fleet.EmergencyVehicle", "fleet.Station", "fleet.VehicleTelemetry",
    "network.Intersection", "network.RoadSegment", "network.CameraFeed",
    "network.RoadEvent", "network.AccidentRecord", "hospitals.Hospital",
    "alerts.DisplayBoard", "alerts.DriverDevice", "alerts.DriverAlert",
    "analytics.Hotspot",
)


class Command(BaseCommand):
    help = "Fingerprint the database, or compare it against an earlier fingerprint."

    def add_arguments(self, parser):
        parser.add_argument("--snapshot", type=str, help="Write a fingerprint to this file.")
        parser.add_argument("--compare", type=str, help="Compare against this fingerprint.")
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        if not options["snapshot"] and not options["compare"]:
            options["quiet"] = False

        fingerprint = self._fingerprint()

        if options["snapshot"]:
            path = Path(options["snapshot"])
            path.write_text(json.dumps(fingerprint, indent=2, sort_keys=True))
            self.stdout.write(
                self.style.SUCCESS(
                    f"Fingerprint written to {path.resolve()} "
                    f"({fingerprint['totals']['rows']} rows across "
                    f"{fingerprint['totals']['models']} models on "
                    f"{fingerprint['database']['vendor']})"
                )
            )

        if options["compare"]:
            self._compare(Path(options["compare"]), fingerprint)
        elif not options["quiet"]:
            self._report(fingerprint)

    # ------------------------------------------------------------------ build
    def _fingerprint(self) -> dict:
        models: dict[str, dict] = {}
        total_rows = 0

        for label, fields in FINGERPRINT_FIELDS.items():
            app_label, model_name = label.split(".")
            model = app_registry.get_model(app_label, model_name)

            count = model.objects.count()
            total_rows += count
            entry = {"rows": count, "checksum": self._checksum(model, fields)}

            if label in GEO_MODELS:
                entry["spatial"] = self._spatial_summary(model)

            models[label] = entry

        return {
            "database": {
                "vendor": connection.vendor,
                "name": str(connection.settings_dict.get("NAME")),
            },
            "totals": {"models": len(models), "rows": total_rows},
            "models": models,
        }

    @staticmethod
    def _checksum(model, fields: tuple[str, ...]) -> str:
        """Order-independent digest over the chosen columns.

        Rows are hashed individually and the digests XORed, so the result does
        not depend on physical row order - which legitimately differs between
        SQLite and PostgreSQL and must not be mistaken for data loss.
        """
        accumulator = 0
        columns = ("pk",) + fields
        for row in model.objects.values_list(*columns).iterator(chunk_size=5000):
            rendered = "|".join("" if v is None else str(v) for v in row)
            digest = hashlib.sha256(rendered.encode()).digest()
            accumulator ^= int.from_bytes(digest[:16], "big")
        return f"{accumulator:032x}"

    @staticmethod
    def _spatial_summary(model) -> dict:
        """Coordinate extent, rounded so float formatting cannot cause noise."""
        from django.db.models import Max, Min

        bounds = model.objects.aggregate(
            min_lat=Min("latitude"), max_lat=Max("latitude"),
            min_lon=Min("longitude"), max_lon=Max("longitude"),
        )
        summary = {k: (round(v, 6) if v is not None else None) for k, v in bounds.items()}
        summary["null_coordinates"] = model.objects.filter(latitude__isnull=True).count()

        # On PostGIS, confirm the generated column agrees with the floats.
        from apps.core.spatial import postgis_available

        if postgis_available():
            table = model._meta.db_table
            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f'WHERE "geom" IS NOT NULL AND ('
                    f'  ABS(ST_Y("geom"::geometry) - "latitude") > 1e-9 OR'
                    f'  ABS(ST_X("geom"::geometry) - "longitude") > 1e-9)'
                )
                summary["geom_mismatches"] = cursor.fetchone()[0]
                cursor.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "geom" IS NULL')
                summary["geom_null"] = cursor.fetchone()[0]
        return summary

    # --------------------------------------------------------------- compare
    def _compare(self, path: Path, current: dict) -> None:
        if not path.exists():
            raise CommandError(f"No fingerprint at {path}")
        before = json.loads(path.read_text())

        self.stdout.write(
            f"\n  before: {before['database']['vendor']:12} "
            f"{before['totals']['rows']} rows / {before['totals']['models']} models"
        )
        self.stdout.write(
            f"  after : {current['database']['vendor']:12} "
            f"{current['totals']['rows']} rows / {current['totals']['models']} models\n"
        )

        problems: list[str] = []
        width = max(len(k) for k in before["models"])

        for label, old in sorted(before["models"].items()):
            new = current["models"].get(label)
            if new is None:
                problems.append(f"{label}: missing after migration")
                continue

            row_ok = old["rows"] == new["rows"]
            sum_ok = old["checksum"] == new["checksum"]
            mark = "ok  " if (row_ok and sum_ok) else "FAIL"
            self.stdout.write(
                f"  [{mark}] {label.ljust(width)}  {old['rows']:>6} -> {new['rows']:>6}"
                + ("" if sum_ok else "   checksum differs")
            )
            if not row_ok:
                problems.append(f"{label}: {old['rows']} rows before, {new['rows']} after")
            if not sum_ok:
                problems.append(f"{label}: content checksum changed")

            mismatches = (new.get("spatial") or {}).get("geom_mismatches")
            if mismatches:
                problems.append(f"{label}: {mismatches} geometry/coordinate mismatches")

        if problems:
            self.stdout.write("")
            for problem in problems:
                self.stdout.write(self.style.ERROR(f"  ! {problem}"))
            raise CommandError(f"Migration verification FAILED ({len(problems)} problem(s)).")

        self.stdout.write(
            self.style.SUCCESS("\n  Verified: every model matched on row count and checksum.")
        )

    def _report(self, fingerprint: dict) -> None:
        width = max(len(k) for k in fingerprint["models"])
        self.stdout.write(
            f"\nDatabase fingerprint ({fingerprint['database']['vendor']})\n"
        )
        for label, entry in sorted(fingerprint["models"].items()):
            spatial = entry.get("spatial") or {}
            note = ""
            if spatial.get("geom_null"):
                note = f"   {spatial['geom_null']} row(s) without geometry"
            if spatial.get("geom_mismatches"):
                note = f"   {spatial['geom_mismatches']} MISMATCHED geometry"
            self.stdout.write(
                f"  {label.ljust(width)}  {entry['rows']:>6} rows  {entry['checksum'][:12]}{note}"
            )
        self.stdout.write(
            f"\n  {fingerprint['totals']['rows']} rows across "
            f"{fingerprint['totals']['models']} models"
        )
