import { Navigate, Outlet, useLocation } from "react-router-dom";

import type { Role } from "@/api/types";
import { useAuthStore } from "@/stores/authStore";

interface RequireAuthProps {
  /** When given, the user must hold at least one of these roles. */
  roles?: Role[];
}

/**
 * Route guard.
 *
 * Client-side guards are a **usability** feature, not a security boundary -
 * every one of these routes is also enforced server-side by the permission
 * classes from Phase 2. The value here is not showing an operator a screen
 * that will only 403 on them.
 */
export function RequireAuth({ roles }: RequireAuthProps) {
  const status = useAuthStore((state) => state.status);
  const hasRole = useAuthStore((state) => state.hasRole);
  const location = useLocation();

  if (status !== "authenticated") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  if (roles?.length && !hasRole(...roles)) {
    return (
      <div className="page scroll">
        <div className="card" style={{ maxWidth: 560, margin: "10vh auto" }}>
          <h3>Not permitted</h3>
          <p className="muted">
            This screen needs one of: <b>{roles.join(", ")}</b>. Your account does not
            hold it.
          </p>
          <p className="muted" style={{ fontSize: 12 }}>
            Roles are granted by an administrator in the Django admin, or with{" "}
            <code>manage.py seed_users</code>.
          </p>
        </div>
      </div>
    );
  }

  return <Outlet />;
}
