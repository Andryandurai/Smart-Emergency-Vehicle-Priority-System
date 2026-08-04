/**
 * Portal registry — which application a person belongs in, and which one a
 * URL belongs to.
 *
 * SEVPS is not one application with items hidden per role. It is three
 * separate front ends that happen to share a bundle:
 *
 *   ops        the control-room console  (`/`, `/fleet`, `/analytics`, …)
 *   driver     the in-cab driver portal  (`/d/…`)
 *   paramedic  the handheld clinical app (`/p/…`)
 *
 * Before this module existed there was no single answer to "where does this
 * user belong", and the two places that needed one disagreed:
 *
 *  - `RequireAuth` redirected paramedics to `/p` and nobody else anywhere, so
 *    a driver signing in landed on the operations console and got the control
 *    room's map, table and top navigation — the admin UI, for a driver.
 *  - `LoginPage` sent every user to `location.state.from`, which is whatever
 *    path the *previous* session was bounced off. Sign out of the paramedic
 *    portal at `/p` and the next person to sign in — an administrator — was
 *    navigated straight back to `/p` and shown the paramedic portal.
 *
 * Both are answered here, once. `portalForUser` reads the raw role list
 * rather than the auth store's `hasRole`, which returns true for every role
 * when the user is a superuser: correct for authorisation, catastrophic for
 * this question, because an administrator would resolve to the paramedic
 * portal.
 */
import type { CurrentUser } from "@/api/types";

export type PortalId = "ops" | "driver" | "paramedic" | "hospital" | "public";

/** Where each portal starts. A redirect target, and a tab bar's first item. */
export const PORTAL_HOME: Record<PortalId, string> = {
  ops: "/",
  driver: "/d",
  paramedic: "/p",
  hospital: "/h",
  public: "/driver",
};

/**
 * Portals whose users are inside the operations shell (`AppShell`).
 *
 * Only these may see the public screens, which render in that shell and
 * therefore carry its navigation. A driver sent to `/driver` — the road-user
 * alert receiver, a different screen that happens to share the word — would
 * be shown the control room's top bar, which is the collision this module
 * exists to prevent.
 */
const SHELL_PORTALS: ReadonlySet<PortalId> = new Set<PortalId>(["ops", "public"]);

/**
 * The portal this account lives in.
 *
 * Precedence matters and is not alphabetical: an account may hold several
 * roles, and the first match wins. Administrators are tested first so that a
 * superuser — who implicitly holds every role — resolves to the operations
 * console rather than to whichever crew role happens to be listed first.
 */
export function portalForUser(user: CurrentUser | null): PortalId {
  if (!user) return "public";
  const roles = user.roles ?? [];
  if (user.is_superuser || roles.includes("administrators")) return "ops";
  if (roles.includes("paramedic_crew")) return "paramedic";
  if (roles.includes("ambulance_drivers")) return "driver";
  // A receiving hospital has its own console now - the board, the inbound
  // ambulances and the ward's own figures. Before it existed, hospital staff
  // were sent to the operations shell and given a city map they had no use
  // for and mostly no permission to read.
  if (roles.includes("hospital_staff")) return "hospital";
  if (roles.includes("traffic_police") || roles.includes("dispatchers")) return "ops";
  return "public";
}

/**
 * The portal that owns a path.
 *
 * `/d` and `/driver` are deliberately distinguished by exact segment rather
 * than by prefix: they are two different screens for two different people —
 * the ambulance driver's portal and the public road user's alert receiver.
 */
export function portalForPath(pathname: string): PortalId {
  if (pathname === "/d" || pathname.startsWith("/d/")) return "driver";
  if (pathname === "/p" || pathname.startsWith("/p/")) return "paramedic";
  if (pathname === "/h" || pathname.startsWith("/h/")) return "hospital";
  if (pathname === "/driver" || pathname === "/boards") return "public";
  return "ops";
}

/** Where this user should be sent when they have nowhere particular to go. */
export function homeForUser(user: CurrentUser | null): string {
  return PORTAL_HOME[portalForUser(user)];
}

/**
 * May this user be on this path at all?
 *
 * The single rule the guard enforces: you are in your own portal, or on a
 * public screen that renders in a shell you already belong to.
 */
export function mayVisit(user: CurrentUser | null, pathname: string): boolean {
  const here = portalForPath(pathname);
  const mine = portalForUser(user);
  if (here === mine) return true;
  return here === "public" && SHELL_PORTALS.has(mine);
}

/**
 * Where to land after signing in.
 *
 * `intended` is honoured only when it belongs to the portal this account
 * lives in. That is what stops a path left behind by the previous session
 * from deciding which application the next person sees.
 */
export function landingFor(user: CurrentUser | null, intended?: string | null): string {
  if (intended && intended !== "/login" && mayVisit(user, intended)) return intended;
  return homeForUser(user);
}
