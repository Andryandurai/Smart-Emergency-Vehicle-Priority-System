/**
 * The analytics screen (Phase 10) and the notification centre (Phase 9).
 *
 * Charts are the thing most worth testing in a browser: an SVG chart with a
 * wrong series key renders an axis frame with no line in it, which looks like
 * "no data yet" rather than "broken". Nothing below the browser can tell those
 * apart.
 */
import { expect, signIn, test } from "./fixtures";

test.describe("analytics", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, "police");
    await page.goto("/analytics");
    // The route is lazy-loaded so Recharts stays out of the main bundle; wait
    // for the chunk rather than racing it.
    await expect(page.getByRole("heading", { name: /AI Traffic Analytics/i })).toBeVisible();
  });

  test("charts actually draw, not just their axes", async ({ page }) => {
    // Recharts renders SVG, so "a chart appeared" is checkable properly:
    // a surface with no paths is an empty frame pretending to be a chart.
    const svg = page.locator(".recharts-surface").first();
    await expect(svg).toBeVisible({ timeout: 20_000 });

    const paths = await page.locator(".recharts-surface path").count();
    expect(paths, "charts rendered no geometry").toBeGreaterThan(0);
  });

  test("the KPI tiles show a direction of travel", async ({ page }) => {
    const tiles = page.locator(".trend-tile");
    if ((await tiles.count()) === 0) test.skip(true, "not enough history to compare");
    await expect(tiles.first()).toBeVisible();
    await expect(tiles.first().locator(".trend-value")).not.toBeEmpty();
  });

  test("demand volume gets no good/bad verdict", async ({ page }) => {
    // A city having a busy week is not the platform performing worse; a red
    // arrow there says it is. Phase 10 fixed this and it must stay fixed.
    const trips = page.locator(".trend-tile").filter({ hasText: /Emergency trips/i });
    if ((await trips.count()) === 0) test.skip(true, "tile not rendered in this window");
    const className = (await trips.first().getAttribute("class")) ?? "";
    expect(className).toContain("flat");
    expect(className).not.toContain("bad");
  });

  test("the series picker switches what is plotted", async ({ page }) => {
    const chips = page.locator(".series-chip");
    await expect(chips.first()).toBeVisible();

    const target = chips.filter({ hasText: /Driver alerts/i }).first();
    await target.click();
    await expect(target).toHaveClass(/on/);
  });

  test("changing the window refetches", async ({ page }) => {
    const response = page.waitForResponse(
      (r) => r.url().includes("/analytics/trends/") && r.url().includes("days=7"),
    );
    await page.getByLabel(/window/i).selectOption("7");
    expect((await response).ok()).toBe(true);
  });

  test("an export actually downloads, with credentials attached", async ({ page }) => {
    // This test found a real bug: the export cards were `<a href download>`,
    // and a browser navigation carries no Authorization header, so every
    // export returned 401 - saved to disk as a file named `daily.csv`
    // containing an error page. They are now fetched with the bearer token
    // and handed to the browser as a blob.
    const card = page.locator(".export-card").first();
    await expect(card).toBeVisible();
    expect(await card.getAttribute("data-url")).toMatch(
      /\/api\/v1\/analytics\/export\/.+\.csv/,
    );

    const download = page.waitForEvent("download", { timeout: 20_000 });
    await card.click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\.csv$/);

    // The file must carry its own window, or it is unattributable six months
    // later in a shared drive.
    const stream = await file.createReadStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream) chunks.push(chunk as Buffer);
    expect(Buffer.concat(chunks).toString("utf8")).toContain("# window:");
  });

  test("the accident hotspot map renders", async ({ page }) => {
    await expect(page.locator(".hotspot-map .leaflet-container")).toBeVisible();
  });
});

test.describe("notification centre", () => {
  test("the bell is present for a signed-in role and absent otherwise", async ({ page }) => {
    await page.goto("/driver");
    await expect(page.locator(".notif-bell")).toHaveCount(0);

    await signIn(page, "dispatcher");
    await expect(page.locator(".notif-bell")).toBeVisible();
  });

  test("the panel opens and reports push state honestly", async ({ page }) => {
    await signIn(page, "dispatcher");
    await page.locator(".notif-bell").click();
    await expect(page.locator(".notif-panel")).toBeVisible();

    // Whatever the state, it must be *stated*. "Notifications are off" with no
    // reason is not actionable; each state has a different remedy.
    await expect(page.locator(".notif-push")).toBeVisible();
    await expect(page.locator(".notif-push")).not.toBeEmpty();
  });

  test("the inbox loads without error", async ({ page }) => {
    await signIn(page, "dispatcher");
    const response = page.waitForResponse((r) => r.url().includes("/notify/inbox/"));
    await page.locator(".notif-bell").click();
    expect((await response).ok()).toBe(true);
    await expect(page.locator(".notif-error")).toHaveCount(0);
  });

  test("the service worker is served from the root with the right scope", async ({ page }) => {
    // Served from /static/ it would control only /static/ - the one part of
    // the site with no pages in it - and would never receive a push.
    await signIn(page, "dispatcher");
    const response = await page.request.get("/sw.js");
    expect(response.status()).toBe(200);
    expect(response.headers()["service-worker-allowed"]).toBe("/");
    expect(response.headers()["cache-control"]).toContain("no-cache");
  });
});

test.describe("settings", () => {
  test("the notification preferences section renders", async ({ page }) => {
    await signIn(page, "police");
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: /notifications/i })).toBeVisible();
  });

  test("critical alerts are stated to be unmutable", async ({ page }) => {
    // A checkbox that silently refuses to take effect is worse than no
    // checkbox, so the rule is written on the screen.
    await signIn(page, "police");
    await page.goto("/settings");
    await expect(page.locator(".pref-note")).toContainText(/critical/i);
  });
});
