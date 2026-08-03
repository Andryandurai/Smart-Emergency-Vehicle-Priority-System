/** Hospital Preparedness Dashboard - feature 4.7. */
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { ApiError } from "@/api/client";
import { hospitals } from "@/api/endpoints";
import type { Hospital, HospitalCapacity, Trip } from "@/api/types";
import { Dot, MapCanvas, VehicleMarkers } from "@/components/MapCanvas";
import {
  Badge, Card, ConnectionDot, Empty, ErrorNote, RedactionNote, Stat,
  fmtDistance, fmtEta, levelClass,
} from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";
import { useAuthStore } from "@/stores/authStore";

export function HospitalPage() {
  const { code = "" } = useParams();
  const canWrite = useAuthStore((state) => state.hasRole("hospital_staff", "administrators"));

  const [hospital, setHospital] = useState<Hospital | null>(null);
  const [inbound, setInbound] = useState<Trip[]>([]);
  const [capacity, setCapacity] = useState<Partial<HospitalCapacity>>({});
  const [error, setError] = useState<string | null>(null);
  const [, setTick] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const all = await hospitals.list(signal);
        const match = all.find((row) => row.code.toLowerCase() === code.toLowerCase()) ?? null;
        setHospital(match);
        if (match) {
          setInbound(await hospitals.inbound(match.id));
          if (match.capacity) setCapacity(match.capacity);
        }
      } catch (err) {
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Could not load the hospital.");
        }
      }
    },
    [code],
  );

  useEffect(() => {
    void load();
  }, [load]);
  usePolling((signal) => load(signal), 4000, { immediate: false });

  const { status } = useSocket(`/ws/hospital/${encodeURIComponent(code)}/`, {
    handlers: {
      snapshot: (data) => {
        const snapshot = data as { capacity: HospitalCapacity };
        if (snapshot.capacity) setCapacity(snapshot.capacity);
      },
      capacity_updated: (data) => setCapacity(data as HospitalCapacity),
      inbound_update: () => void load(),
      trip_stage: () => void load(),
      hospital_alert: () => void load(),
    },
  });

  const publishCapacity = async (): Promise<void> => {
    if (!hospital) return;
    setError(null);
    try {
      setCapacity(
        await hospitals.updateCapacity(hospital.id, {
          emergency_beds_available: Number(capacity.emergency_beds_available ?? 0),
          icu_beds_available: Number(capacity.icu_beds_available ?? 0),
          patients_waiting: Number(capacity.patients_waiting ?? 0),
          doctors_on_duty: Number(capacity.doctors_on_duty ?? 0),
        }),
      );
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 403
          ? "Your role cannot publish capacity. Sign in as hospital staff."
          : (err as Error).message,
      );
    }
  };

  const toggleDiversion = async (): Promise<void> => {
    if (!hospital) return;
    setError(null);
    try {
      setHospital(
        await hospitals.setDiversion(
          hospital.id,
          !hospital.is_on_diversion,
          hospital.is_on_diversion ? "" : "ED at capacity",
        ),
      );
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 403
          ? "Diversion is a clinical decision — hospital staff only."
          : (err as Error).message,
      );
    }
  };

  if (!hospital) {
    return (
      <div className="page scroll">
        <ErrorNote error={error} />
        <Empty>Loading hospital {code}…</Empty>
      </div>
    );
  }

  const field = (key: keyof HospitalCapacity) => ({
    value: String(capacity[key] ?? ""),
    onChange: (event: React.ChangeEvent<HTMLInputElement>) =>
      setCapacity((prev) => ({ ...prev, [key]: Number(event.target.value) })),
  });

  return (
    <div className="split right">
      <MapCanvas centre={[hospital.latitude, hospital.longitude]} zoom={13}>
        <Dot position={[hospital.latitude, hospital.longitude]} colour="#2ecc71" radius={10}>
          <b>{hospital.name}</b>
        </Dot>
        <VehicleMarkers
          vehicles={inbound.map((trip) => ({
            id: trip.vehicle,
            uuid: String(trip.vehicle),
            callsign: trip.vehicle_callsign,
            vehicle_type: trip.vehicle_type,
            status: "transporting",
            latitude: trip.vehicle_latitude,
            longitude: trip.vehicle_longitude,
            heading_deg: 0,
            speed_kmh: trip.vehicle_speed_kmh,
            priority_level: trip.priority_level,
            siren_mode: trip.siren_mode,
            light_pattern: trip.light_pattern,
            last_seen_at: null,
            is_stale: false,
          }))}
        />
      </MapCanvas>

      <aside className="sidebar right">
        <div className="sidebar-head">
          <ConnectionDot status={status} />
        </div>
        <ErrorNote error={error} />

        <Card title="Receiving hospital">
          <div style={{ fontSize: 18, fontWeight: 700 }}>{hospital.name}</div>
          <div className="muted" style={{ fontSize: 12 }}>
            {hospital.code} · {hospital.city}
          </div>
          <div style={{ marginTop: 8 }}>
            {hospital.is_on_diversion ? (
              <Badge tone="bad">ON DIVERSION — excluded from recommendations</Badge>
            ) : (
              <Badge tone="ok">Accepting emergency arrivals</Badge>
            )}
          </div>
        </Card>

        <div className="stat-row">
          <Stat value={inbound.length} label="Inbound" />
          <Stat value={capacity.emergency_beds_available ?? "-"} label="ED beds free" />
          <Stat value={capacity.icu_beds_available ?? "-"} label="ICU beds free" />
          <Stat
            value={`${Math.round((capacity.workload_index ?? 0) * 100)}%`}
            label="Workload"
          />
        </div>

        <Card title="Inbound patients">
          {inbound.length === 0 ? (
            <Empty>No inbound emergency vehicles.</Empty>
          ) : (
            [...inbound]
              .sort((a, b) => new Date(a.eta ?? 0).getTime() - new Date(b.eta ?? 0).getTime())
              .map((trip) => (
                <div key={trip.id} className={`trip ${levelClass(trip.priority_level)}`}>
                  <div className="head">
                    <span className="ref">{trip.vehicle_callsign}</span>
                    <Badge tone={levelClass(trip.priority_level) as "l1"}>
                      L{trip.priority_level}
                    </Badge>
                  </div>
                  <div className="meta">
                    <b>{trip.category_display}</b>
                    {trip.patient_age ? ` · age ${trip.patient_age}` : ""}
                  </div>
                  {/* What the crew observed. This is what lets the receiving
                      team call the trauma bay and cross-match blood before
                      the doors open, where a category of "Undetermined" -
                      which an unsure crew correctly selects - says nothing. */}
                  {trip.symptom_labels && trip.symptom_labels.length > 0 && (
                    <div className="symptom-chips">
                      {trip.symptom_labels.map((label) => (
                        <span key={label} className="symptom-chip">
                          {label}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="meta">{trip.stage_display}</div>
                  <div className="meta">
                    <span className="eta">ETA {fmtEta(trip.eta)}</span> ·{" "}
                    {fmtDistance(trip.distance_remaining_m)} out
                    {trip.patient_deteriorating && (
                      <> · <Badge tone="bad">deteriorating</Badge></>
                    )}
                  </div>
                  {trip.patient_notes && <div className="meta muted">{trip.patient_notes}</div>}
                  {trip.clinical_data_redacted && <RedactionNote />}
                </div>
              ))
          )}
        </Card>

        <Card title="Update live capacity">
          <div className="grid-2" style={{ gap: 8 }}>
            <div>
              <label htmlFor="ed">ED beds available</label>
              <input id="ed" type="number" min={0} {...field("emergency_beds_available")} />
            </div>
            <div>
              <label htmlFor="icu">ICU beds available</label>
              <input id="icu" type="number" min={0} {...field("icu_beds_available")} />
            </div>
            <div>
              <label htmlFor="waiting">Patients waiting</label>
              <input id="waiting" type="number" min={0} {...field("patients_waiting")} />
            </div>
            <div>
              <label htmlFor="doctors">Doctors on duty</label>
              <input id="doctors" type="number" min={0} {...field("doctors_on_duty")} />
            </div>
          </div>
          <div className="btn-row">
            <button type="button" disabled={!canWrite} onClick={() => void publishCapacity()}>
              Publish capacity
            </button>
            <button type="button" className="ghost danger" disabled={!canWrite}
              onClick={() => void toggleDiversion()}>
              Toggle diversion
            </button>
          </div>
          <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
            {canWrite
              ? "Capacity feeds the recommendation engine directly — stale figures are down-weighted automatically."
              : "Read-only: publishing capacity requires the hospital role."}
          </div>
        </Card>
      </aside>
    </div>
  );
}
