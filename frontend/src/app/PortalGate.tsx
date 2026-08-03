/**
 * Portal isolation.
 *
 * Wraps the whole route table, above every shell, and answers one question on
 * every navigation: is this signed-in user allowed to be on this path? If
 * not, they go to their own portal's home instead.
 *
 * It sits above `RequireAuth` rather than inside it because the screens that
 * leaked worst were the ones *outside* the auth guard. `/driver` and
 * `/boards` are public and render in the operations shell, so a signed-in
 * ambulance driver who reached either one was shown the control room's
 * navigation — Operations, Fleet, Analytics, Settings — which is precisely
 * the "same admin page shown to the driver" fault. A guard that only ran on
 * authenticated routes could never have caught it.
 *
 * Anonymous visitors are passed straight through: the public screens are
 * public, and `RequireAuth` still handles everything that is not.
 */
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { homeForUser, mayVisit } from "@/app/portals";
import { useAuthStore } from "@/stores/authStore";

export function PortalGate({ children }: { children: ReactNode }) {
  const user = useAuthStore((state) => state.user);
  const status = useAuthStore((state) => state.status);
  const location = useLocation();

  if (status !== "authenticated") return <>{children}</>;

  // A signed-in user has no business on the sign-in form, and leaving them
  // there is how a stale `from` gets a second chance to fire.
  if (location.pathname === "/login") {
    return <Navigate to={homeForUser(user)} replace />;
  }

  if (!mayVisit(user, location.pathname)) {
    return <Navigate to={homeForUser(user)} replace />;
  }

  return <>{children}</>;
}
