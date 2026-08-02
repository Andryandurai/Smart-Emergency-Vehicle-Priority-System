/**
 * Authentication and role guards.
 *
 * These are the tests that justify having an E2E layer at all. The token
 * strategy — access token in memory, refresh token in an httpOnly cookie — is
 * split across the API client, the auth store and Django, and no single-layer
 * test can prove it works end to end. The specific failure it guards against
 * is the worst kind: a controller reloads the page mid-incident and is bounced
 * to the login screen.
 */
import { ACCOUNTS, expect, signIn, signOut, test } from "./fixtures";

test.describe("sign-in", () => {
  test("an operator can sign in and reach the operations console", async ({ page }) => {
    await signIn(page, "police");
    // The ops console is a full-bleed map with no page heading by design -
    // screen space goes to the situation, not to a title. Assert on the
    // landmarks that actually exist.
    await expect(page.locator(".split.wide")).toBeVisible();
    await expect(page.locator("aside.sidebar")).toBeVisible();
    await expect(page.locator(".leaflet-container")).toBeVisible();
  });

  test("bad credentials are reported, not silently swallowed", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel(/username/i).fill("police");
    await page.getByLabel(/password/i).fill("wrong-password");
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page.locator(".badge.bad")).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("the session survives a hard reload", async ({ page }) => {
    // The single most important behaviour here. The access token is in memory
    // and dies with the page; only the httpOnly refresh cookie can restore the
    // session. If this breaks, every reload during an incident is a re-login.
    await signIn(page, "dispatcher");
    await page.reload();

    await expect(page.locator(".whoami .who")).toHaveText("dispatcher");
    await expect(page).not.toHaveURL(/\/login/);
  });

  test("the access token is never written to browser storage", async ({ page }) => {
    // A durable credential in localStorage is exfiltrable by any XSS. This
    // asserts the design decision from Phase 2 still holds.
    await signIn(page, "police");
    const stored = await page.evaluate(() => ({
      local: JSON.stringify(window.localStorage),
      session: JSON.stringify(window.sessionStorage),
    }));
    expect(stored.local).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}/);
    expect(stored.session).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}/);
  });

  test("the refresh cookie is httpOnly and scoped to the auth path", async ({ page, context }) => {
    await signIn(page, "police");
    const cookie = (await context.cookies()).find((c) => c.name === "sevps_refresh");
    expect(cookie, "refresh cookie should be set").toBeTruthy();
    expect(cookie!.httpOnly).toBe(true);
    // Scoped so it is not sent with every request to every endpoint.
    expect(cookie!.path).toContain("/api/v1/auth/");
  });

  test("signing out ends the session for good", async ({ page }) => {
    await signIn(page, "police");
    await signOut(page);
    // Going back must not resurrect it - the refresh token is blacklisted.
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("role guards", () => {
  test("an anonymous visitor is redirected away from operational screens", async ({ page }) => {
    for (const route of ["/", "/analytics", "/hospitals", "/paramedic"]) {
      await page.goto(route);
      await expect(page, `${route} should require a login`).toHaveURL(/\/login/);
    }
  });

  test("the public screens stay open without an account", async ({ page }) => {
    // Layer 4's promise: a road user's phone and a roadside sign hold no
    // credentials. If this ever requires a login, the feature is gone for the
    // people it exists for.
    await page.goto("/driver");
    await expect(page).toHaveURL(/\/driver/);
    await expect(page.locator(".leaflet-container")).toBeVisible();

    await page.goto("/boards");
    await expect(page).toHaveURL(/\/boards/);
  });

  test("the navigation only offers what the role can actually open", async ({ page }) => {
    await page.goto("/driver");
    // Wait for the nav to render before reading it: an empty array from a
    // still-mounting React tree passes `not.toContain` for the wrong reason.
    await expect(page.locator("nav.topnav a").first()).toBeVisible();
    const anonymousLinks = await page.locator("nav.topnav a").allTextContents();
    expect(anonymousLinks).toContain("Driver");
    expect(anonymousLinks).not.toContain("Operations");

    await signIn(page, "police");
    const signedInLinks = await page.locator("nav.topnav a").allTextContents();
    expect(signedInLinks).toContain("Operations");
    expect(signedInLinks).toContain("Analytics");
  });

  test("the role is shown, so an operator can see what they are signed in as", async ({ page }) => {
    await signIn(page, "hospital");
    await expect(page.locator(".whoami .role")).toContainText(/hospital/i);
  });

  test("a deep link is preserved through the login redirect", async ({ page }) => {
    // Being dropped on the dashboard after signing in, having asked for the
    // analytics screen, is a small thing that erodes trust in a tool.
    await page.goto("/analytics");
    await expect(page).toHaveURL(/\/login/);

    await page.getByLabel(/username/i).fill(ACCOUNTS.police.username);
    await page.getByLabel(/password/i).fill(ACCOUNTS.police.password);
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page).toHaveURL(/\/analytics/);
  });
});
