import { Suspense, lazy, useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/app/AppShell";
import { PortalGate } from "@/app/PortalGate";
import { RequireAuth } from "@/app/RequireAuth";
import { DriverShell } from "@/driver/DriverShell";
import { HospitalsPage as DriverHospitalsPage } from "@/driver/HospitalsPage";
import { NavigationPage as DriverNavigationPage } from "@/driver/NavigationPage";
import { ProfilePage as DriverProfilePage } from "@/driver/ProfilePage";
import { TakeoverPage } from "@/driver/TakeoverPage";
import { BoardsPage } from "@/pages/BoardsPage";
import { DriverConsolePage } from "@/pages/DriverConsolePage";
import { DriverPage } from "@/pages/DriverPage";
import { FleetBoardPage } from "@/pages/FleetBoardPage";
import { HospitalListPage } from "@/pages/HospitalListPage";
import { HospitalPage } from "@/pages/HospitalPage";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { OperationsPage } from "@/pages/OperationsPage";
import { ParamedicPage } from "@/pages/ParamedicPage";
import { ParamedicSelectPage } from "@/pages/ParamedicSelectPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { HospitalsPage as ParamedicHospitalsPage } from "@/paramedic/HospitalsPage";
import { NavigationPage as ParamedicNavigationPage } from "@/paramedic/NavigationPage";
import { NewEmergencyPage } from "@/paramedic/NewEmergencyPage";
import { ParamedicShell } from "@/paramedic/ParamedicShell";
import { ProfilePage as ParamedicProfilePage } from "@/paramedic/ProfilePage";
import { ShiftPage } from "@/paramedic/ShiftPage";
import { useAuthStore } from "@/stores/authStore";

/**
 * Analytics is the only screen that needs Recharts (~470 kB). Loading it
 * eagerly would put charting code in front of every operator opening the
 * live map during an incident - the screen where load time actually matters.
 */
const AnalyticsPage = lazy(() =>
  import("@/pages/AnalyticsPage").then((module) => ({ default: module.AnalyticsPage })),
);

export function App() {
  const bootstrap = useAuthStore((state) => state.bootstrap);
  const status = useAuthStore((state) => state.status);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  // Guards must not redirect while the silent refresh is still in flight, or
  // every reload bounces an authenticated operator to the login screen.
  if (status === "idle" || status === "checking") {
    return (
      <div className="boot">
        <div className="boot-mark">SEVPS</div>
        <div className="boot-sub">Restoring session…</div>
      </div>
    );
  }

  return (
    <BrowserRouter>
      {/* Portal isolation, above every shell. Answers "may this signed-in
          user be on this path" for the whole route table, including the
          public screens that sit outside RequireAuth. See PortalGate. */}
      <PortalGate>
      <Routes>
        <Route path="/login" element={<LoginPage />} />

        {/* The paramedic portal is a separate application shell, not the
            operations console with items hidden. It has its own layout, its
            own navigation and no city map. See ParamedicShell. */}
        <Route element={<RequireAuth />}>
          <Route path="/p" element={<ParamedicShell />}>
            <Route index element={<ShiftPage />} />
            <Route path="emergency" element={<NewEmergencyPage />} />
            <Route path="navigate" element={<ParamedicNavigationPage />} />
            <Route path="hospitals" element={<ParamedicHospitalsPage />} />
            <Route path="profile" element={<ParamedicProfilePage />} />
          </Route>
        </Route>

        {/* The driver portal, likewise its own application: a cab instrument
            panel with four tabs and no city map. Distinct from `/driver`,
            which is the public road-user alert receiver - same word, opposite
            side of the windscreen. See DriverShell. */}
        <Route element={<RequireAuth />}>
          <Route path="/d" element={<DriverShell />}>
            <Route index element={<TakeoverPage />} />
            <Route path="navigate" element={<DriverNavigationPage />} />
            <Route path="hospitals" element={<DriverHospitalsPage />} />
            <Route path="profile" element={<DriverProfilePage />} />
          </Route>
        </Route>

        <Route element={<AppShell />}>
          {/* Public-facing screens: a road user and a roadside sign have no
              credentials, and Layer 4 depends on them working anyway. */}
          <Route path="/driver" element={<DriverPage />} />
          <Route path="/boards" element={<BoardsPage />} />

          <Route element={<RequireAuth />}>
            <Route path="/" element={<OperationsPage />} />
            <Route path="/hospitals" element={<HospitalListPage />} />
            <Route path="/hospital/:code" element={<HospitalPage />} />
            <Route path="/paramedic" element={<ParamedicSelectPage />} />
            <Route path="/paramedic/:callsign" element={<ParamedicPage />} />
            {/* The ambulance driver's console. Distinct from /driver, which
                is the public road-user alert receiver - same word, opposite
                side of the windscreen. */}
            <Route path="/drive" element={<DriverConsolePage />} />
            <Route path="/fleet" element={<FleetBoardPage />} />
            <Route
              path="/analytics"
              element={
                <Suspense fallback={<div className="boot-sub">Loading charts…</div>}>
                  <AnalyticsPage />
                </Suspense>
              }
            />
            <Route path="/settings" element={<SettingsPage />} />
          </Route>

          <Route path="/index.html" element={<Navigate to="/" replace />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
      </PortalGate>
    </BrowserRouter>
  );
}
