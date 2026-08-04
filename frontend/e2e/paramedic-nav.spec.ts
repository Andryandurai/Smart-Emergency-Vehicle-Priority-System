/**
 * Paramedic navigation: layout and live movement.
 *
 * Both assertions need a live shift with a trip on it, so the spec builds one
 * through the API first. Driving the setup through the real endpoints rather
 * than fixtures keeps it honest: if the crew handshake or the assessment
 * changes shape, this fails too, which is the correct outcome for a screen
 * whose whole content comes from them.
 */
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

// Relative, so every call goes through the same Vite proxy the application
// uses. Addressing Django directly means this setup and the browser can end up
// talking to two different servers, which is exactly how it first failed.
async function token(request: APIRequestContext, username: string, password: string) {
  const response = await request.post(`/api/v1/auth/jwt/create/`, {
    data: { username, password },
  });
  return ((await response.json()) as { access: string }).access;
}

function auth(access: string) {
  return { Authorization: `Bearer ${access}` };
}

/** A live shift on an ambulance, with an assessed trip bound for a hospital. */
async function setUpJourney(request: APIRequestContext): Promise<void> {
  const driver = await token(request, "driver", "sevps-driver");
  const medic = await token(request, "paramedic", "sevps-paramedic");

  // Clear anything a previous run left open.
  for (const access of [driver, medic]) {
    const mine = await (
      await request.get(`/api/v1/fleet/shifts/mine/`, { headers: auth(access) })
    ).json();
    if (mine.shift) {
      await request.post(`/api/v1/fleet/shifts/${mine.shift.id}/end/`, {
        headers: auth(access),
        data: {},
      });
    }
  }

  const selectable = await (
    await request.get(`/api/v1/fleet/shifts/selectable-vehicles/`, {
      headers: auth(driver),
    })
  ).json();
  const callsign = selectable.vehicles[0].callsign;

  const shift = await (
    await request.post(`/api/v1/fleet/shifts/claim/`, {
      headers: auth(driver),
      data: { vehicle_callsign: callsign },
    })
  ).json();

  const catalogue = await (
    await request.get(`/api/v1/fleet/shifts/equipment-catalogue/`, {
      headers: auth(driver),
    })
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

  const hospitals = await (
    await request.get(`/api/v1/hospitals/hospitals/?limit=3`, { headers: auth(driver) })
  ).json();
  await request.post(`/api/v1/dispatch/trips/${trip.id}/assess/`, {
    headers: auth(medic),
    data: {
      emergency_category: "cardiac",
      hospital_id: hospitals.results[0].id,
      override_reason: "layout test",
      choice_reason: "clinical_judgement",
    },
  });
  await request.post(`/api/v1/dispatch/trips/${trip.id}/stage/`, {
    headers: auth(driver),
    data: { stage: "to_hospital" },
  });
}

async function signInAsParamedic(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill("paramedic");
  await page.getByLabel(/password/i).fill("sevps-paramedic");
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await expect(page.locator(".pm-root")).toBeVisible();
}

test.describe("paramedic navigation", () => {
  test.beforeEach(async ({ request }) => {
    await setUpJourney(request);
  });

  test("map, details and tab bar meet with no blank space between them", async ({ page }) => {
    await signInAsParamedic(page);
    await page.goto("/p/navigate");
    await expect(page.locator(".pm-nav-map")).toBeVisible();
    await expect(page.locator(".leaflet-container")).toBeVisible();

    const box = async (selector: string) => {
      const rect = await page.locator(selector).boundingBox();
      expect(rect, `${selector} has no box`).not.toBeNull();
      return rect!;
    };

    const root = await box(".pm-root");
    const map = await box(".pm-nav-map");
    const side = await box(".pm-nav-side");
    const tabs = await box(".pm-tabs");

    // One pixel of tolerance for sub-pixel rounding; anything more is a gap
    // somebody can see.
    expect(Math.abs(side.y - (map.y + map.height)), "gap between map and details").toBeLessThanOrEqual(1);
    expect(Math.abs(tabs.y - (side.y + side.height)), "gap between details and tab bar").toBeLessThanOrEqual(1);
    // And the three of them together must fill the shell, so there is no
    // leftover strip below the tab bar either.
    expect(Math.abs(root.y + root.height - (tabs.y + tabs.height)), "gap below the tab bar").toBeLessThanOrEqual(1);
  });

  /**
   * Movement is asserted through the speed readout, not the marker's pixel
   * position.
   *
   * The camera follows the ambulance, so a moving vehicle keeps roughly the
   * same place on screen while the map slides underneath it - the marker's
   * transform is very nearly the one thing that does *not* change when the
   * journey is working. The speed readout is fed from the same fix and is zero
   * for a stationary vehicle, which makes it the honest signal.
   */
  test("the ambulance is moving, not parked", async ({ page }) => {
    await signInAsParamedic(page);
    await page.goto("/p/navigate");
    await expect(page.locator(".leaflet-container")).toBeVisible();

    const speed = page.locator(".pm-nav-speed b");
    await expect(speed).toBeVisible();
    // Several journey ticks: the hook runs every 4s and the server ignores any
    // fix under 3s old, so the first tick or two can legitimately be refused.
    await expect
      .poll(async () => Number((await speed.textContent()) ?? "0"), {
        message: "the ambulance never reported a speed above zero",
        timeout: 30_000,
        intervals: [1000],
      })
      .toBeGreaterThan(0);
  });
});
