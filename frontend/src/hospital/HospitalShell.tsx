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
 * The shell owns the hospital socket. Alerts have to arrive whichever tab is
 * open - an ambulance assigned while somebody is editing bed counts is exactly
 * the one that must not be missed - so the connection and the popups live
 * here, above the tabs, and the persistent panel is rendered by the dashboard.
 *
 * Behind it is the existing Layer 5 backend: the same trips, corridor ETAs and
 * capacity rows the recommender reads.
 */
import { useCallback, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { hospitalPortal } from "@/api/endpoints";
import type { HospitalDashboard } from "@/api/types";
import { useHospitalNotices } from "@/hospital/notifications";
import type { HospitalNotice } from "@/hospital/notifications";
import { useSocket } from "@/hooks/useSocket";
import { useAuthStore } from "@/stores/authStore";

const TABS = [
  { to: "/h", label: "Dashboard" },
  { to: "/h/ambulances", label: "Ambulances" },
  { to: "/h/updates", label: "Updates" },
];

export function HospitalShell() {
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const navigate = useNavigate();
  const location = useLocation();

  const [code, setCode] = useState<string | null>(null);
  const [board, setBoard] = useState<HospitalDashboard | null>(null);

  const push = useHospitalNotices((state) => state.push);
  const notices = useHospitalNotices((state) => state.items);
  const resetNotices = useHospitalNotices((state) => state.reset);

  /**
   * Which ward this account opens.
   *
   * Asked for rather than chosen. Each hospital login is bound to one ward
   * through `Hospital.staff_group`, so the server returns exactly one row and
   * there is nothing to pick - the header used to carry a dropdown, which let
   * a charge nurse change whose beds they were editing by brushing a control.
   */
  useEffect(() => {
    hospitalPortal
      .choices()
      .then((result) => setCode(result.hospitals[0]?.code ?? null))
      .catch(() => setCode(null));
  }, []);

  /**
   * The header's status pill.
   *
   * Polled here rather than in the Dashboard tab so it stays true while the
   * Ambulances or Updates tab is open - the whole point of a header status is
   * that it does not depend on which screen somebody left up.
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

  // --- alerts -------------------------------------------------------------
  //
  // All four already exist on the hospital channel; nothing new is published
  // for the portal's benefit. `hospital_alert` is raised the moment a crew
  // commit to a destination - which is precisely "a driver and paramedic have
  // chosen this hospital and navigation has begun".
  const { status: socket } = useSocket(code ? `/ws/hospital/${code}/` : "", {
    handlers: {
      hospital_alert: (data) => {
        const alert = data as {
          trip: number;
          vehicle_callsign: string;
          category_display: string;
          priority_level: number;
          eta: string | null;
        };
        push({
          id: `inbound:${alert.trip}`,
          kind: "inbound",
          title: "Ambulance inbound",
          body:
            `${alert.vehicle_callsign} is on the way with a ` +
            `${(alert.category_display ?? "emergency").toLowerCase()} patient ` +
            `(Level ${alert.priority_level}).`,
          tripId: alert.trip,
          callsign: alert.vehicle_callsign ?? "",
          at: new Date().toISOString(),
        });
        void refreshBoard();
      },

      trip_stage: (data) => {
        const event = data as {
          id?: number;
          trip_id?: number;
          stage: string;
          vehicle?: string;
          vehicle_callsign?: string;
        };
        const tripId = event.trip_id ?? event.id ?? null;
        const callsign = event.vehicle_callsign ?? event.vehicle ?? "";
        if (event.stage === "arrived") {
          push({
            id: `arrived:${tripId}`,
            kind: "arrived",
            title: "Ambulance arrived",
            body: `${callsign || "An ambulance"} has arrived. Confirm once the patient is with your team.`,
            tripId,
            callsign,
            at: new Date().toISOString(),
          });
        }
        void refreshBoard();
      },

      // A new destination, an ETA revision or a re-plan. No alert - it is not
      // news, it is the same patient - but the board's counts change.
      inbound_update: () => void refreshBoard(),

      ambulance_breakdown: (data) => {
        const event = data as { vehicle?: string; trip_id?: number };
        push({
          id: `breakdown:${event.trip_id ?? event.vehicle ?? "unknown"}`,
          kind: "breakdown",
          title: "Ambulance breakdown",
          body: `${event.vehicle ?? "An ambulance"} bound for you has broken down. A replacement is being sought.`,
          tripId: event.trip_id ?? null,
          callsign: event.vehicle ?? "",
          at: new Date().toISOString(),
        });
        void refreshBoard();
      },

      /**
       * The patient is through the door.
       *
       * Raised by "Patient Received", and it carries the outstanding
       * admission: the ward's resources have not moved yet, and until somebody
       * admits them the recommender is still routing on beds this patient is
       * lying in. So the notice is an action, not an announcement.
       */
      patient_received: (data) => {
        const event = data as {
          id?: number;
          reference?: string;
          vehicle_callsign?: string;
        };
        push({
          id: `received:${event.id ?? event.reference ?? "unknown"}`,
          kind: "received",
          title: "Patient arrived",
          body:
            `${event.vehicle_callsign ?? "The ambulance"} has handed over ` +
            `${event.reference ?? "the patient"}. Admit them to stand the ward's ` +
            `resources down.`,
          tripId: event.id ?? null,
          callsign: event.vehicle_callsign ?? "",
          at: new Date().toISOString(),
          needsAdmission: true,
        });
        void refreshBoard();
      },

      patient_admitted: () => void refreshBoard(),
      capacity_updated: () => void refreshBoard(),
    },
  });

  // A different hospital's board must never inherit this one's alerts.
  useEffect(() => {
    resetNotices();
  }, [code, resetNotices]);

  const signOut = async (): Promise<void> => {
    await logout();
    navigate("/login", { replace: true });
  };

  const here = TABS.find((tab) =>
    tab.to === "/h" ? location.pathname === "/h" : location.pathname.startsWith(tab.to),
  );

  const popups = notices.filter((notice) => notice.fresh && !notice.acknowledged).slice(0, 3);

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
          <span className={`hp-link ${socket}`} title={`Live link: ${socket}`} aria-hidden />
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

      {/* Popups sit above every tab. They stand for a long time and then
          settle by themselves - settling clears only the popup, never the
          entry in the Alerts division, which leaves when the ward removes it
          and not before. */}
      {popups.length > 0 && (
        <div className="hp-popups" role="alert" aria-live="assertive">
          {popups.map((notice) => (
            <Popup key={notice.id} notice={notice} />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * How long a popup stands before it settles.
 *
 * Forty-five seconds, not the four a toast usually gets. The reader is a
 * charge nurse who may be across the department with their hands full when an
 * ambulance is assigned, and a prompt that has gone by the time they look up
 * has told nobody anything. It is generous rather than permanent because the
 * Alerts division is the permanent record - the popup only has to survive long
 * enough to be noticed.
 */
const POPUP_VISIBLE_MS = 45_000;

function Popup({ notice }: { notice: HospitalNotice }) {
  const acknowledge = useHospitalNotices((state) => state.acknowledge);
  const settle = useHospitalNotices((state) => state.settle);
  const navigate = useNavigate();

  // Settle, never remove: the row has to survive into the panel so the ward
  // can still act on it after the prompt has gone.
  useEffect(() => {
    const timer = window.setTimeout(() => settle(notice.id), POPUP_VISIBLE_MS);
    return () => window.clearTimeout(timer);
  }, [notice.id, settle]);

  return (
    <div className={`hp-popup ${notice.kind}`}>
      <div className="hp-popup-head">
        <span className="hp-popup-tag">{notice.title}</span>
        {notice.callsign && <span className="hp-popup-call">{notice.callsign}</span>}
      </div>
      <p>{notice.body}</p>
      <div className="hp-popup-actions">
        {notice.needsAdmission ? (
          // The admission is the only action worth offering on an arrival: it
          // is what actually moves the ward's figures.
          <button
            type="button"
            className="hp-btn primary small"
            onClick={() => {
              settle(notice.id);
              navigate("/h");
            }}
          >
            Admit patient
          </button>
        ) : (
          <button
            type="button"
            className="hp-btn primary small"
            onClick={() => {
              // Opening the ambulance list is itself a response, so the popup
              // goes - but the entry stays in the panel until acknowledged.
              settle(notice.id);
              navigate("/h/ambulances");
            }}
          >
            View ambulance
          </button>
        )}
        <button
          type="button"
          className="hp-btn ghost small"
          onClick={() => acknowledge(notice.id)}
        >
          {notice.needsAdmission ? "Later" : "Acknowledge"}
        </button>
      </div>
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
