"""Populate the PostGIS LineString columns from the stored JSON polylines.

Point columns need no backfill - they are ``GENERATED ALWAYS`` from
latitude/longitude and PostgreSQL fills them on write. Polylines do, because
the JSON holds ``[[lat, lon], ...]`` while PostGIS expects ``(x=lon, y=lat)``;
that axis swap needs an array walk, so it cannot be a generated expression.

Idempotent and resumable: re-running only touches rows whose geometry column
is still NULL unless ``--all`` is given.

    python manage.py backfill_geometry
    python manage.py backfill_geometry --all --batch 2000
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.core.spatial import LINESTRING_TABLES, postgis_available, reset_backend_cache


class Command(BaseCommand):
    help = "Fill PostGIS LineString columns from JSON polylines (PostgreSQL only)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Rewrite every row, not just NULLs.")
        parser.add_argument("--batch", type=int, default=1000)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        reset_backend_cache()
        if not postgis_available():
            raise CommandError(
                "PostGIS is not active on this connection.\n"
                f"  vendor: {connection.vendor}\n"
                "Point geometry needs no backfill on SQLite - the haversine "
                "backend reads latitude/longitude directly."
            )

        from django.apps import apps as app_registry

        total = 0
        for app_label, model_name, json_field in LINESTRING_TABLES:
            model = app_registry.get_model(app_label, model_name)
            total += self._backfill(model, json_field, options)

        self.stdout.write(self.style.SUCCESS(f"\nBackfilled {total} row(s)."))

    def _backfill(self, model, json_field: str, options) -> int:
        table = model._meta.db_table
        queryset = model.objects.all()
        if not options["all"]:
            queryset = queryset.extra(where=[f'"{table}"."path" IS NULL'])

        pending = queryset.count()
        self.stdout.write(f"  {table}: {pending} row(s) to backfill")
        if options["dry_run"] or not pending:
            return 0

        written = 0
        batch: list[tuple] = []
        for row in queryset.only("pk", json_field).iterator(chunk_size=options["batch"]):
            polyline = getattr(row, json_field) or []
            wkt = self._to_wkt(polyline)
            if wkt is None:
                continue
            batch.append((wkt, row.pk))
            if len(batch) >= options["batch"]:
                written += self._flush(table, batch)
                batch = []
        written += self._flush(table, batch)

        self.stdout.write(f"    wrote {written}")
        return written

    @staticmethod
    def _to_wkt(polyline) -> str | None:
        """``[[lat, lon], ...]`` -> ``LINESTRING(lon lat, ...)``.

        A LineString needs at least two distinct vertices; degenerate shapes
        are skipped rather than written as invalid geometry.
        """
        points = []
        for pair in polyline:
            if not pair or len(pair) < 2:
                continue
            lat, lon = float(pair[0]), float(pair[1])
            coordinate = f"{lon} {lat}"
            if not points or points[-1] != coordinate:
                points.append(coordinate)
        if len(points) < 2:
            return None
        return f"LINESTRING({', '.join(points)})"

    @staticmethod
    def _flush(table: str, batch: list[tuple]) -> int:
        if not batch:
            return 0
        with connection.cursor() as cursor:
            cursor.executemany(
                f'UPDATE "{table}" SET "path" = ST_GeogFromText(%s) WHERE id = %s',
                batch,
            )
        return len(batch)
