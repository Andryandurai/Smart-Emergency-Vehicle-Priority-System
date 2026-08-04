import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { NotificationCentre } from "@/components/NotificationCentre";
import { useAuthStore } from "@/stores/authStore";

interface NavItem {
  to: string;
  label: string;
  /** Hidden when signed out - these all 401 without credentials. */
  authOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "Operations", authOnly: true },
  { to: "/fleet", label: "Fleet", authOnly: true },
  { to: "/ambulances", label: "Ambulances", authOnly: true },
  { to: "/hospitals", label: "Hospitals", authOnly: true },
  // The crew boards: who is on duty, with whom, on what. Distinct from
  // `/drive`, which is the in-vehicle console itself and stays reachable by
  // URL for an administrator who wants to see what a crew sees.
  { to: "/drivers", label: "Drivers", authOnly: true },
  { to: "/paramedics", label: "Paramedics", authOnly: true },
  // No "Road Alerts" tab. `/driver` is the public road-user alert receiver -
  // a civilian's screen, not an operator's - and it stays reachable at its own
  // URL for the phones and roadside devices that open it directly. What it
  // never was is a control-room destination, so it is off the admin nav.
  { to: "/boards", label: "Boards" },
  { to: "/settings", label: "Settings", authOnly: true },
];

export function AppShell() {
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const navigate = useNavigate();

  const signOut = async (): Promise<void> => {
    await logout();
    navigate("/login", { replace: true });
  };

  const roleLabel = user?.is_superuser ? "administrator" : user?.roles.join(", ") || "no role";

  return (
    <>
      <header className="topbar">
        <NavLink className="brand" to="/">
          <span className="brand-mark">SEVPS</span>
          <span className="brand-sub">Smart Emergency Vehicle Priority System</span>
        </NavLink>

        <nav className="topnav">
          {NAV.filter((item) => !item.authOnly || user).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="whoami">
          {/* Only for signed-in users: the inbox is role-scoped and 401s
              otherwise. Road users get their alerts through /driver. */}
          <NotificationCentre enabled={Boolean(user)} />
          {user ? (
            <>
              <span className="who">{user.username}</span>
              <span className="role">{roleLabel}</span>
              <button type="button" className="linkish" onClick={() => void signOut()}>
                Sign out
              </button>
            </>
          ) : (
            <NavLink className="signin" to="/login">
              Sign in
            </NavLink>
          )}
        </div>
      </header>

      <main className="page-root">
        <Outlet />
      </main>
    </>
  );
}
