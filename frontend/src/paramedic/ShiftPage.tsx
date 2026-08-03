/**
 * Start of shift.
 *
 * The paramedic's half of the crew handshake. The driver picks the ambulance
 * and sends the request; this screen shows what arrived, and accepting it is
 * what starts the day. A paramedic never chooses a vehicle here - they are
 * not standing at it, and a shift opened on an ambulance nobody is sitting in
 * is worse than no shift at all.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/api/client";
import { fleet, shifts as shiftApi } from "@/api/endpoints";
import type { CrewShift, MyShift, VehiclePayload } from "@/api/types";
import { fmtTime } from "@/components/ui";

export function ShiftPage() {
  const [state, setState] = useState<MyShift | null>(null);
  const [vehicle, setVehicle] = useState<VehiclePayload | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const mine = await shiftApi.mine();
      setState(mine);
      setError(null);
      if (mine.shift) {
        // Full ambulance details for the card - registration, ownership,
        // capability - which the shift payload only summarises.
        const live = await fleet.live();
        setVehicle(
          live.vehicles.find((v) => v.callsign === mine.shift!.vehicle_callsign) ?? null,
        );
      } else {
        setVehicle(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your shift.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while explicitly waiting for a request, so the screen is not
  // hitting the API every few seconds all shift for no reason.
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [waiting, refresh]);

  const act = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    try {
      await action();
      await refresh();
      setWaiting(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(null);
    }
  };

  const shift = state?.shift ?? null;
  const pending = state?.awaiting_my_acceptance ?? [];

  // --- on duty ------------------------------------------------------------
  if (shift && shift.status === "active") {
    return (
      <div className="pm-page">
        <div className="pm-hero on-duty">
          <span className="pm-hero-tag">On duty</span>
          <h1>{shift.vehicle_callsign}</h1>
          <p>Since {fmtTime(shift.accepted_at)}</p>
        </div>

        <AmbulanceCard shift={shift} vehicle={vehicle} />

        <section className="pm-card">
          <h3>Crew</h3>
          <div className="pm-crew">
            <div className="pm-crew-row">
              <span className="k">Driver</span>
              <b>{shift.driver_detail?.name ?? "—"}</b>
            </div>
            <div className="pm-crew-row">
              <span className="k">Paramedic</span>
              <b>{shift.paramedic_detail?.name ?? "—"} (you)</b>
            </div>
          </div>
        </section>

        {error && <div className="pm-error">{error}</div>}

        <button
          type="button"
          className="pm-btn danger"
          disabled={busy !== null}
          onClick={() => void act("end", () => shiftApi.end(shift.id))}
        >
          {busy === "end" ? "Ending…" : "End shift"}
        </button>
      </div>
    );
  }

  // --- a request is waiting -----------------------------------------------
  if (pending.length > 0) {
    return (
      <div className="pm-page">
        <div className="pm-hero request">
          <span className="pm-hero-tag">Sync request</span>
          <h1>{pending[0]!.vehicle_callsign}</h1>
          <p>
            <b>{pending[0]!.driver_detail?.name ?? "Your driver"}</b> wants to crew with you
          </p>
        </div>

        {pending.map((request) => (
          <div key={request.id} className="pm-card">
            <div className="pm-kv">
              <span>Ambulance</span>
              <b>{request.vehicle_callsign}</b>
            </div>
            <div className="pm-kv">
              <span>Vehicle number</span>
              <b className="pm-plate">{request.vehicle_registration || "—"}</b>
            </div>
            <div className="pm-kv">
              <span>Driver</span>
              <b>{request.driver_detail?.name ?? "—"}</b>
            </div>
            <div className="pm-kv">
              <span>Requested</span>
              <b>{fmtTime(request.requested_at)}</b>
            </div>

            {error && <div className="pm-error">{error}</div>}

            <button
              type="button"
              className="pm-btn primary"
              disabled={busy !== null}
              onClick={() => void act("accept", () => shiftApi.accept(request.id))}
            >
              {busy === "accept" ? "Accepting…" : "Accept & start shift"}
            </button>
            <button
              type="button"
              className="pm-btn ghost"
              disabled={busy !== null}
              onClick={() =>
                void act("decline", () => shiftApi.decline(request.id, "Declined by paramedic"))
              }
            >
              Decline
            </button>
          </div>
        ))}
      </div>
    );
  }

  // --- nothing yet --------------------------------------------------------
  return (
    <div className="pm-page">
      <div className="pm-hero idle">
        <span className="pm-hero-tag">Off duty</span>
        <h1>Start your shift</h1>
        <p>Your driver sends the request once they have taken over an ambulance.</p>
      </div>

      {error && <div className="pm-error">{error}</div>}

      {waiting ? (
        <div className="pm-card waiting">
          <div className="pm-spinner" aria-hidden />
          <h3>Waiting for your driver</h3>
          <p>
            They select the ambulance and send you a sync request. This screen updates
            on its own — no need to refresh.
          </p>
          <button type="button" className="pm-btn ghost" onClick={() => setWaiting(false)}>
            Stop waiting
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="pm-btn primary big"
          onClick={() => {
            setWaiting(true);
            void refresh();
          }}
        >
          Start shift
        </button>
      )}

      <p className="pm-note">
        Only the driver can select the ambulance and open the shift. You accept it.
      </p>
    </div>
  );
}

function AmbulanceCard({
  shift,
  vehicle,
}: {
  shift: CrewShift;
  vehicle: VehiclePayload | null;
}) {
  return (
    <section className="pm-card">
      <h3>Ambulance</h3>
      <div className="pm-plate-big">{shift.vehicle_registration || "—"}</div>
      <div className="pm-kv">
        <span>Ambulance ID</span>
        <b>{shift.vehicle_callsign}</b>
      </div>
      <div className="pm-kv">
        <span>Type</span>
        <b>{vehicle?.vehicle_type_display ?? "Ambulance"}</b>
      </div>
      <div className="pm-kv">
        <span>Operator</span>
        <b>{vehicle?.operator || "—"}</b>
      </div>
      <div className="pm-kv">
        <span>Ownership</span>
        <b>{vehicle?.ownership_display ?? "—"}</b>
      </div>
      <div className="pm-kv">
        <span>Capability</span>
        <b>{vehicle?.is_als ? "Advanced Life Support" : "Basic Life Support"}</b>
      </div>
      <div className="pm-kv">
        <span>Vehicle status</span>
        <b>{vehicle?.readiness_display ?? "—"}</b>
      </div>
      <div className="pm-kv">
        <span>Current state</span>
        <b>{vehicle?.status_display ?? vehicle?.status ?? "—"}</b>
      </div>
      {shift.equipment_check?.skipped && shift.equipment_check.is_outstanding && (
        <div className="pm-warn">
          Vehicle inspection was skipped for an emergency — still outstanding.
        </div>
      )}
    </section>
  );
}
