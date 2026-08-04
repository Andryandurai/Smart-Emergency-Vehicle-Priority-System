/**
 * Portal isolation.
 *
 * The regression these cover was reported as "I log in as Admin, log out,
 * open the Driver portal, and I get the Admin page with the Admin UI" - and
 * its mirror image, a paramedic's screen surviving into the next session.
 *
 * Both had the same two causes, and both are asserted here rather than
 * described: the login screen obeyed `location.state.from`, which after a
 * sign-out is the *previous* user's path, and only the paramedic role had a
 * portal of its own to be redirected into.
 *
 * The assertions deliberately check for the *absence* of the other portals'
 * root elements. Landing on the right URL is not the claim being made; the
 * claim is that no part of another portal's chrome is on the page.
 */
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

const ACCOUNTS = {
  admin: { username: "admin", password: "sevps-admin", root: "header.topbar", home: "/" },
  driver: { username: "driver", password: "sevps-driver", root: ".dp-root", home: "/d" },
  paramedic: { username: "paramedic", password: "sevps-paramedic", root: ".pm-root", home: "/p" },
  hospital: { username: "hospital", password: "sevps-hospital", root: ".hp-root", home: "/h" },
} as const;

type Who = keyof typeof ACCOUNTS;

/** Every portal root. Exactly one may ever be present. */
const ROOTS = ["header.topbar", ".dp-root", ".pm-root", ".hp-root"] as const;

async function signIn(page: Page, who: Who): Promise<void> {
  const account = ACCOUNTS[who];
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(account.username);
  await page.getByLabel(/password/i).fill(account.password);
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await expect(page.locator(account.root)).toBeVisible();
}

async function signOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page).toHaveURL(/\/login/);
}

/** Asserts that this portal's shell is on the page and no other portal's is. */
async function onlyPortalOf(page: Page, who: Who): Promise<void> {
  const mine = ACCOUNTS[who].root;
  await expect(page.locator(mine)).toBeVisible();
  for (const other of ROOTS) {
    if (other === mine) continue;
    await expect(page.locator(other)).toHaveCount(0);
  }
}

test.describe("portal isolation", () => {
  test("each role lands in its own portal and sees no other portal's shell", async ({ page }) => {
    for (const who of ["admin", "driver", "paramedic", "hospital"] as const) {
      await signIn(page, who);
      await expect(page).toHaveURL(new RegExp(`${ACCOUNTS[who].home.replace("/", "\\/")}$`));
      await onlyPortalOf(page, who);
      await signOut(page);
    }
  });

  test("admin -> logout -> driver does not show the admin console", async ({ page }) => {
    await signIn(page, "admin");
    await expect(page.locator("header.topbar")).toBeVisible();
    await signOut(page);

    await signIn(page, "driver");
    // The reported fault, asserted directly.
    await expect(page).toHaveURL(/\/d$/);
    await onlyPortalOf(page, "driver");
    await expect(page.getByRole("link", { name: /^Operations$/ })).toHaveCount(0);
    await expect(page.getByRole("link", { name: /^Analytics$/ })).toHaveCount(0);
  });

  test("paramedic -> logout -> admin does not show the paramedic portal", async ({ page }) => {
    await signIn(page, "paramedic");
    await expect(page).toHaveURL(/\/p$/);
    await signOut(page);

    await signIn(page, "admin");
    await expect(page).toHaveURL(/\/$/);
    await onlyPortalOf(page, "admin");
  });

  test("paramedic -> logout -> driver does not show the paramedic portal", async ({ page }) => {
    await signIn(page, "paramedic");
    await signOut(page);

    await signIn(page, "driver");
    await expect(page).toHaveURL(/\/d$/);
    await onlyPortalOf(page, "driver");
  });

  test("a driver cannot navigate into the admin or paramedic portals", async ({ page }) => {
    await signIn(page, "driver");
    // Including the public screens, which render in the operations shell and
    // would otherwise hand a driver the control room's navigation.
    for (const path of ["/", "/fleet", "/analytics", "/settings", "/p", "/driver", "/boards"]) {
      await page.goto(path);
      await expect(page).toHaveURL(/\/d(\/|$)/);
      await onlyPortalOf(page, "driver");
    }
  });

  test("an administrator cannot land in the crew or hospital portals", async ({ page }) => {
    await signIn(page, "admin");
    for (const path of ["/d", "/d/navigate", "/p", "/p/profile", "/h", "/h/ambulances"]) {
      await page.goto(path);
      await expect(page).toHaveURL(/\/$/);
      await onlyPortalOf(page, "admin");
    }
  });

  test("hospital staff get the hospital portal, not the operations console", async ({ page }) => {
    await signIn(page, "hospital");
    await expect(page).toHaveURL(/\/h$/);
    await onlyPortalOf(page, "hospital");

    // Including the screens they used to be handed: before this portal
    // existed, a hospital login landed on the control room's city map.
    for (const path of ["/", "/fleet", "/analytics", "/hospitals", "/d", "/p", "/boards"]) {
      await page.goto(path);
      await expect(page).toHaveURL(/\/h(\/|$)/);
      await onlyPortalOf(page, "hospital");
    }
  });

  test("the hospital portal has exactly its three tabs", async ({ page }) => {
    await signIn(page, "hospital");
    const tabs = page.locator(".hp-tabs .hp-tab");
    await expect(tabs).toHaveCount(3);
    await expect(tabs).toHaveText([/Dashboard/, /Ambulances/, /Updates/]);
  });

  test("the driver portal has exactly its four tabs", async ({ page }) => {
    await signIn(page, "driver");
    const rail = page.locator(".dp-rail .dp-tab");
    await expect(rail).toHaveCount(4);
    await expect(rail).toHaveText([/Take Over/, /Navigation/, /Hospitals/, /Profile/]);

    for (const [path, heading] of [
      ["/d/hospitals", /Hospitals/],
      ["/d/profile", /Suresh Kumar/],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
      await onlyPortalOf(page, "driver");
    }
  });
});
