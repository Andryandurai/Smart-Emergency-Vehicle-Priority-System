# SEVPS application image.
#
# Four stages, each there for a reason:
#
#   frontend  - builds the React console with Node, so the image does not
#               depend on someone having run `npm run build` on the host. A
#               `frontend/dist` baked in from a developer laptop is how a
#               deployment ends up serving last week's UI against this week's
#               API, with nothing visible from the outside to say so.
#   base      - runtime OS packages only.
#   deps      - Python wheels, in their own layer so an application edit does
#               not invalidate the (slow) pip step.
#   runtime   - the shipped image. No Node, no build toolchain, no GDAL.
#
# GDAL is deliberately absent: SEVPS reaches PostGIS through generated
# geography columns and raw spatial SQL rather than GeoDjango model fields.
# Set SEVPS_USE_GEODJANGO=1 only if you want GeoDjango's ORM expressions, and
# uncomment the libgdal line below to match.

# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build

# package*.json first: the dependency install is cached until they actually
# change, which is the difference between a 5-second and a 90-second rebuild.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# `npm run build` is `tsc --noEmit && vite build`, so a type error fails the
# image build rather than shipping and failing in someone's browser.
RUN npm run build


# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq is needed by psycopg at runtime; curl is used by the healthcheck.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libpq5 \
        curl \
    # && apt-get install --no-install-recommends -y libgdal32 gdal-bin  # SEVPS_USE_GEODJANGO=1
    && rm -rf /var/lib/apt/lists/*


# ---------------------------------------------------------------------------
FROM base AS deps

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# psycopg and channels-redis are installed here rather than pinned into
# requirements.txt: a laptop pilot runs on SQLite with the in-memory channel
# layer and needs neither, and requiring them would make a plain
# `pip install -r requirements.txt` fail without a Postgres toolchain present.
RUN pip install --prefix=/install -r requirements.txt \
 && pip install --prefix=/install "psycopg[binary]==3.2.1" "channels-redis==4.2.0"


# ---------------------------------------------------------------------------
FROM base AS runtime

COPY --from=deps /install /usr/local

WORKDIR /app

# Run as a non-root user: this container terminates traffic-signal commands,
# and a compromise should not also own the filesystem.
RUN useradd --create-home --uid 10001 sevps

COPY --chown=sevps:sevps . .
# The built console comes from the Node stage, never from the host.
COPY --from=frontend --chown=sevps:sevps /build/dist ./frontend/dist

RUN mkdir -p /app/staticfiles /app/media /app/models \
 && chown -R sevps:sevps /app \
 && chmod +x /app/docker/entrypoint.sh

USER sevps

EXPOSE 8000

# Liveness, not readiness. `/health/ready/` checks the database; using it here
# would have Docker restart a perfectly healthy application every time
# Postgres blinked - which cannot fix anything and makes recovery slower.
# See apps/core/views.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health/live/ || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
