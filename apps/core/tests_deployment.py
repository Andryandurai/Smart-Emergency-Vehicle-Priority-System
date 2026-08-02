"""Phase 11 tests: health probes, and the deployment artifacts they support.

The deployment checks here are unusual for a Django test suite, and they earn
their place: a compose file is configuration nobody executes until a deploy,
and a mistake in it - a published database port, a service that migrates when
it should not, a WebSocket location missing the Upgrade header - is discovered
in production or not at all. Parsing them in CI is the only cheap way to find
those.

They check *structure and intent*, not that Docker works. Docker is not
required to run this suite.
"""
from __future__ import annotations

import pathlib
import re
import unittest

from django.conf import settings
from django.test import TestCase

ROOT = pathlib.Path(settings.BASE_DIR)

try:
    import yaml

    HAVE_YAML = True
except ImportError:  # pragma: no cover
    HAVE_YAML = False


def read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


class HealthProbeTests(TestCase):
    """Liveness and readiness answer different questions; conflating them is
    what makes an orchestrator restart healthy containers during an outage."""

    def test_liveness_is_public_and_cheap(self):
        response = self.client.get("/api/v1/health/live/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "alive")

    def test_liveness_touches_no_dependency(self):
        """Restarting the app cannot fix a database outage - so do not report
        one as a reason to restart."""
        with self.assertNumQueries(0):
            self.client.get("/api/v1/health/live/")

    def test_readiness_reports_the_dependencies(self):
        body = self.client.get("/api/v1/health/ready/").json()
        self.assertEqual(body["status"], "ready")
        self.assertIn("database", body["checks"])
        self.assertIn("channel_layer", body["checks"])

    def test_readiness_returns_503_when_a_dependency_is_down(self):
        from unittest.mock import patch

        with patch("apps.core.views.connection") as fake:
            fake.cursor.side_effect = RuntimeError("connection refused")
            response = self.client.get("/api/v1/health/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not-ready")

    def test_the_original_health_endpoint_still_works(self):
        """Existing load balancers and the console point at it."""
        response = self.client.get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")

    def test_probes_leak_no_configuration(self):
        """A public probe must not disclose hostnames, passwords or paths."""
        for url in ("/api/v1/health/live/", "/api/v1/health/ready/"):
            body = str(self.client.get(url).json())
            for secret in (settings.SECRET_KEY, str(settings.DATABASES["default"]["NAME"])):
                self.assertNotIn(secret, body)


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class ComposeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = yaml.safe_load(read("docker-compose.yml"))
        cls.override = yaml.safe_load(read("docker-compose.override.yml"))
        cls.prod = yaml.safe_load(read("docker-compose.prod.yml"))

    def test_the_base_file_publishes_no_host_ports(self):
        """Secure by default: forgetting an overlay must fail closed.

        Port publishing lives in the auto-loaded override, which Compose skips
        whenever files are named with -f. So a production deploy that omits an
        overlay comes up unreachable rather than coming up with an emergency
        service's database on a public interface.
        """
        published = [name for name, svc in self.base["services"].items() if "ports" in svc]
        self.assertEqual(published, [])

    def test_only_nginx_is_exposed_in_production(self):
        exposed = [name for name, svc in self.prod["services"].items() if svc.get("ports")]
        self.assertEqual(exposed, ["nginx"])

    def test_web_and_worker_wait_for_migrations_to_complete(self):
        """`service_started` would let the worker query a table that does not
        exist yet, then crash-loop until restart backoff hid the cause."""
        for service in ("web", "worker"):
            condition = self.base["services"][service]["depends_on"]["migrate"]["condition"]
            self.assertEqual(condition, "service_completed_successfully")

    def test_the_worker_is_pinned_to_one_replica(self):
        """Two workers would both decide a junction should be released and
        issue duplicate controller commands."""
        for compose in (self.base, self.prod):
            self.assertEqual(compose["services"]["worker"]["deploy"]["replicas"], 1)

    def test_production_requires_its_secrets_rather_than_defaulting_them(self):
        for service in ("web", "worker", "migrate"):
            env = self.prod["services"][service]["environment"]
            self.assertEqual(env["SEVPS_DEBUG"], "0")
            # `${VAR:?msg}` fails the deploy; `${VAR:-default}` boots on the
            # development key, which is how a pilot default signs real sessions.
            self.assertIn(":?", env["SEVPS_SECRET_KEY"])
            self.assertIn(":?", env["SEVPS_ALLOWED_HOSTS"])

    def test_production_requires_a_vapid_key(self):
        """Without one, push is silently undeliverable - the worst failure
        mode for an alerting channel."""
        env = self.prod["services"]["web"]["environment"]
        self.assertIn(":?", env["SEVPS_VAPID_PRIVATE_KEY"])

    def test_every_named_volume_is_declared(self):
        declared = set(self.base.get("volumes") or {})
        for compose in (self.base, self.prod):
            for service in compose["services"].values():
                for mount in service.get("volumes") or []:
                    head = mount.split(":")[0]
                    if not head.startswith((".", "/")):
                        self.assertIn(head, declared)

    def test_the_vapid_key_survives_container_replacement(self):
        """Regenerating it invalidates every push subscription in the fleet."""
        self.assertIn("vapid_keys", self.base["volumes"])
        for service in ("web", "worker", "migrate"):
            mounts = self.base["services"][service]["volumes"]
            self.assertTrue(any(m.startswith("vapid_keys:") for m in mounts))

    def test_interpolated_variables_are_real_settings(self):
        """A typo'd SEVPS_ var in compose is silently ignored by Django."""
        known = set(re.findall(r'env(?:_bool|_int)?\(\s*"([A-Z_]+)"', read("sevps", "settings.py")))
        compose_only = {
            "SEVPS_DB_PORT", "SEVPS_REDIS_PORT", "SEVPS_WEB_PORT",
            "SEVPS_HTTP_PORT", "SEVPS_WEB_REPLICAS",
        }
        used: set[str] = set()
        for name in ("docker-compose.yml", "docker-compose.override.yml",
                     "docker-compose.prod.yml"):
            used |= set(re.findall(r"\$\{(SEVPS_[A-Z_]+)", read(name)))
        self.assertEqual(used - known - compose_only, set())


class ImageTests(TestCase):
    def test_the_image_builds_the_console_itself(self):
        """A dist baked in from a laptop serves last week's UI against this
        week's API, with nothing visible from outside to say so."""
        dockerfile = read("Dockerfile")
        self.assertIn("FROM node:", dockerfile)
        self.assertIn("npm run build", dockerfile)
        self.assertIn("COPY --from=frontend", dockerfile)

    def test_no_build_toolchain_ships_in_the_runtime_layer(self):
        runtime = read("Dockerfile").split("AS runtime")[1]
        for tool in ("FROM node:", "build-essential", "npm "):
            self.assertNotIn(tool, runtime)

    def test_the_healthcheck_uses_liveness_not_readiness(self):
        line = next(
            l for l in read("Dockerfile").splitlines() if l.strip().startswith("CMD curl")
        )
        self.assertIn("/health/live/", line)
        self.assertNotIn("/health/ready/", line)

    def test_the_container_does_not_run_as_root(self):
        """This process issues traffic-signal commands."""
        self.assertIn("USER sevps", read("Dockerfile"))

    def test_the_build_context_excludes_the_database_and_node_modules(self):
        ignore = read(".dockerignore")
        for pattern in ("*.sqlite3", "node_modules", "frontend/dist"):
            self.assertIn(pattern, ignore)

    def test_only_one_role_runs_migrations(self):
        entrypoint = read("docker", "entrypoint.sh")
        web = entrypoint.split("web)")[1].split(";;")[0]
        worker = entrypoint.split("worker)")[1].split(";;")[0]
        self.assertIn("run_migrations", web)
        self.assertNotIn("run_migrations", worker)

    def test_migrations_are_serialised_by_an_advisory_lock(self):
        """Django has no internal lock; concurrent replicas can half-apply."""
        self.assertIn("pg_advisory_lock", read("docker", "entrypoint.sh"))


class NginxTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.conf = read("docker", "nginx", "nginx.conf")
        cls.headers = read("docker", "nginx", "conf.d", "proxy_headers.inc")

    def test_websocket_upgrade_headers_are_forwarded(self):
        """Without these the handshake gets a 200 instead of a 101, the client
        falls back to polling, and the console is silently seconds behind."""
        self.assertIn("map $http_upgrade $connection_upgrade", self.conf)
        self.assertIn("proxy_set_header Upgrade $http_upgrade", self.conf)
        self.assertIn("proxy_set_header Connection $connection_upgrade", self.conf)

    def test_websocket_timeout_exceeds_the_default(self):
        """A dashboard holds one socket open all shift; the 60s default cuts
        it every minute and the symptom is a console that quietly resyncs."""
        self.assertIn("proxy_read_timeout 3600s", self.conf)

    def test_forwarded_proto_is_set(self):
        """SECURE_PROXY_SSL_HEADER reads it; without it Django believes every
        request is plain HTTP and the secure cookie flags misbehave."""
        self.assertIn("X-Forwarded-Proto", self.headers)

    def test_the_websocket_route_matches_the_asgi_routing(self):
        from sevps import routing

        # `path()` routes expose the literal route string, not a regex.
        prefixes = {
            str(route.pattern).lstrip("^").split("/")[0]
            for route in routing.websocket_urlpatterns
        }
        self.assertEqual(prefixes, {"ws"}, "nginx proxies /ws/ - routes must live there")
        self.assertIn("location /ws/", self.conf)

    def test_the_service_worker_is_served_uncached_from_the_root(self):
        """A stale worker keeps handling pushes with old logic after a deploy."""
        self.assertIn("location = /sw.js", self.conf)
        self.assertIn('Service-Worker-Allowed "/"', self.conf)

    def test_authentication_is_rate_limited_more_tightly_than_the_api(self):
        auth = re.search(r"zone=auth:\S+\s+rate=(\d+)r/(\w)", self.conf)
        api = re.search(r"zone=api:\S+\s+rate=(\d+)r/(\w)", self.conf)
        self.assertIsNotNone(auth)
        self.assertIsNotNone(api)
        self.assertEqual(auth.group(2), "m")   # per minute
        self.assertEqual(api.group(2), "s")    # per second

    def test_health_probes_are_not_rate_limited(self):
        """A probe tripping the rate limiter takes a healthy instance out of
        the pool - the limiter causing the outage it was meant to prevent."""
        for probe in ("health/live", "health/ready"):
            block = self.conf.split(f"/api/v1/{probe}/ {{")[1].split("}")[0]
            self.assertNotIn("limit_req", block)

    def test_hashed_assets_are_immutable_but_the_shell_is_not_cached(self):
        """index.html is unhashed: caching it makes a browser request chunk
        names that no longer exist after a deploy."""
        static_block = self.conf.split("location /static/ {")[1].split("}")[0]
        self.assertIn("immutable", static_block)
        spa_block = self.conf.rsplit("location / {", 1)[1]
        self.assertIn("no-store", spa_block)
