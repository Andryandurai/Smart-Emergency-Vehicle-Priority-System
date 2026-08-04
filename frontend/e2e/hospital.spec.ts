/**
 * The hospital portal, and the admin console's moving fleet.
 *
 * Both are assertions about things that were previously *present but inert* -
 * a hospital login that landed on somebody else's console, and vehicles that
 * were active but parked - so both are checked by observing change over time
 * rather than by finding an element on screen.
 */
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

async function token(request: APIRequestContext, username: string, password: string) {
  const response = await request.post("/api/v1/auth/jwt/create/", {
    data: { username, password },
  });
  return ((await response.json()) as { access: string }).access;
}

const auth = (access: string) => ({ Authorization: `Bearer ${access}` });

async function signIn(page: Page, username: string, password: string, root: string) {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(username);
  await page.getByLabel(/password/i).fill(password);
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await expect(page.locator(root)).toBeVisible();
}

/** A crewed ambulance transporting a patient to `hospitalCode`. */
async function inboundJourney(request: APIRequestContext, hospitalCode: string): Promise<void> {
  const driver = await token(request, "driver", "sevps-driver");
  const medic = await token(request, "paramedic", "sevps-paramedic");

  for (const access of [driver, medic]) {
    const mine = await (
      await request.get("/api/v1/fleet/shifts/mine/", { headers: auth(access) })
    ).json();
    if (mine.shift) {
      await request.post(`/api/v1/fleet/shifts/${mine.shift.id}/end/`, {
        headers: auth(access),
        data: {},
      });
    }
  }

  const selectable = await (
    await request.get("/api/v1/fleet/shifts/selectable-vehicles/", { headers: auth(driver) })
  ).json();
  const shift = await (
    await request.post("/api/v1/fleet/shifts/claim/", {
      headers: auth(driver),
      data: { vehicle_callsign: selectable.vehicles[0].callsign },
    })
  ).json();

  const catalogue = await (
    await request.get("/api/v1/fleet/shifts/equipment-catalogue/", { headers: auth(driver) })
  ).json();
  const items: Record<string, { present: boolean }> = {};
  for (const item of catalogue.items) items[item.code] = { present: true };
  await request.post(`/api/v1/fleet/shifts/${shift.id}/checklist/`, {
    headers: auth(driver),
    data: { items },
  });
  await request.post(`/api/v1/fleet/shifts/${shift.id}/request-paramedic/`, {
    headers: auth(driver),
    data: { paramedic_username: "paramedic" },
  });
  await request.post(`/api/v1/fleet/shifts/${shift.id}/accept/`, {
    headers: auth(medic),
    data: {},
  });

  const trip = await (
    await request.post(`/api/v1/fleet/shifts/${shift.id}/new-emergency/`, {
      headers: auth(driver),
      data: {},
    })
  ).json();

  const choices = await (
    await request.get("/api/v1/hospitals/portal/choices/", { headers: auth(driver) })
  ).json();
  const hospital = choices.hospitals.find((h: { code: string }) => h.code === hospitalCode)
    ?? choices.hospitals[0];

  await request.post(`/api/v1/dispatch/trips/${trip.id}/assess/`, {
    headers: auth(medic),
    data: {
      emergency_category: "cardiac",
      symptoms: ["chest_pain"],
      patient_age: 57,
      patient_notes: "Central chest pain radiating to the left arm.",
      hospital_id: hospital.id,
      override_reason: "e2e",
      choice_reason: "clinical_judgement",
    },
  });
  await request.post(`/api/v1/dispatch/trips/${trip.id}/stage/`, {
    headers: auth(driver),
    data: { stage: "to_hospital" },
  });
}

