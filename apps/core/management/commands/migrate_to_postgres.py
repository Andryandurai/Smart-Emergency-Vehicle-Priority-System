"""Move a SEVPS deployment from SQLite to PostgreSQL/PostGIS without data loss.

Runs the whole cutover as one guided sequence and refuses to continue at the
first sign of trouble:

1. fingerprint the source database          (``verify_migration --snapshot``)
2. dump every SEVPS app plus auth/authtoken (natural keys, so primary keys
   are not assumed to survive)
3. check the target is reachable, PostGIS-enabled and **empty**
4. migrate the schema on the target
5. load the dump
6. fingerprint the target and compare against step 1
7. backfill LineString geometry

The dump is written to disk and kept. If step 5 or 6 fails, the source
database has not been touched and the dump can be replayed by hand.

    python manage.py migrate_to_postgres --target postgres
    python manage.py migrate_to_postgres --target postgres --dump-only

``--target`` names an entry in ``DATABASES``; see ``sevps/settings.py`` for
how ``SEVPS_DB_ENGINE=postgres`` provisions one.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

#: Order matters on load: content types and auth first, then SEVPS data.
DUMP_APPS = [
    "auth", "authtoken", "contenttypes",
    "core", "fleet", "network", "hospitals", "dispatch", "alerts", "analytics",
]

#: Tables that must be empty on the target, or the load would collide.
GUARD_MODELS = ("fleet.EmergencyVehicle", "network.Intersection", "hospitals.Hospital")


class Command(BaseCommand):
    help = "Migrate SQLite -> PostgreSQL/PostGIS with verification at both ends."

    def add_arguments(self, parser):
        parser.add_argument("--target", default="postgres", help="DATABASES alias to migrate into.")
        parser.add_argument("--dump", default="migration/sevps_dump.json")
        parser.add_argument("--snapshot", default="migration/pre_migration.json")
        parser.add_argument("--dump-only", action="store_true", help="Stop after the dump.")
        parser.add_argument(
            "--allow-nonempty", action="store_true",
            help="Load into a target that already holds SEVPS rows. Rarely correct.",
        )

    def handle(self, *args, **options):
        target = options["target"]
        dump_path = Path(options["dump"])
        snapshot_path = Path(options["snapshot"])
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        self._step(1, "Fingerprinting the source database")
        call_command("verify_migration", snapshot=str(snapshot_path), quiet=True)

        self._step(2, f"Dumping data to {dump_path}")
        with dump_path.open("w", encoding="utf-8") as handle:
            call_command(
                "dumpdata", *DUMP_APPS,
                natural_foreign=True, natural_primary=True,
                indent=2, stdout=handle,
                # Sessions are per-deployment and token blacklists are
                # regenerated; carrying them across adds risk for no value.
                exclude=["sessions.session", "token_blacklist.outstandingtoken",
                         "token_blacklist.blacklistedtoken"],
            )
        size_mb = dump_path.stat().st_size / 1_048_576
        self.stdout.write(f"      {size_mb:.2f} MB written")

        if options["dump_only"]:
            self.stdout.write(self.style.SUCCESS("\nDump complete (--dump-only)."))
            return

        self._step(3, f"Checking target connection {target!r}")
        self._check_target(target, options["allow_nonempty"])

        self._step(4, "Applying migrations on the target")
        call_command("migrate", database=target, interactive=False, verbosity=1)

        self._step(5, "Loading data into the target")
        call_command("loaddata", str(dump_path), database=target, verbosity=1)

        self._step(6, "Verifying the target against the source fingerprint")
        self._run_on_target(target, ["verify_migration", "--compare", str(snapshot_path)])

        self._step(7, "Backfilling PostGIS LineString geometry")
        self._run_on_target(target, ["backfill_geometry"])

        self.stdout.write(
            self.style.SUCCESS(
                "\nMigration complete and verified.\n\n"
                "  Switch over by setting:\n"
                "    SEVPS_DB_ENGINE=postgres\n"
                "    SEVPS_ENABLE_POSTGIS=1\n\n"
                f"  The dump is kept at {dump_path} and the source database is untouched.\n"
            )
        )

    # ------------------------------------------------------------------ steps
    def _step(self, number: int, title: str) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n[{number}/7] {title}"))

    def _check_target(self, target: str, allow_nonempty: bool) -> None:
        if target not in connections:
            raise CommandError(
                f"No database alias {target!r}. Configure it in settings.DATABASES "
                f"(set SEVPS_DB_ENGINE=postgres)."
            )

        connection = connections[target]
        try:
            connection.ensure_connection()
        except Exception as exc:
            # Distinguish "no driver" from "no server": they need different
            # fixes, and Django's own message for the former is unhelpful.
            message = str(exc)
            if "psycopg" in message:
                raise CommandError(
                    "The PostgreSQL driver is not installed.\n"
                    "    pip install 'psycopg[binary]==3.2.1'\n"
                    "(It is listed in requirements.txt under the PostgreSQL section.)"
                ) from exc
            raise CommandError(
                f"Cannot reach target database {target!r}.\n"
                f"    {message.strip()}\n\n"
                f"    host={connection.settings_dict.get('HOST')} "
                f"port={connection.settings_dict.get('PORT')} "
                f"name={connection.settings_dict.get('NAME')}\n\n"
                "Start PostgreSQL first - `docker compose up -d db` brings up\n"
                "postgis/postgis with the extension already available."
            ) from exc

        if connection.vendor != "postgresql":
            raise CommandError(f"Target {target!r} is {connection.vendor}, not PostgreSQL.")

        with connection.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'postgis'")
            if cursor.fetchone() is None:
                self.stdout.write(
                    self.style.WARNING(
                        "      PostGIS extension not present yet - the spatial "
                        "migration will create it."
                    )
                )
            else:
                cursor.execute("SELECT PostGIS_Lib_Version()")
                self.stdout.write(f"      PostGIS {cursor.fetchone()[0]}")

        if allow_nonempty:
            return

        from django.apps import apps as app_registry

        for label in GUARD_MODELS:
            app_label, model_name = label.split(".")
            model = app_registry.get_model(app_label, model_name)
            try:
                existing = model.objects.using(target).count()
            except Exception:
                continue        # table not created yet - expected before step 4
            if existing:
                raise CommandError(
                    f"Target already holds {existing} {label} row(s). Loading on top "
                    f"would duplicate or collide. Drop and recreate the database, or "
                    f"pass --allow-nonempty if you are certain."
                )

    def _run_on_target(self, target: str, argv: list[str]) -> None:
        """Run a management command with the target as the default database.

        A subprocess, not ``call_command(database=...)``: the verification and
        backfill commands introspect ``django.db.connection`` (the *default*
        alias) to decide whether PostGIS is active, so they have to run in a
        process where the target genuinely is the default.
        """
        import os

        env = os.environ.copy()
        env["SEVPS_DB_ENGINE"] = "postgres"
        env["SEVPS_ENABLE_POSTGIS"] = "1"

        result = subprocess.run(
            [sys.executable, "manage.py", *argv],
            env=env, capture_output=True, text=True,
        )
        self.stdout.write(result.stdout.rstrip())
        if result.returncode != 0:
            self.stdout.write(self.style.ERROR(result.stderr.rstrip()))
            raise CommandError(
                f"`manage.py {' '.join(argv)}` failed against the target database. "
                "The source database has not been modified."
            )
