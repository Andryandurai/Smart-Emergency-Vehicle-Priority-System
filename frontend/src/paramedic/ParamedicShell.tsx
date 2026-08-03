/**
 * Paramedic portal shell.
 *
 * A separate layout, not the operations console with items hidden. The two
 * jobs have nothing in common: a control-room operator watches a whole city
 * on one wide screen for hours, while a paramedic works one patient at a
 * time, standing up, one-handed, in a moving vehicle. So this is card-led
 * rather than table-led, has four destinations rather than nine, uses large
 * touch targets, and never shows a city map.
 *
 * Everything behind it is the existing backend - the same shifts, trips,
 * recommender and sockets. Only the surface is different.
 */
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";

import { profile as profileApi } from "@/api/endpoints";
import type { StaffProfile } from "@/api/types";
import { useAuthStore } from "@/stores/authStore";

interface Tab {
  to: string;
  label: string;
  glyph: string;
}

/**
 * The whole portal. Operations, Fleet, Drive, Road alerts, Boards, Analytics
 * and Settings are deliberately absent - a paramedic has no fleet or traffic
 * authority, and Settings is replaced by Profile.
 */
const TABS: Tab[] = [
  { to: "/p", label: "Shift", glyph: "🚑" },
  { to: "/p/emergency", label: "Emergency", glyph: "＋" },
  { to: "/p/navigate", label: "Navigation", glyph: "🧭" },
  { to: "/p/hospitals", label: "Hospitals", glyph: "⚕" },
  { to: "/p/profile", label: "Profile", glyph: "👤" },
];

export function ParamedicShell() {
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const [me, setMe] = useState<StaffProfile | null>(null);
  const location = useLocation();
  const navigate = useNavigate();

  // Leaving the portal is part of signing out. Without the navigation this
  // relied on the auth guard noticing and bouncing, which left the paramedic
  // layout on screen for a frame and, worse, left `/p` as the path the login
  // screen was arrived from - the stale `from` the next sign-in used to obey.
  const signOut = async (): Promise<void> => {
    await logout();
    navigate("/login", { replace: true });
  };

  useEffect(() => {
    profileApi.mine().then(setMe).catch(() => setMe(null));
  }, []);

  // Re-read after the profile screen may have changed the picture.
  useEffect(() => {
    if (location.pathname === "/p/profile") return;
    profileApi.mine().then(setMe).catch(() => undefined);
  }, [location.pathname]);

  return (
    <div className="pm-root">
      <header className="pm-top">
        <div className="pm-brand">
          <span className="pm-mark">SEVPS</span>
          <span className="pm-sub">Paramedic</span>
        </div>
        <div className="pm-me">
          <div className="pm-me-text">
            <div className="pm-me-name">{me?.name ?? user?.username}</div>
            <div className="pm-me-role">{me?.staff_id || "Paramedic"}</div>
          </div>
          <Avatar profile={me} />
          <button type="button" className="pm-signout" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      <main className="pm-main">
        <Outlet />
      </main>

      {/* Bottom bar rather than a top nav: this is reached with a thumb, and
          the top of a held device is the hardest place to touch. */}
      <nav className="pm-tabs">
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === "/p"}
            className={({ isActive }) => `pm-tab${isActive ? " active" : ""}`}
          >
            <span className="pm-tab-glyph" aria-hidden>
              {tab.glyph}
            </span>
            <span className="pm-tab-label">{tab.label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}

export function Avatar({
  profile,
  size = 40,
}: {
  profile: StaffProfile | null;
  size?: number;
}) {
  const initials = (profile?.name ?? "?")
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0] ?? "")
    .join("")
    .toUpperCase();

  if (profile?.avatar_url) {
    return (
      <img
        className="pm-avatar"
        src={profile.avatar_url}
        alt={profile.name}
        style={{ width: size, height: size }}
      />
    );
  }
  return (
    <span className="pm-avatar fallback" style={{ width: size, height: size }}>
      {initials}
    </span>
  );
}
