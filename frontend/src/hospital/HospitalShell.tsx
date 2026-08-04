/**
 * Hospital portal shell.
 *
 * The fourth front end, and the one with the most distinct reader: this is a
 * board on the wall of an emergency department, watched from across the room
 * by people whose hands are busy. So it is light rather than dark - an ED is
 * brightly lit and a dark board reflects the ceiling lights back at you - the
 * figures are large, and the whole thing is legible at four metres.
 *
 * That makes four portals with four different navigation geometries:
 * operations a top bar, driver a left rail, paramedic a bottom bar, and this a
 * horizontal tab strip under a status header. None of them can be mistaken for
 * another, which is what keeps them from collapsing back into one another.
 *
 * Behind it is the existing Layer 5 backend - the same trips, corridor ETAs
 * and capacity rows the recommender reads.
 */
import { useCallback, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { hospitalPortal } from "@/api/endpoints";
import type { HospitalChoice, HospitalDashboard } from "@/api/types";
import { useAuthStore } from "@/stores/authStore";

const TABS = [
  { to: "/h", label: "Dashboard" },
  { to: "/h/ambulances", label: "Ambulances" },
  { to: "/h/updates", label: "Updates" },
];

/** Where the chosen ward is remembered between visits. */
const STORAGE_KEY = "sevps.hospital.code";

export function HospitalShell() {
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const navigate = useNavigate();
  const location = useLocation();

  const [choices, setChoices] = useState<HospitalChoice[]>([]);
  const [bound, setBound] = useState(false);
  const [code, setCode] = useState<string | null>(
    () => window.localStorage.getItem(STORAGE_KEY),
  );
  const [board, setBoard] = useState<HospitalDashboard | null>(null);

  useEffect(() => {
    hospitalPortal
      .choices()
      .then((result) => {
        setChoices(result.hospitals);
        setBound(result.bound);
        // A bound account has exactly one ward and no say in the matter, so a
        // code left in storage by an earlier session must not override it.
        if (result.bound && result.hospitals[0]) {
          setCode(result.hospitals[0].code);
        } else if (!code && result.hospitals[0]) {
          setCode(result.hospitals[0].code);
        }
      })
      .catch(() => setChoices([]));
    // Runs once: the roster of wards this account may open does not change
    // while the board is up.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (code) window.localStorage.setItem(STORAGE_KEY, code);
  }, [code]);

  /**
   * The header's status pill.
   *
   * Polled here rather than in the Dashboard tab so the pill stays true while
   * the Ambulances or Updates tab is open - the whole point of a header status
   * is that it does not depend on which screen somebody left up.
   */
  const refreshBoard = useCallback(async () => {
    if (!code) return;
    try {
      setBoard(await hospitalPortal.dashboard(code));
    } catch {
      setBoard(null);
    }
  }, [code]);

  useEffect(() => {
    void refreshBoard();
    const timer = window.setInterval(() => void refreshBoard(), 10000);
    return () => window.clearInterval(timer);
  }, [refreshBoard]);

  const signOut = async (): Promise<void> => {
    await logout();
    navigate("/login", { replace: true });
  };

  const here = TABS.find((tab) =>
    tab.to === "/h" ? location.pathname === "/h" : location.pathname.startsWith(tab.to),
  );

  return (
    <div className="hp-root">
      <header className="hp-top">
        <div className="hp-identity">
          <div className="hp-brand">
            <span className="hp-mark">SEVPS</span>
            <span className="hp-sub">Hospital</span>
          </div>
          <div className="hp-hospital">
            <div className="hp-hospital-name">{board?.hospital.name ?? "Loading…"}</div>
            <div className="hp-hospital-meta">
              {board ? `${board.hospital.code} · ${board.hospital.city}` : ""}
            </div>
          </div>
        </div>

        <div className="hp-header-right">
          {board && (
            <span className={`hp-status ${board.status}`}>
              <span className="hp-status-dot" aria-hidden />
              {board.status}
            </span>
          )}

          {/* Only where the deployment has not bound this account to one ward.
              With a binding the server ignores the request anyway, so offering
              the choice would be a control that does nothing. */}
          {!bound && choices.length > 1 && (
            <select
              className="hp-picker"
              aria-label="Hospital"
              value={code ?? ""}
              onChange={(event) => setCode(event.target.value)}
            >
              {choices.map((choice) => (
                <option key={choice.code} value={choice.code}>
                  {choice.name}
                </option>
              ))}
            </select>
          )}

          <span className="hp-user">{user?.username}</span>
          <button type="button" className="hp-signout" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      <nav className="hp-tabs" aria-label="Hospital sections">
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === "/h"}
            className={({ isActive }) => `hp-tab${isActive ? " active" : ""}`}
          >
            {tab.label}
            {tab.to === "/h/ambulances" && board?.active_ambulances_coming ? (
              <span className="hp-tab-count">{board.active_ambulances_coming}</span>
            ) : null}
          </NavLink>
        ))}
        <span className="hp-tab-here">{here?.label}</span>
      </nav>

      <main className="hp-main">
        <Outlet context={{ code, board, refreshBoard }} />
      </main>
    </div>
  );
}

/** What the shell hands each tab through the router outlet. */
export interface HospitalOutletContext {
  /** The ward being shown. Null only before the first choices call returns. */
  code: string | null;
  board: HospitalDashboard | null;
  refreshBoard: () => Promise<void>;
}
