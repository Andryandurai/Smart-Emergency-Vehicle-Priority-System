/**
 * Shared E2E helpers.
 *
 * Signing in goes through the real login form rather than by injecting a
 * token, because the login flow is itself one of the things most likely to
 * break: the access token lives in memory and the refresh token is an httpOnly
 * cookie, so a change to either silently breaks session restore on reload.
 * A test that injects a token would keep passing while real users could not
 * sign in.
 */
import { expect, test as base, type Page } from "@playwright/test";

/** Accounts created by `manage.py seed_users`. */
export const ACCOUNTS = {
  admin: { username: "admin", password: "sevps-admin" },
  police: { username: "police", password: "sevps-police" },
  dispatcher: { username: "dispatcher", password: "sevps-dispatcher" },
  paramedic: { username: "paramedic", password: "sevps-paramedic" },
  hospital: { username: "hospital", password: "sevps-hospital" },
  public: { username: "public", password: "sevps-public" },
} as const;

export type AccountName = keyof typeof ACCOUNTS;

export async function signIn(page: Page, account: AccountName): Promise<void> {
  const { username, password } = ACCOUNTS[account];
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(username);
  await page.getByLabel(/password/i).fill(password);
  await page.getByRole("button", { name: /sign in/i }).click();

  // Wait for *a* portal shell, not for a URL and not for the operations one:
  // the post-login destination differs by role, and asserting on the topbar
  // would fail for every role that has a portal of its own - which, since the
  // hospital portal landed, is most of them.
  await expect(
    page.locator("header.topbar, .dp-root, .pm-root, .hp-root").first(),
  ).toBeVisible();
}

export async function signOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page).toHaveURL(/\/login/);
}

/**
 * Fail a test on any uncaught page error.
 *
 * Without this, a React component that throws inside an effect renders an
 * empty region and the assertion below it fails with "element not found",
 * which sends whoever is debugging to the selector rather than to the stack
 * trace that actually explains it.
 */
export const test = base.extend<{ pageErrors: string[] }>({
  pageErrors: async ({ page }, use) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") {
        const text = message.text();
        // Tile 404s and aborted fetches during navigation are noise, not bugs.
        if (!/favicon|tile|net::ERR_ABORTED|Failed to load resource/i.test(text)) {
          errors.push(text);
        }
      }
    });
    await use(errors);
    expect(errors, `console errors: ${errors.join(" | ")}`).toEqual([]);
  },
});

export { expect };