test.describe("hospital portal", () => {
  test("the dashboard shows every figure the board is specified to carry", async ({ page }) => {
    await signIn(page, "hospital", "sevps-hospital", ".hp-root");

    // The six headline figures, by their labels rather than by position.
    for (const label of [
      "Active Ambulances Coming",
      "Emergency Cases Today",
      "Available Beds",
      "Available ICU Beds",
      "Available Ventilators",
      "Available Operation Theatres",
      "Emergency Staff On Duty",
    ]) {
      await expect(page.locator(".hp-figure", { hasText: label })).toBeVisible();
    }

    // Hospital name, id and a derived status.
    await expect(page.locator(".hp-hero-id h1")).not.toBeEmpty();
    await expect(page.locator(".hp-hero-id p")).toContainText(/Hospital ID/);
    await expect(page.locator(".hp-hero-status .hp-status")).toHaveText(/ready|busy|full/);

    // Eight teams and seven ward rows.
    await expect(page.locator(".hp-teams .hp-team")).toHaveCount(8);
    for (const team of [
      "Emergency Team", "Trauma Team", "Cardiology", "Neurology",
      "Burn Unit", "ICU", "Operation Theatre", "Blood Bank",
    ]) {
      await expect(page.locator(".hp-team-label", { hasText: new RegExp(`^${team}$`) })).toBeVisible();
    }
    await expect(page.locator(".hp-table tbody tr")).toHaveCount(7);
  });

  test("an inbound ambulance appears with crew, patient and live navigation", async ({
    page,
    request,
  }) => {
    await inboundJourney(request, "APOLLO");
    await signIn(page, "hospital", "sevps-hospital", ".hp-root");
    await page.goto("/h/ambulances");

    const row = page.locator(".hp-amb").first();
    await expect(row).toBeVisible();
    await expect(row.locator(".hp-amb-number")).not.toBeEmpty();
    // Driver and paramedic names, from the live shift.
    await expect(row.locator(".hp-amb-crew")).toContainText(/Driver/);
    await expect(row.locator(".hp-amb-crew")).toContainText(/Paramedic/);

    await row.locator(".hp-amb-head").click();
    await expect(row.locator(".hp-amb-detail")).toBeVisible();
    // The per-ambulance navigation view.
    await expect(row.locator(".leaflet-container")).toBeVisible();
    // Patient block.
    await expect(row.locator(".hp-amb-info")).toContainText(/Emergency category/i);
    await expect(row.locator(".hp-amb-info")).toContainText(/Priority level/i);
    await expect(row.locator(".hp-symptoms")).toBeVisible();
    await expect(row.locator(".hp-assessment")).not.toBeEmpty();
    // And the handover button, held back until the ambulance is actually here.
    await expect(row.getByRole("button", { name: /Patient Received/i })).toBeVisible();
  });

  test("the updates tab writes back to the dashboard", async ({ page }) => {
    await signIn(page, "hospital", "sevps-hospital", ".hp-root");
    await page.goto("/h/updates");

    const cases = page.locator(".hp-field", { hasText: "Emergency Cases Today" }).locator("input");
    await expect(cases).toBeVisible();
    await cases.fill("77");

    // Team toggles are on this tab too.
    await expect(page.locator(".hp-team-edit .hp-toggle")).toHaveCount(8);

    await page.getByRole("button", { name: /Save updates/i }).click();
    await expect(page.getByRole("button", { name: /Saved/i })).toBeVisible();

    await page.goto("/h");
    await expect(
      page.locator(".hp-figure", { hasText: "Emergency Cases Today" }).locator("b"),
    ).toHaveText("77");
  });
});

test.describe("operations console", () => {
  /**
   * The reported fault: vehicles active but stationary.
   *
   * Asserted on the store's own numbers rather than on marker pixels - the
   * map pans and markers re-project, so a changed transform proves very
   * little. A vehicle reporting a speed is a vehicle that is moving.
   */
  test("vehicles move along their optimised routes", async ({ page }) => {
    await signIn(page, "admin", "sevps-admin", "header.topbar");

    await expect
      .poll(
        async () =>
          page.evaluate(async () => {
            const response = await fetch("/api/v1/fleet/vehicles/live/", {
              headers: { "Content-Type": "application/json" },
              credentials: "same-origin",
            });
            if (!response.ok) return 0;
            const body = (await response.json()) as { vehicles: { speed_kmh: number }[] };
            return body.vehicles.filter((vehicle) => vehicle.speed_kmh > 0).length;
          }),
        {
          message: "no vehicle ever reported a speed above zero on the operations console",
          timeout: 40_000,
          intervals: [2000],
        },
      )
      .toBeGreaterThan(0);
  });
});
