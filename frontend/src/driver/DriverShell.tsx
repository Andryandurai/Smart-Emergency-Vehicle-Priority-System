/**
 * Driver portal shell.
 *
 * The third of SEVPS's three front ends, and deliberately unlike either of
 * the others. The operations console is a wall-mounted control room: dark,
 * dense, table-led, a city map on every screen. The paramedic app is a
 * handheld clinical tool: single column, card-led, bottom tab bar for a
 * thumb. This is neither — it is an instrument panel in a cab, read at a
 * glance by someone holding a steering wheel, so it is landscape, it uses a
 * left rail rather than a top or bottom bar, and its figures are large,
 * amber-on-charcoal and legible at arm's length.
 *
 * The structural difference is the point, not decoration. Three portals that
 * merely restyled the same layout would be one bug away from looking
 * identical again; three portals with different navigation geometry cannot be
 * mistaken for each other even mid-render.
 *
 * Everything behind it is the existing backend — the same shifts, trips,
 * recommender, corridor and sockets the other two use. Only the surface is
 * new.
 */
import { useCallback, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { profile as profileApi, shifts as shiftApi } from "@/api/endpoints";
import type { ChecklistDue, CrewShift, StaffProfile } from "@/api/types";
import { useAuthStore } from "@/stores/authStore";

interface Tab {
  to: string;
  label: string;
  glyph: string;
  /** Second line in the rail — what the tab is for, not what it is called. */
  hint: string;
}

/**
 * The whole portal, exactly four destinations.
 *
 * There is no fleet board, no analytics, no city map and no settings page: a
 * driver has no authority over any of them, and a rail full of tabs that
 * refuse to open teaches people to stop reading the rail.
 */
const TABS: Tab[] = [
  { to: "/d", label: "Take Over", glyph: "🚑", hint: "Ambulance & readiness" },
  { to: "/d/navigate", label: "Navigation", glyph: "🧭", hint: "Route & signals" },
  { to: "/d/hospitals", label: "Hospitals", glyph: "🏥", hint: "Capacity & choice" },
  { to: "/d/profile", label: "Profile", glyph: "👤", hint: "Your details" },
];

export function DriverShell() {
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const [me, setMe] = useState<StaffProfile | null>(null);
  const [shift, setShift] = useState<CrewShift | null>(null);
  const [checklistDue, setChecklistDue] = useState<ChecklistDue | null>(null);
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    profileApi.mine().then(setMe).catch(() => setMe(null));
  }, []);

  // Re-read after the profile screen may have changed the picture.
  useEffect(() => {
    if (location.pathname === "/d/profile") return;
    profileApi.mine().then(setMe).catch(() => undefined);
  }, [location.pathname]);

  /**
   * The shift badge in the rail.
   *
   * Polled rather than fetched once, because the state a driver most needs to
   * see change is the one they cannot cause themselves: the paramedic
   * accepting the sync request happens on somebody else's device, and a rail
   * still reading "Off duty" ten minutes after the shift went live is how a
   * crew ends up each believing the other has not signed on.
   */
  const refreshShift = useCallback(async () => {
    try {
      const mine = await shiftApi.mine();
      setShift(mine.shift);
      // Held at the shell so every tab agrees on it. The reminder has to
      // appear on arrival *and* on any later visit to any tab, which a flag
      // owned by one screen could not do.
      setChecklistDue(mine.checklist_due ?? null);
    } catch {
      setShift(null);
      setChecklistDue(null);
    }
  }, []);

  useEffect(() => {
    void refreshShift();
    const timer = window.setInterval(() => void refreshShift(), 6000);
    return () => window.clearInterval(timer);
  }, [refreshShift]);

  const signOut = async (): Promise<void> => {
    await logout();
    navigate("/login", { replace: true });
  };

  const onDuty = shift?.status === "active";

  return (
    <div className="dp-root">
      <nav className="dp-rail">
        <div className="dp-brand">
          <span className="dp-mark">SEVPS</span>
          <span className="dp-sub">Driver</span>
        </div>

        <div className="dp-tabs">
          {TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              end={tab.to === "/d"}
              className={({ isActive }) => `dp-tab${isActive ? " active" : ""}`}
            >
              <span className="dp-tab-glyph" aria-hidden>
                {tab.glyph}
              </span>
              <span className="dp-tab-text">
                <span className="dp-tab-label">{tab.label}</span>
                <span className="dp-tab-hint">{tab.hint}</span>
              </span>
            </NavLink>
          ))}
        </div>

        <div className="dp-rail-foot">
          <div className={`dp-duty ${onDuty ? "on" : "off"}`}>
            <span className="dp-duty-dot" aria-hidden />
            <span className="dp-duty-text">
              {onDuty ? shift?.vehicle_callsign : "Off duty"}
            </span>
          </div>
        </div>
      </nav>

      <div className="dp-body">
        <header className="dp-top">
          <div className="dp-top-title">
            {TABS.find((tab) =>
              tab.to === "/d" ? location.pathname === "/d" : location.pathname.startsWith(tab.to),
            )?.label ?? "Driver"}
          </div>

          <div className="dp-me">
            <div className="dp-me-text">
              <div className="dp-me-name">{me?.name ?? user?.username}</div>
              <div className="dp-me-role">{me?.staff_id || "Ambulance Driver"}</div>
            </div>
            <DriverAvatar profile={me} />
            <button type="button" className="dp-signout" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </header>

        <main className="dp-main">
          <Outlet context={{ shift, checklistDue, refreshShift }} />
        </main>
      </div>
    </div>
  );
}

/**
 * Driver avatar.
 *
 * Its own component rather than the paramedic portal's, which carries `pm-`
 * classes. Sharing it would mean a change to the paramedic's header could
 * silently restyle the driver's — exactly the coupling the portal split is
 * meant to remove.
 */
export function DriverAvatar({
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
        className="dp-avatar"
        src={profile.avatar_url}
        alt={profile.name}
        style={{ width: size, height: size }}
      />
    );
  }
  return (
    <span className="dp-avatar fallback" style={{ width: size, height: size }}>
      {initials}
    </span>
  );
}

/** What the shell hands every tab through the router outlet. */
export interface DriverOutletContext {
  shift: CrewShift | null;
  /** Non-null when a skipped readiness check has come due. See ChecklistDue. */
  checklistDue: ChecklistDue | null;
  refreshShift: () => Promise<void>;
}
