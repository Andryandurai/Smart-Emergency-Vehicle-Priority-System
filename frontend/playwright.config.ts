/**
 * Playwright configuration (Phase 12).
 *
 * These tests drive the real console against a real Django server. That is the
 * point: Vitest covers pure logic and the Django suite covers the API, but
 * neither can catch the failures that only exist where they meet — a socket
 * that never upgrades, a role guard that redirects a signed-in controller to
 * the login page, a chart that renders an empty axis frame because the series
 * key changed name.
 *
 * The stack is started by Playwright rather than assumed to be running, so
 * `npx playwright test` is a single command with no setup ritual and no
 * "works on my machine because I had the server up".
 */
import { fileURLToPath } from "node:url";
import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

const DJANGO_PORT = Number(process.env.SEVPS_E2E_API_PORT ?? 8020);
const VITE_PORT = Number(process.env.SEVPS_E2E_WEB_PORT ?? 5273);
// package.json declares "type": "module", so __dirname does not exist here.
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export default defineConfig({
  testDir: "./e2e",
  // Generous: the first test after a cold start waits for Django to boot and
  // for Vite to compile the app. Tight timeouts here produce flakes that look
  // like product bugs.
  timeout: 45_000,
  expect: { timeout: 10_000 },

  // A test that only passes on retry is a flaky test, and a flaky end-to-end
  // suite gets ignored, which is worse than not having one. Retries are
  // allowed in CI only, where infrastructure noise is real.
  retries: process.env.CI ? 2 : 0,
  // Serial locally. These share one Django database; parallel workers would
  // have one test's trip appear in another's dashboard assertions.
  workers: 1,
  forbidOnly: !!process.env.CI,

  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: `http://127.0.0.1:${VITE_PORT}`,
    // Kept only for failures: a passing suite that writes a trace per test
    // fills a CI artifact store for no benefit.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    // The paramedic and driver screens ask for position. Granting it up front
    // avoids an OS-level permission dialog Playwright cannot dismiss.
    permissions: ["geolocation"],
    geolocation: { latitude: 13.0604, longitude: 80.2496 },
    locale: "en-IN",
    timezoneId: "Asia/Kolkata",
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Control-room screens are wide; a 1280×720 default hides the layer
        // control and the sidebar behind responsive breakpoints.
        viewport: { width: 1600, height: 900 },
      },
    },
  ],

  webServer: [
    {
      // A dedicated port and a dedicated database file, so running the E2E
      // suite never touches the development database an operator may have
      // seeded by hand.
      command: `python ../manage.py runserver 127.0.0.1:${DJANGO_PORT} --noreload`,
      url: `http://127.0.0.1:${DJANGO_PORT}/api/v1/health/live/`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        SEVPS_LOG_LEVEL: "WARNING",
        SEVPS_DEBUG: "1",
        SEVPS_ALLOWED_HOSTS: "localhost,127.0.0.1",
        // Its own database file. An E2E run that mutates the working database
        // either destroys a developer's setup or passes because of it.
        // Absolute: the server's cwd is frontend/, not the repo root, so a
        // relative path would quietly create a second, unseeded database.
        SEVPS_SQLITE_PATH: path.join(REPO_ROOT, "e2e.sqlite3"),
      },
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      // `--host 127.0.0.1` matters on Windows: Vite otherwise binds to
      // `localhost`, which resolves to ::1, while Playwright probes 127.0.0.1
      // and reports the server as never having started.
      command: `npx vite --port ${VITE_PORT} --strictPort --host 127.0.0.1`,
      url: `http://127.0.0.1:${VITE_PORT}/login`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        // Point the dev proxy at the E2E Django, not the developer's own.
        SEVPS_API_TARGET: `http://127.0.0.1:${DJANGO_PORT}`,
      },
    },
  ],
});
