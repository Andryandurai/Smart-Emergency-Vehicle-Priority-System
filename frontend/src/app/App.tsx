import { Suspense, lazy, useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/app/AppShell";
import { RequireAuth } from "@/app/RequireAuth";
import { BoardsPage } from "@/pages/BoardsPage";
import { DriverPage } from "@/pages/DriverPage";
import { HospitalListPage } from "@/pages/HospitalListPage";
import { HospitalPage } from "@/pages/HospitalPage";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { OperationsPage } from "@/pages/OperationsPage";
import { ParamedicPage } from "@/pages/ParamedicPage";
import { ParamedicSelectPage } from "@/pages/ParamedicSelectPage";
import { SettingsPage } from "@/pages/SettingsPage";
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
      <Routes>
        <Route path="/login" element={<LoginPage />} />

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
    </BrowserRouter>
  );
}
