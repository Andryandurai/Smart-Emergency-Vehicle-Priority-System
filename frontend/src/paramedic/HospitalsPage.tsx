/**
 * Hospitals — read only.
 *
 * A paramedic needs to know what a hospital can take and how full it is. They
 * are not the ones who know whether a bed is genuinely free, so nothing here
 * is editable: capacity and diversion are published by the hospital itself
 * and corrected by administrators. Rendering them as plain figures rather
 * than as disabled inputs is deliberate - a greyed-out field invites someone
 * to go looking for the permission to use it.
 */
import { useEffect, useMemo, useState } from "react";

import { hospitals as hospitalApi } from "@/api/endpoints";
import type { Hospital } from "@/api/types";
import { fmtTime } from "@/components/ui";

export function HospitalsPage() {
  const [list, setList] = useState<Hospital[]>([]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    hospitalApi
      .list()
      .then(setList)
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Could not load hospitals."),
      );
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter(
      (h) =>
        h.name.toLowerCase().includes(q) ||
        h.code.toLowerCase().includes(q) ||
        h.facility_codes.some((f) => f.includes(q.replace(/\s+/g, "_"))),
    );
  }, [list, query]);

  return (
    <div className="pm-page">
      <h2 className="pm-h2">Hospitals</h2>
      <p className="pm-note">View only. Capacity is published by each hospital.</p>

      <input
        className="pm-search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search name, code or capability"
      />

      {error && <div className="pm-error">{error}</div>}

      {filtered.map((hospital) => {
        const capacity = hospital.capacity;
        const readiness = capacity
          ? Math.round((1 - capacity.workload_index) * 100)
          : null;
        const expanded = open === hospital.id;
        return (
          <section
            key={hospital.id}
            className={`pm-card hospital${hospital.is_on_diversion ? " diverted" : ""}`}
          >
            <button
              type="button"
              className="pm-hospital-toggle"
              onClick={() => setOpen(expanded ? null : hospital.id)}
            >
              <div>
                <div className="pm-hospital-name">{hospital.name}</div>
                <div className="pm-hospital-sub">
                  {hospital.code} · {hospital.city}
                  {hospital.is_trauma_designated ? " · Trauma centre" : ""}
                </div>
              </div>
              <span className={`pm-pill ${hospital.is_on_diversion ? "bad" : "ok"}`}>
                {hospital.is_on_diversion ? "On diversion" : "Accepting"}
              </span>
            </button>

            {capacity && (
              <div className="pm-cap-row">
                <div>
                  <b>{capacity.emergency_beds_available}</b>
                  <span>ED beds</span>
                </div>
                <div>
                  <b>{capacity.icu_beds_available}</b>
                  <span>ICU beds</span>
                </div>
                <div>
                  <b>{capacity.ventilators_available}</b>
                  <span>Ventilators</span>
                </div>
                <div>
                  <b className={readinessTone(readiness)}>{readiness ?? "—"}%</b>
                  <span>Ready</span>
                </div>
              </div>
            )}

            {expanded && (
              <div className="pm-hospital-detail">
                {hospital.is_on_diversion && hospital.diversion_reason && (
                  <div className="pm-warn">Diversion: {hospital.diversion_reason}</div>
                )}
                <div className="pm-kv">
                  <span>Emergency phone</span>
                  <b>{hospital.emergency_phone || "—"}</b>
                </div>
                {capacity && (
                  <>
                    <div className="pm-kv">
                      <span>Theatres free</span>
                      <b>{capacity.operation_theatres_free}</b>
                    </div>
                    <div className="pm-kv">
                      <span>Patients waiting</span>
                      <b>{capacity.patients_waiting}</b>
                    </div>
                    <div className="pm-kv">
                      <span>Doctors on duty</span>
                      <b>{capacity.doctors_on_duty}</b>
                    </div>
                    <div className="pm-kv">
                      <span>Capacity reported</span>
                      <b>
                        {fmtTime(capacity.reported_at)}
                        {capacity.is_stale ? " (stale)" : ""}
                      </b>
                    </div>
                  </>
                )}
                <div className="pm-facilities">
                  {hospital.facility_codes.map((code) => (
                    <span key={code} className="pm-facility">
                      {code.replaceAll("_", " ")}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </section>
        );
      })}

      {filtered.length === 0 && !error && (
        <div className="pm-card">No hospital matches that search.</div>
      )}
    </div>
  );
}

function readinessTone(readiness: number | null): string {
  if (readiness === null) return "";
  if (readiness >= 60) return "ok";
  if (readiness >= 35) return "warn";
  return "bad";
}
