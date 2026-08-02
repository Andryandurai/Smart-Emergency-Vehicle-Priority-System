import logging

from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.dispatch import receiver

log = logging.getLogger("sevps.core")


@receiver(connection_created)
def configure_sqlite(sender, connection, **kwargs):
    """Make SQLite survive the multi-process deployment SEVPS actually uses.

    The platform normally runs at least two processes against one database -
    the ASGI server and ``sevps_worker`` (plus ``simulate`` during a demo).
    SQLite's default rollback journal permits a single writer and blocks
    readers while it writes, so that topology deadlocks almost immediately
    with "database is locked".

    * ``journal_mode=WAL``  - readers no longer block on the writer, which is
      the actual fix; the ASGI server keeps serving dashboards while the
      worker writes.
    * ``busy_timeout``      - wait for a contended write rather than failing
      instantly. Paired with WAL, contention becomes a short wait.
    * ``synchronous=NORMAL``- safe under WAL (a crash can lose the last
      commit, not corrupt the database) and much faster than FULL.

    Postgres needs none of this, so the handler is a no-op there.
    """
    if connection.vendor != "sqlite":
        return
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=20000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
    except Exception:  # pragma: no cover - never block startup on a pragma
        log.warning("could not apply SQLite tuning pragmas", exc_info=True)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "SEVPS Core"
