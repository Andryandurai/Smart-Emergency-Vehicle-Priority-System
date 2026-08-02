# SEVPS application image.
#
# Deliberately slim: no GDAL, no build toolchain in the final layer. SEVPS
# reaches PostGIS through generated geography columns and raw spatial SQL
# rather than GeoDjango model fields, so libgdal is not a runtime dependency.
# Set SEVPS_USE_GEODJANGO=1 only if you want GeoDjango's ORM expressions, and
# uncomment the GDAL line below to match.

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
# Dependencies are installed in their own layer so application edits do not
# invalidate the (slow) pip step.
# ---------------------------------------------------------------------------
FROM base AS deps

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
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
RUN mkdir -p /app/staticfiles /app/media && chown -R sevps:sevps /app
USER sevps

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health/ || exit 1

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "sevps.asgi:application"]
