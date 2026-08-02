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
  { to: "/hospitals", label: "Hospitals", authOnly: true },
  { to: "/paramedic", label: "Paramedic", authOnly: true },
  { to: "/driver", label: "Driver" },
  { to: "/boards", label: "Boards" },
  { to: "/analytics", label: "Analytics", authOnly: true },
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
          {user?.is_staff && (
            <a href="/admin/" target="_blank" rel="noopener noreferrer">
              Admin
            </a>
          )}
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
