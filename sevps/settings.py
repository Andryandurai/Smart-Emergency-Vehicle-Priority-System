"""
Django settings for the SEVPS platform.

SEVPS boots with zero configuration (SQLite + in-memory channel layer) so a
pilot can be demonstrated on a laptop, and scales to PostgreSQL/PostGIS +
Redis + ASGI workers for a city-wide deployment by setting env vars only.
"""
from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("SEVPS_SECRET_KEY", "dev-insecure-key-do-not-use-in-production")
DEBUG = env_bool("SEVPS_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in env("SEVPS_ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in env("SEVPS_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

INSTALLED_APPS = [
    "daphne",  # must precede staticfiles so runserver becomes ASGI-aware
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # third party
    "rest_framework",
    "rest_framework.authtoken",           # legacy token auth, kept during migration
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "channels",
    # SEVPS layers
    "apps.core",
    "apps.fleet",       # Layer 1 - emergency vehicle tracking
    "apps.network",     # road graph, signals, cameras, incidents
    "apps.brain",       # Layer 2 - AI traffic intelligence engine
    "apps.hospitals",   # Layer 5 - rule-based emergency + hospital engine
    "apps.dispatch",    # Layer 3 & 6 - signal preemption, siren policy, trips
    "apps.alerts",      # Layer 4 - driver alert system
    "apps.analytics",   # dashboards 4.8 / 4.9
    "apps.notify",      # push delivery, notification history, preferences
    "apps.dashboards",  # server-rendered operator UIs
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sevps.urls"
WSGI_APPLICATION = "sevps.wsgi.application"
ASGI_APPLICATION = "sevps.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.sevps_settings",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database
#   sqlite   : default, no external service required
#   postgres : production; optionally with PostGIS for native spatial indexes
# ---------------------------------------------------------------------------
DB_ENGINE = env("SEVPS_DB_ENGINE", "sqlite").strip().lower()
ENABLE_POSTGIS = env_bool("SEVPS_ENABLE_POSTGIS", False)

def postgres_config() -> dict:
    """Connection settings for the PostgreSQL/PostGIS target.

    The plain ``postgresql`` backend is used even when PostGIS is enabled.
    SEVPS reaches PostGIS through generated ``geography`` columns and raw
    ``ST_DWithin``/KNN queries (see ``apps/core/spatial.py``) rather than
    GeoDjango model fields, so the GIS backend - and therefore a GDAL install
    on every machine - is not required. Set ``SEVPS_USE_GEODJANGO=1`` to opt
    into the GIS backend if you want GeoDjango's query expressions and have
    GDAL available.
    """
    use_geodjango = env_bool("SEVPS_USE_GEODJANGO", False)
    return {
        "ENGINE": (
            "django.contrib.gis.db.backends.postgis"
            if use_geodjango
            else "django.db.backends.postgresql"
        ),
        "NAME": env("SEVPS_DB_NAME", "sevps"),
        "USER": env("SEVPS_DB_USER", "sevps"),
        "PASSWORD": env("SEVPS_DB_PASSWORD", "sevps"),
        "HOST": env("SEVPS_DB_HOST", "127.0.0.1"),
        "PORT": env("SEVPS_DB_PORT", "5432"),
        "CONN_MAX_AGE": env_int("SEVPS_DB_CONN_MAX_AGE", 60),
        "OPTIONS": {"connect_timeout": 10},
    }


# Overridable so the Playwright suite can run against its own file rather than
# whatever a developer has seeded by hand. An E2E run that mutates the working
# database is one that either destroys real setup or passes because of it.
SQLITE_PATH = env("SEVPS_SQLITE_PATH", "") or str(BASE_DIR / "sevps.sqlite3")

SQLITE_CONFIG = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": SQLITE_PATH,
    "OPTIONS": {"timeout": 20},
    # Django defaults the SQLite test database to shared-cache memory, which
    # cannot run in WAL mode. Concurrent writers there fail with "database
    # table is locked" no matter how the application is configured, so the
    # multi-writer behaviour under test would be an artefact of the harness
    # rather than of SEVPS. A file-based test database gets the same WAL and
    # busy-timeout pragmas as production (see apps/core/apps.py).
    "TEST": {"NAME": BASE_DIR / "test_sevps.sqlite3"},
}

if DB_ENGINE in {"postgres", "postgresql"}:
    DATABASES = {"default": postgres_config()}
    # The SQLite file stays reachable as a named alias so a migration can read
    # the old database and write the new one in a single process.
    if Path(SQLITE_PATH).exists():
        DATABASES["sqlite"] = SQLITE_CONFIG
    if env_bool("SEVPS_USE_GEODJANGO", False):
        INSTALLED_APPS.insert(1, "django.contrib.gis")
else:
    DATABASES = {"default": SQLITE_CONFIG}
    # `migrate_to_postgres` needs a target alias while the default is still
    # SQLite. Only defined when a host is configured, so a plain pilot install
    # is not asked for Postgres credentials it does not have.
    if env("SEVPS_DB_HOST", ""):
        DATABASES["postgres"] = postgres_config()

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("SEVPS_TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Vite emits hashed assets into frontend/dist/assets and index.html references
# them as /assets/... . Adding dist as a static root lets collectstatic and the
# dev static handler serve them without a second web server.
if (BASE_DIR / "frontend" / "dist" / "assets").exists():
    STATICFILES_DIRS.append(BASE_DIR / "frontend" / "dist")
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Session sign-in for the server-rendered screens. Not /admin/login/, because
# the paramedic and hospital roles are deliberately non-staff and the admin
# login rejects them. The React console does not use this - it authenticates
# with JWT against /api/v1/auth/jwt/create/.
LOGIN_URL = "/legacy/login/"
LOGIN_REDIRECT_URL = "/legacy/"
LOGOUT_REDIRECT_URL = "/legacy/"

# ---------------------------------------------------------------------------
# Real-time layer (WebSockets)
# ---------------------------------------------------------------------------
REDIS_URL = env("SEVPS_REDIS_URL", "").strip()
if REDIS_URL:
    try:
        import channels_redis  # noqa: F401
    except ImportError as exc:
        # Falling back silently would be worse than failing: the deployment
        # would look configured for multi-process fan-out while every event
        # raised by the worker vanished before reaching a dashboard.
        raise ImproperlyConfigured(
            "SEVPS_REDIS_URL is set but channels-redis is not installed.\n"
            "    pip install channels-redis==4.2.0\n"
            "Unset SEVPS_REDIS_URL to run single-process with the in-memory layer."
        ) from exc

    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [REDIS_URL],
                # A dashboard that falls behind must not stall the publisher;
                # dropping frames is recoverable because clients detect the
                # sequence gap and resync.
                "capacity": env_int("SEVPS_CHANNEL_CAPACITY", 500),
                "expiry": env_int("SEVPS_CHANNEL_EXPIRY", 10),
            },
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# ---------------------------------------------------------------------------
# REST framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    # JWT first (the target scheme), then the legacy DRF token, then session.
    # All three are accepted during the migration so existing field devices
    # and the server-rendered dashboards keep working unchanged.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Deliberately the *strictest* sane default: an endpoint that forgets to
    # declare a policy requires authentication rather than leaking. The policy
    # audit in apps/core/api_policy.py then fails the build for any endpoint
    # that relies on this default instead of declaring its own.
    "DEFAULT_PERMISSION_CLASSES": ["apps.core.permissions.IsAuthenticatedRole"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# ---------------------------------------------------------------------------
# JWT (SimpleJWT)
#   Short access tokens because they are also passed on the WebSocket query
#   string, where they end up in access logs; long refresh tokens because the
#   refresh lives in an httpOnly cookie and is rotated + blacklisted on use.
# ---------------------------------------------------------------------------
from datetime import timedelta  # noqa: E402

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env_int("SEVPS_JWT_ACCESS_MINUTES", 15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env_int("SEVPS_JWT_REFRESH_DAYS", 7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("SEVPS_JWT_SIGNING_KEY", "") or SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_OBTAIN_SERIALIZER": "apps.core.jwt.SEVPSTokenObtainPairSerializer",
}

CORS_ALLOW_ALL_ORIGINS = DEBUG
# The React dev server needs credentialed cross-origin requests for the
# httpOnly refresh cookie to be set and sent (Phase 4).
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in env("SEVPS_CORS_ORIGINS", "").split(",") if o.strip()
]

# ---------------------------------------------------------------------------
# SEVPS domain configuration
# ---------------------------------------------------------------------------
SEVPS = {
    # Layer 2 / 3 - green corridor
    "GREEN_CORRIDOR_LOOKAHEAD_S": env_int("SEVPS_GREEN_CORRIDOR_LOOKAHEAD_S", 120),
    "SIGNAL_MAX_HOLD_S": env_int("SEVPS_SIGNAL_MAX_HOLD_S", 90),
    "SIGNAL_CLEARANCE_S": env_int("SEVPS_SIGNAL_CLEARANCE_S", 8),
    # Layer 4 - driver alerts
    "DRIVER_ALERT_RADIUS_M": env_int("SEVPS_DRIVER_ALERT_RADIUS_M", 800),
    "DRIVER_ALERT_MAX_ETA_S": env_int("SEVPS_DRIVER_ALERT_MAX_ETA_S", 90),
    # Layer 2 - routing
    "ROUTE_ALGORITHM": env("SEVPS_ROUTE_ALGORITHM", "astar"),  # astar | dijkstra
    "REROUTE_MIN_GAIN_S": env_int("SEVPS_REROUTE_MIN_GAIN_S", 45),
    "REROUTE_MIN_INTERVAL_S": env_int("SEVPS_REROUTE_MIN_INTERVAL_S", 30),
    # Congestion-triggered replanning. 0.55 is the moderate/heavy boundary in
    # CongestionLevel.from_ratio (index = 1 - speed ratio, and HEAVY starts at
    # ratio 0.45), so "heavy congestion" means one thing platform-wide: the
    # word in the popup, the colour on the map and the reroute trigger all
    # agree. Two segments rather than one, because a single slow link is often
    # just a signal queue that clears before the vehicle reaches it.
    "REROUTE_CONGESTION_THRESHOLD": env_float("SEVPS_REROUTE_CONGESTION_THRESHOLD", 0.55),
    "REROUTE_CONGESTION_MIN_SEGMENTS": env_int("SEVPS_REROUTE_CONGESTION_MIN_SEGMENTS", 2),
    "REROUTE_CONGESTED_MIN_GAIN_S": env_int("SEVPS_REROUTE_CONGESTED_MIN_GAIN_S", 10),
    "CONGESTION_MODEL_PATH": env("SEVPS_CONGESTION_MODEL_PATH", ""),
    # Layer 5 - hospital recommendation weights (must be interpretable & tunable)
    "HOSPITAL_WEIGHTS": {
        "capability": 0.34,
        "travel_time": 0.30,
        "bed_availability": 0.18,
        "workload": 0.12,
        "quality": 0.06,
    },
    "HOSPITAL_SEARCH_RADIUS_KM": env_int("SEVPS_HOSPITAL_SEARCH_RADIUS_KM", 25),
    "HOSPITAL_MAX_CANDIDATES": env_int("SEVPS_HOSPITAL_MAX_CANDIDATES", 8),
    # Mapping providers
    "TRAFFIC_PROVIDER": env("SEVPS_TRAFFIC_PROVIDER", "internal"),
    "MAPBOX_TOKEN": env("SEVPS_MAPBOX_TOKEN", ""),
    "GOOGLE_MAPS_KEY": env("SEVPS_GOOGLE_MAPS_KEY", ""),
    "OSRM_URL": env("SEVPS_OSRM_URL", ""),
    # Computer vision
    "CV_MODE": env("SEVPS_CV_MODE", "simulated"),  # simulated | yolo
    "CV_MODEL": env("SEVPS_CV_MODEL", "yolov8n.pt"),
    "CV_CONFIDENCE": float(env("SEVPS_CV_CONFIDENCE", "0.35")),
    # Machine learning (Phase 6). Models are optional: every estimator has a
    # statistical baseline, and predictions report which produced them.
    "MODEL_DIR": env("SEVPS_MODEL_DIR", "") or str(BASE_DIR / "models"),
    # Notifications (Phase 9). Web Push is the primary transport and needs
    # only a locally generated VAPID keypair - no vendor account. FCM is an
    # optional adapter for native Android clients; see docs/NOTIFICATIONS.md.
    "VAPID_PRIVATE_KEY": env("SEVPS_VAPID_PRIVATE_KEY", ""),
    "VAPID_PUBLIC_KEY": env("SEVPS_VAPID_PUBLIC_KEY", ""),
    "VAPID_KEY_PATH": env("SEVPS_VAPID_KEY_PATH", ""),
    # RFC 8292 requires a contactable URI so a push service operator can reach
    # the sender. Defaulted rather than left empty so a pilot install works
    # after `generate_vapid_keys` alone.
    "VAPID_SUBJECT": env("SEVPS_VAPID_SUBJECT", "mailto:ops@sevps.local"),
    "FCM_CREDENTIALS": env("SEVPS_FCM_CREDENTIALS", ""),
    "PUSH_DRIVER_ALERTS": env_bool("SEVPS_PUSH_DRIVER_ALERTS", True),
    # Spatial backend
    "POSTGIS_ENABLED": ENABLE_POSTGIS,
    "USE_GEODJANGO": env_bool("SEVPS_USE_GEODJANGO", False),
    # Simulation
    "SIM_TICK_SECONDS": env_int("SEVPS_SIM_TICK_SECONDS", 2),
    # Map defaults (Chennai)
    "MAP_CENTER": [
        float(env("SEVPS_MAP_LAT", "13.0604")),
        float(env("SEVPS_MAP_LON", "80.2496")),
    ],
    "MAP_ZOOM": env_int("SEVPS_MAP_ZOOM", 13),
}

# ---------------------------------------------------------------------------
# Production hardening
#   Applied automatically whenever DEBUG is off, so a deployment cannot ship
#   without them by omission.  Set SEVPS_BEHIND_TLS_PROXY=0 if TLS terminates
#   on this process rather than at a load balancer.
# ---------------------------------------------------------------------------
if not DEBUG:
    BEHIND_PROXY = env_bool("SEVPS_BEHIND_TLS_PROXY", True)

    SECURE_SSL_REDIRECT = env_bool("SEVPS_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"

    # HSTS defaults to one year; override to 0 during a staged TLS rollout,
    # since an over-eager HSTS header is painful to walk back.
    SECURE_HSTS_SECONDS = env_int("SEVPS_HSTS_SECONDS", 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SEVPS_HSTS_SUBDOMAINS", True)
    # Preload stays OFF by default, and `check --deploy` warns about that on
    # purpose. Submitting a domain to the browser preload list is close to
    # irreversible: removal takes months to propagate through browser
    # releases, and until it does, every subdomain is unreachable over plain
    # HTTP. For a municipal deployment that may still have a roadside display
    # controller or a legacy signal bridge on HTTP, that is an outage of the
    # thing this platform exists to run. Opt in once the estate is known to be
    # fully TLS.
    SECURE_HSTS_PRELOAD = env_bool("SEVPS_HSTS_PRELOAD", False)

    if BEHIND_PROXY:
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
        USE_X_FORWARDED_HOST = True

    if SECRET_KEY == "dev-insecure-key-do-not-use-in-production":
        raise ImproperlyConfigured(
            "SEVPS_SECRET_KEY must be set to a strong random value when DEBUG is off."
        )
    if "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            "SEVPS_ALLOWED_HOSTS must list explicit hostnames when DEBUG is off."
        )

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "sevps": {"format": "[{asctime}] {levelname:<7} {name}: {message}", "style": "{"}
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "sevps"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "daphne": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "sevps": {"level": env("SEVPS_LOG_LEVEL", "INFO"), "handlers": ["console"], "propagate": False},
    },
}
