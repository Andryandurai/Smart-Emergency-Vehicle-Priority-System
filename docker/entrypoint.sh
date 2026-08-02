#!/bin/sh
# SEVPS container entrypoint.
#
# Takes a role as its argument (web | worker | simulate | migrate | shell) so
# every container runs the same image and differs only by what it is told to
# be. That matters here because the web and worker processes must agree
# exactly about code and migrations - a worker one deploy behind the web tier
# is a worker preempting signals with stale routing logic.
#
# Two things this fixes about running `migrate` inline in a compose command:
#
#   1. **Concurrency.** With two web replicas, both ran `migrate` at once.
#      Django has no internal lock; concurrent migrations on the same database
#      race and can half-apply. A Postgres advisory lock serialises them.
#   2. **Ordering.** The worker started as soon as the web *container* did, not
#      when migrations finished, so it could query a table that did not exist
#      yet and crash-loop until Docker's restart backoff hid the cause.
set -eu

ROLE="${1:-web}"
shift || true

log() { echo "[entrypoint] $*"; }

# ---------------------------------------------------------------------------
# Wait for the database rather than crash-looping against it. Compose's
# `service_healthy` covers the normal case; this covers managed databases
# (RDS, Cloud SQL) where there is no healthcheck to depend on and a failover
# can take the endpoint away for tens of seconds mid-deploy.
# ---------------------------------------------------------------------------
wait_for_db() {
    [ "${SEVPS_DB_ENGINE:-sqlite}" = "sqlite" ] && return 0

    attempts="${SEVPS_DB_WAIT_ATTEMPTS:-60}"
    i=1
    while [ "$i" -le "$attempts" ]; do
        if python -c "
import sys
from django.db import connections
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sevps.settings')
django.setup()
try:
    connections['default'].cursor()
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
            log "database reachable"
            return 0
        fi
        log "waiting for database ($i/$attempts)"
        i=$((i + 1))
        sleep 2
    done
    log "database unreachable after $attempts attempts"
    return 1
}

# ---------------------------------------------------------------------------
# Migrate under an advisory lock so concurrent replicas serialise instead of
# racing. On SQLite there is nothing to coordinate - one process, one file.
# ---------------------------------------------------------------------------
run_migrations() {
    if [ "${SEVPS_DB_ENGINE:-sqlite}" = "sqlite" ]; then
        python manage.py migrate --noinput
        return
    fi

    log "acquiring migration lock"
    python - <<'PY'
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sevps.settings")
django.setup()

from django.core.management import call_command
from django.db import connection

# A session-scoped advisory lock: the second replica blocks here until the
# first has finished, then finds every migration already applied and does
# nothing. The key is arbitrary but must be stable across deploys.
LOCK_KEY = 87310219
with connection.cursor() as cursor:
    cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
    try:
        call_command("migrate", interactive=False, verbosity=1)
    finally:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
PY
}

collect_static() {
    # --clear is deliberately absent: the volume is shared with nginx, and
    # emptying it mid-deploy serves 404s for the seconds it takes to refill.
    python manage.py collectstatic --noinput --verbosity 0
    log "static files collected"
}

ensure_vapid() {
    # Phase 9: without a key, push is silently undeliverable. Generating one
    # here would be wrong - a key regenerated on every container start
    # invalidates every push subscription in the fleet - so this only warns.
    if [ -z "${SEVPS_VAPID_PRIVATE_KEY:-}" ] && [ ! -f "${SEVPS_VAPID_KEY_PATH:-/app/models/vapid_private.pem}" ]; then
        log "WARNING: no VAPID key. Web Push will not deliver."
        log "         Run 'manage.py generate_vapid_keys --print-only' and set"
        log "         SEVPS_VAPID_PRIVATE_KEY, or mount a key into /app/models."
    fi
}

case "$ROLE" in
    web)
        wait_for_db
        run_migrations
        collect_static
        ensure_vapid
        log "starting ASGI server"
        # One worker process per container. Scaling is horizontal, via
        # replicas behind nginx, so that the channel layer - not a process
        # pool - is what fans events out. In-process concurrency would give
        # each replica its own in-memory group registry if Redis were ever
        # misconfigured, and the failure would look like "some dashboards
        # update, some do not".
        exec daphne -b 0.0.0.0 -p 8000 --proxy-headers sevps.asgi:application "$@"
        ;;

    worker)
        wait_for_db
        # Deliberately does NOT migrate: exactly one role owns schema changes.
        log "starting maintenance worker"
        exec python manage.py sevps_worker "$@"
        ;;

    migrate)
        wait_for_db
        run_migrations
        collect_static
        log "migrations complete"
        ;;

    simulate)
        wait_for_db
        exec python manage.py simulate "$@"
        ;;

    shell)
        exec "$@"
        ;;

    *)
        # Anything else is passed through, so `docker run image manage.py ...`
        # works without the entrypoint having to know every command.
        exec "$ROLE" "$@"
        ;;
esac
