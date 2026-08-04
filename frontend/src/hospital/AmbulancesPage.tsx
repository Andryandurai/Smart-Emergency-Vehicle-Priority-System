/**
 * Tab 2 — Ambulances.
 *
 * Who is coming, in the order they will actually be dealt with. The server
 * sorts by priority and then by ETA, which is the order a receiving team
 * works in: a Level 1 twelve minutes out is prepared for before a Level 3 that
 * arrives sooner.
 *
 * Selecting a row opens that ambulance's live navigation - the same route the
 * crew are driving and the same position their own console shows, because both
 * come from the trip's active route and the vehicle's last fix. The map here is
 * a plan view rather than the crew's heading-up one: a charge nurse is asking
 * "where are they and how long", not "which lane".
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Marker, useMap } from "react-leaflet";
import { useOutletContext } from "react-router-dom";

import { ApiError } from "@/api/client";
import { hospitalPortal } from "@/api/endpoints";
import type { Breakdown, InboundAmbulance } from "@/api/types";
import { MapCanvas, ROUTE_BLUE, RouteLine, hospitalIcon, vehicleIcon } from "@/components/MapCanvas";
import { fmtDistance, fmtEta } from "@/components/ui";
import type { HospitalOutletContext } from "@/hospital/HospitalShell";
import { useJourneyTick } from "@/hooks/useJourneyTick";
import { usePolling } from "@/hooks/usePolling";

export function AmbulancesPage() {
  const { code, board, refreshBoard } = useOutletContext<HospitalOutletContext>();
  const [rows, setRows] = useState<InboundAmbulance[]>([]);
  const [breakdowns, setBreakdowns] = useState<Breakdown[]>([]);
  const [openTrip, setOpenTrip] = useState<number | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!code) return;
      try {
        const result = await hospitalPortal.ambulances(code, signal);
        setRows(result.ambulances);
        setBreakdowns(result.breakdowns);
        setError(null);
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          setError(err instanceof Error ? err.message : "Could not load inbound ambulances.");
        }
      } finally {
        setLoaded(true);
      }
    },
    [code],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Two seconds: this is the screen an ED watches while an ambulance closes
  // the last few kilometres, and a stale ETA here is the difference between a
  // team standing ready and a team being called.
  usePolling((signal) => refresh(signal), 2000);

  // Keep the ambulances actually moving while this board is the only screen
  // open. The hospital is often the one display left up overnight.
  useJourneyTick(rows.length > 0);

  const receive = async (row: InboundAmbulance) => {
    setBusy(row.trip_id);
    setError(null);
    try {
      await hospitalPortal.patientReceived(row.trip_id, code ?? undefined);
      await refresh();
      await refreshBoard();
      if (openTrip === row.trip_id) setOpenTrip(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not record the handover.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="hp-page">
      <h1 className="hp-h1">
        Inbound ambulances
        <span className="hp-count">{rows.length} on the way</span>
      </h1>
      <p className="hp-lead">
        Ordered by emergency level, then by arrival time — the order to prepare in.
      </p>

      {error && <div className="hp-error">{error}</div>}

      {/* A crew broken down with a patient bound here is this hospital's
          problem too: the bed being held is needed later, or not at all. */}
      {breakdowns.map((breakdown) => (
        <div key={breakdown.id} className="hp-breakdown">
          <span className="hp-breakdown-tag">Ambulance breakdown</span>
          <div>
            <b>{breakdown.vehicle}</b> has broken down en route to you
            {breakdown.emergency_category_display
              ? ` with a ${breakdown.emergency_category_display.toLowerCase()} patient`
              : ""}
            .{" "}
            {breakdown.replacement
              ? `${breakdown.replacement} is taking over the transport.`
              : "A replacement ambulance is being sought."}
          </div>
        </div>
      ))}

      {loaded && rows.length === 0 && (
        <div className="hp-empty">
          No ambulance is currently bringing a patient to {board?.hospital.name ?? "this hospital"}.
        </div>
      )}

      <div className="hp-amb-list">
        {rows.map((row, index) => (
          <article
            key={row.trip_id}
            className={`hp-amb l${row.emergency_level}${row.has_arrived ? " arrived" : ""}${
              openTrip === row.trip_id ? " open" : ""
            }`}
          >
            <button
              type="button"
              className="hp-amb-head"
              onClick={() => setOpenTrip(openTrip === row.trip_id ? null : row.trip_id)}
              aria-expanded={openTrip === row.trip_id}
            >
              <span className="hp-amb-order">{index + 1}</span>

              <span className="hp-amb-id">
                <span className="hp-amb-number">{row.ambulance_number}</span>
                <span className="hp-dim">{row.registration || row.reference}</span>
              </span>

              <span className="hp-amb-crew">
                <span className="hp-dim">Driver</span>
                <b>{row.driver_name ?? "—"}</b>
                <span className="hp-dim">Paramedic</span>
                <b>{row.paramedic_name ?? "—"}</b>
              </span>

              <span className="hp-amb-eta">
                <b>{row.has_arrived ? "Arrived" : fmtEta(row.eta)}</b>
                <span className="hp-dim">{fmtDistance(row.distance_remaining_m)} away</span>
              </span>

              <span className="hp-amb-flags">
                <span className={`hp-level l${row.emergency_level}`}>L{row.emergency_level}</span>
                <span className="hp-category">{row.patient_category}</span>
                <span className="hp-dim">{row.current_status}</span>
              </span>
            </button>

            {openTrip === row.trip_id && (
              <div className="hp-amb-detail">
                <div className="hp-amb-nav">
                  <AmbulanceMap row={row} />
                </div>

                <div className="hp-amb-info">
                  <section>
                    <h3>Patient</h3>
                    {row.patient.redacted ? (
                      <p className="hp-dim">
                        Clinical details are withheld from your role.
                      </p>
                    ) : (
                      <>
                        <div className="hp-kv">
                          <span>Emergency category</span>
                          <b>{row.patient.emergency_category}</b>
                        </div>
                        <div className="hp-kv">
                          <span>Priority level</span>
                          <b>L{row.patient.priority_level}</b>
                        </div>
                        <div className="hp-kv">
                          <span>ETA</span>
                          <b>{fmtEta(row.patient.eta ?? row.eta)}</b>
                        </div>
                        {row.patient.patient_age != null && (
                          <div className="hp-kv">
                            <span>Age</span>
                            <b>{row.patient.patient_age}</b>
                          </div>
                        )}
                        {row.patient.deteriorating && (
                          <div className="hp-warn small">Patient reported as deteriorating.</div>
                        )}
                        <div className="hp-kv column">
                          <span>Symptoms</span>
                          <div className="hp-symptoms">
                            {row.patient.symptoms?.length ? (
                              row.patient.symptoms.map((symptom) => (
                                <span key={symptom} className="hp-symptom">
                                  {symptom}
                                </span>
                              ))
                            ) : (
                              <span className="hp-dim">None recorded</span>
                            )}
                          </div>
                        </div>
                        <div className="hp-kv column">
                          <span>Patient assessment</span>
                          <p className="hp-assessment">
                            {row.patient.assessment || "No assessment recorded yet."}
                          </p>
                        </div>
                      </>
                    )}
                  </section>

                  <section>
                    <h3>Journey</h3>
                    <div className="hp-kv">
                      <span>Current status</span>
                      <b>{row.current_status}</b>
                    </div>
                    <div className="hp-kv">
                      <span>Vehicle</span>
                      <b>{row.vehicle_status}</b>
                    </div>
                    <div className="hp-kv">
                      <span>Speed</span>
                      <b>{Math.round(row.current_location.speed_kmh)} km/h</b>
                    </div>
                    <div className="hp-kv">
                      <span>Distance remaining</span>
                      <b>{fmtDistance(row.distance_remaining_m)}</b>
                    </div>
                    <div className="hp-kv">
                      <span>Reference</span>
                      <b>{row.reference}</b>
                    </div>
                  </section>
                </div>

                {/* The receiving team's half of the handover. Enabled only on
                    arrival: pressing it while the ambulance is still moving
                    would close the trip, release the corridor and tell the
                    crew they had finished a journey they are still driving. */}
                <div className="hp-amb-actions">
                  <button
                    type="button"
                    className="hp-btn primary"
                    disabled={!row.has_arrived || busy === row.trip_id}
                    onClick={() => void receive(row)}
                    title={
                      row.has_arrived
                        ? "Confirm the patient is with your team"
                        : "Available once the ambulance has arrived"
                    }
                  >
                    {busy === row.trip_id
                      ? "Recording…"
                      : row.has_arrived
                        ? "Patient Received"
                        : "Patient Received (on arrival)"}
                  </button>
                </div>
              </div>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}

/**
 * One ambulance's live navigation.
 *
 * North-up and zoomed to fit, deliberately unlike the crew's heading-up view.
 * The question here is "how far away are they", which a rotating map makes
 * harder rather than easier.
 */
function AmbulanceMap({ row }: { row: InboundAmbulance }) {
  const position: [number, number] = [
    row.current_location.latitude,
    row.current_location.longitude,
  ];
  const destination: [number, number] | null =
    row.destination.latitude != null && row.destination.longitude != null
      ? [row.destination.latitude, row.destination.longitude]
      : null;

  return (
    <MapCanvas centre={position} zoom={14} className="map hp-map">
      <RouteLine geometry={row.route_geometry} colour={ROUTE_BLUE} />
      <Marker
        position={position}
        icon={vehicleIcon(row.emergency_level, "ambulance", {
          heading: row.current_location.heading_deg,
          focused: true,
        })}
        zIndexOffset={3000}
      />
      {destination && <Marker position={destination} icon={hospitalIcon({})} />}
      <FitRoute geometry={row.route_geometry} position={position} />
    </MapCanvas>
  );
}

/**
 * Frame the whole journey, once.
 *
 * Fitted on the first render and whenever the *route* changes, not on every
 * position update: refitting each time a fix arrives would zoom the map
 * steadily inwards as the ambulance closes on the hospital, which is exactly
 * when a charge nurse wants to keep seeing both ends.
 */
function FitRoute({
  geometry,
  position,
}: {
  geometry: [number, number][];
  position: [number, number];
}) {
  const map = useMap();
  const fittedFor = useRef<string>("");

  useEffect(() => {
    const key = `${geometry.length}:${geometry[0]?.join(",") ?? ""}:${geometry.at(-1)?.join(",") ?? ""}`;
    if (fittedFor.current === key) return;
    fittedFor.current = key;

    if (geometry.length < 2) {
      map.setView(position, 14);
      return;
    }
    map.fitBounds(geometry, { padding: [28, 28], animate: false });
  }, [map, geometry, position]);

  return null;
}
