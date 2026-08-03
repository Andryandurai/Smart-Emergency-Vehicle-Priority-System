# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: frontend\e2e\portals.spec.ts >> portal isolation >> admin -> logout -> driver does not show the admin console
- Location: frontend\e2e\portals.spec.ts:65:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/login", waiting until "load"

```

# Test source

```ts
  1   | /**
  2   |  * Portal isolation.
  3   |  *
  4   |  * The regression these cover was reported as "I log in as Admin, log out,
  5   |  * open the Driver portal, and I get the Admin page with the Admin UI" - and
  6   |  * its mirror image, a paramedic's screen surviving into the next session.
  7   |  *
  8   |  * Both had the same two causes, and both are asserted here rather than
  9   |  * described: the login screen obeyed `location.state.from`, which after a
  10  |  * sign-out is the *previous* user's path, and only the paramedic role had a
  11  |  * portal of its own to be redirected into.
  12  |  *
  13  |  * The assertions deliberately check for the *absence* of the other portals'
  14  |  * root elements. Landing on the right URL is not the claim being made; the
  15  |  * claim is that no part of another portal's chrome is on the page.
  16  |  */
  17  | import { expect, test } from "@playwright/test";
  18  | import type { Page } from "@playwright/test";
  19  | 
  20  | const ACCOUNTS = {
  21  |   admin: { username: "admin", password: "sevps-admin", root: "header.topbar", home: "/" },
  22  |   driver: { username: "driver", password: "sevps-driver", root: ".dp-root", home: "/d" },
  23  |   paramedic: { username: "paramedic", password: "sevps-paramedic", root: ".pm-root", home: "/p" },
  24  | } as const;
  25  | 
  26  | type Who = keyof typeof ACCOUNTS;
  27  | 
  28  | /** Every portal root. Exactly one may ever be present. */
  29  | const ROOTS = ["header.topbar", ".dp-root", ".pm-root"] as const;
  30  | 
  31  | async function signIn(page: Page, who: Who): Promise<void> {
  32  |   const account = ACCOUNTS[who];
> 33  |   await page.goto("/login");
      |              ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  34  |   await page.getByLabel(/username/i).fill(account.username);
  35  |   await page.getByLabel(/password/i).fill(account.password);
  36  |   await page.getByRole("button", { name: /^sign in$/i }).click();
  37  |   await expect(page.locator(account.root)).toBeVisible();
  38  | }
  39  | 
  40  | async function signOut(page: Page): Promise<void> {
  41  |   await page.getByRole("button", { name: /sign out/i }).click();
  42  |   await expect(page).toHaveURL(/\/login/);
  43  | }
  44  | 
  45  | /** Asserts that this portal's shell is on the page and no other portal's is. */
  46  | async function onlyPortalOf(page: Page, who: Who): Promise<void> {
  47  |   const mine = ACCOUNTS[who].root;
  48  |   await expect(page.locator(mine)).toBeVisible();
  49  |   for (const other of ROOTS) {
  50  |     if (other === mine) continue;
  51  |     await expect(page.locator(other)).toHaveCount(0);
  52  |   }
  53  | }
  54  | 
  55  | test.describe("portal isolation", () => {
  56  |   test("each role lands in its own portal and sees no other portal's shell", async ({ page }) => {
  57  |     for (const who of ["admin", "driver", "paramedic"] as const) {
  58  |       await signIn(page, who);
  59  |       await expect(page).toHaveURL(new RegExp(`${ACCOUNTS[who].home.replace("/", "\\/")}$`));
  60  |       await onlyPortalOf(page, who);
  61  |       await signOut(page);
  62  |     }
  63  |   });
  64  | 
  65  |   test("admin -> logout -> driver does not show the admin console", async ({ page }) => {
  66  |     await signIn(page, "admin");
  67  |     await expect(page.locator("header.topbar")).toBeVisible();
  68  |     await signOut(page);
  69  | 
  70  |     await signIn(page, "driver");
  71  |     // The reported fault, asserted directly.
  72  |     await expect(page).toHaveURL(/\/d$/);
  73  |     await onlyPortalOf(page, "driver");
  74  |     await expect(page.getByRole("link", { name: /^Operations$/ })).toHaveCount(0);
  75  |     await expect(page.getByRole("link", { name: /^Analytics$/ })).toHaveCount(0);
  76  |   });
  77  | 
  78  |   test("paramedic -> logout -> admin does not show the paramedic portal", async ({ page }) => {
  79  |     await signIn(page, "paramedic");
  80  |     await expect(page).toHaveURL(/\/p$/);
  81  |     await signOut(page);
  82  | 
  83  |     await signIn(page, "admin");
  84  |     await expect(page).toHaveURL(/\/$/);
  85  |     await onlyPortalOf(page, "admin");
  86  |   });
  87  | 
  88  |   test("paramedic -> logout -> driver does not show the paramedic portal", async ({ page }) => {
  89  |     await signIn(page, "paramedic");
  90  |     await signOut(page);
  91  | 
  92  |     await signIn(page, "driver");
  93  |     await expect(page).toHaveURL(/\/d$/);
  94  |     await onlyPortalOf(page, "driver");
  95  |   });
  96  | 
  97  |   test("a driver cannot navigate into the admin or paramedic portals", async ({ page }) => {
  98  |     await signIn(page, "driver");
  99  |     // Including the public screens, which render in the operations shell and
  100 |     // would otherwise hand a driver the control room's navigation.
  101 |     for (const path of ["/", "/fleet", "/analytics", "/settings", "/p", "/driver", "/boards"]) {
  102 |       await page.goto(path);
  103 |       await expect(page).toHaveURL(/\/d(\/|$)/);
  104 |       await onlyPortalOf(page, "driver");
  105 |     }
  106 |   });
  107 | 
  108 |   test("an administrator cannot land in the driver or paramedic portals", async ({ page }) => {
  109 |     await signIn(page, "admin");
  110 |     for (const path of ["/d", "/d/navigate", "/p", "/p/profile"]) {
  111 |       await page.goto(path);
  112 |       await expect(page).toHaveURL(/\/$/);
  113 |       await onlyPortalOf(page, "admin");
  114 |     }
  115 |   });
  116 | 
  117 |   test("the driver portal has exactly its four tabs", async ({ page }) => {
  118 |     await signIn(page, "driver");
  119 |     const rail = page.locator(".dp-rail .dp-tab");
  120 |     await expect(rail).toHaveCount(4);
  121 |     await expect(rail).toHaveText([/Take Over/, /Navigation/, /Hospitals/, /Profile/]);
  122 | 
  123 |     for (const [path, heading] of [
  124 |       ["/d/hospitals", /Hospitals/],
  125 |       ["/d/profile", /Suresh Kumar/],
  126 |     ] as const) {
  127 |       await page.goto(path);
  128 |       await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
  129 |       await onlyPortalOf(page, "driver");
  130 |     }
  131 |   });
  132 | });
  133 | 
```