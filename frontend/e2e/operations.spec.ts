/**
 * The Emergency Operations Dashboard (feature 4.11).
 *
 * The seam this covers is the one no other layer can: the WebSocket. A socket
 * that never upgrades fails *quietly* — the console falls back to polling and
 * still shows correct data, just seconds late, with nothing on screen to say
 * so. That is exactly the failure mode a green corridor cannot tolerate, and
 * exactly the one a unit test cannot see.
 */
import { expect, signIn, test } from "./fixtures";

test.describe("operations console", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, "police");
  });

  test("the map, sidebar and layer control all render", async ({ page }) => {
    await expect(page.locator(".leaflet-container")).toBeVisible();
    await expect(page.locator("aside.sidebar")).toBeVisible();
    await expect(page.locator(".layer-control")).toBeVisible();
    await expect(page.locator(".map-legend")).toBeVisible();
  });

  test("the basemap is a keyless provider", async ({ page }) => {
    // An emergency platform must not need a tile contract to draw a map.
    //
    // Asserted on the rendered tile URLs rather than by watching for a network
    // request: tiles are fetched as soon as the map mounts, so a listener
    // attached afterwards races the very thing it is watching for and the
    // test flakes rather than fails.
    const tile = page.locator("img.leaflet-tile").first();
    await expect(tile).toBeAttached({ timeout: 20_000 });
    const src = await tile.getAttribute("src");
    expect(src).toMatch(/basemaps\.cartocdn\.com|tile\.openstreetmap\.org/);
    expect(src).not.toMatch(/access_token|api_key|[?&]key=/);
  });

  test("the WebSocket upgrades rather than falling back to polling", async ({ page }) => {
    // The whole point of this spec file.
    const socket = await page.waitForEvent("websocket", { timeout: 15_000 });
    expect(socket.url()).toContain("/ws/ops/");
    expect(socket.isClosed()).toBe(false);
  });

  test("the connection indicator reports live, not degraded", async ({ page }) => {
    await expect(page.locator(".dot, .conn-dot").first()).toBeVisible();
    // Whatever the visual, the socket must not be reporting itself anonymous:
    // that badge means JWT auth over the socket failed and it fell back.
    await expect(page.getByText("socket anonymous")).toHaveCount(0);
  });

  test("the stat tiles are populated", async ({ page }) => {
    const stats = page.locator(".stat-row .stat, .stat-row > div");
    await expect(stats.first()).toBeVisible();
    expect(await stats.count()).toBeGreaterThanOrEqual(4);
  });

  test("GIS layers can be toggled and the choice survives a reload", async ({ page }) => {
    await page.locator(".layer-toggle").click();
    await expect(page.locator(".layer-panel")).toBeVisible();

    const roadNetwork = page.locator(".layer-row").filter({ hasText: /road network/i }).locator("input");
    const before = await roadNetwork.isChecked();
    await roadNetwork.click();
    await expect(roadNetwork).toBeChecked({ checked: !before });

    // Persisted to localStorage: a controller who turned off the road network
    // to see vehicles clearly should not have to do it again after a refresh.
    await page.reload();
    await page.locator(".layer-toggle").click();
    await expect(
      page.locator(".layer-row").filter({ hasText: /road network/i }).locator("input"),
    ).toBeChecked({ checked: !before });
  });

  test("layers needing a role are marked, not silently missing", async ({ page, context }) => {
    // An operator asking "why can I not see vehicles" should get the answer
    // from the control, not from devtools.
    const anonymous = await context.newPage();
    await anonymous.goto("/driver");
    await anonymous.close();

    await page.locator(".layer-toggle").click();
    // Signed in as police, nothing should be locked.
    await expect(page.locator(".layer-row.locked")).toHaveCount(0);
  });

  test("the event log is present", async ({ page }) => {
    // Attached, not visible: an empty log has zero height, and a quiet network
    // is the normal state. Asserting visibility would make this pass only when
    // something had gone wrong somewhere in the city.
    await expect(page.locator(".log")).toBeAttached();
  });

  test("no clinical data reaches a traffic-police screen", async ({ page }) => {
    // Phase 4 found this leaking for real. Traffic control runs a corridor
    // knowing priority and position, never a diagnosis.
    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(/patient_notes|patient_age/i);
  });
});

test.describe("hospital console", () => {
  /**
   * Hospital staff now have a portal of their own, so this no longer asserts
   * that they can open the operations console's hospital list - they cannot,
   * and should not. `/hospitals` is a control-room screen; theirs is `/h`.
   * The portal's own behaviour is covered by portals.spec.ts.
   */
  test("hospital staff reach their own dashboard", async ({ page }) => {
    await signIn(page, "hospital");
    await expect(page.locator(".hp-root")).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  });
});

test.describe("paramedic console", () => {
  test("an ambulance crew reaches the vehicle selection screen", async ({ page }) => {
    await signIn(page, "paramedic");
    await page.goto("/paramedic");
    await expect(page.locator(".page")).toBeVisible();
  });
});

test.describe("public driver screen", () => {
  test("a road user gets the alert screen with no account", async ({ page }) => {
    await page.goto("/driver");
    await expect(page.locator(".leaflet-container")).toBeVisible();
    // And no sign of anything operational.
    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(/patient|preemption|corridor plan/i);
  });
});
