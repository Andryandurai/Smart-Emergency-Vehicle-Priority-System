/**
 * Tab 3 — Hospitals.
 *
 * Two jobs on one screen. The first is reference: what can each hospital
 * take, and how full is it right now. The second is the decision a driver is
 * sometimes asked to make on the spot, when a patient or their family asks to
 * be taken somewhere in particular.
 *
 * Changing the destination is deliberately not a one-tap action. The reason
 * is asked for first, from a fixed list rather than a free-text box, because
 * the distinction the record has to preserve is *why*: a patient exercising
 * their right to choose and a crew overruling the recommender are different
 * events, and review must not have to guess which one happened. The list is
 * `HospitalChoiceReason` from the backend enum, so the two cannot drift.
 *
 * Capacity is read-only here. A driver is not the person who knows whether a
 * bed is genuinely free — that is published by the hospital and corrected by
 * administrators.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useOutletContext } from "react-router-dom";

import { ApiError } from "@/api/client";
import { dispatch as dispatchApi, hospitals as hospitalApi } from "@/api/endpoints";
import type { Hospital, HospitalChoiceReason, Trip } from "@/api/types";
import { fmtTime } from "@/components/ui";
import { DpError } from "@/driver/TakeoverPage";
import type { DriverOutletContext } from "@/driver/DriverShell";

/**
 * Why the destination is changing.
 *
 * `recommended` is absent: it is what the engine chose, so it cannot also be
 * the reason for overriding it.
 */
const CHANGE_REASONS: { value: HospitalChoiceReason; label: string; hint: string }[] = [
  {
    value: "patient_request",
    label: "Patient asked for this hospital",
    hint: "The patient named it themselves",
  },
  {
    value: "family_request",
    label: "Family asked for this hospital",
    hint: "Relatives travelling with or meeting the patient",
  },
  {
    value: "clinical_judgement",
    label: "Crew clinical judgement",
    hint: "The crew consider it the better receiving unit",
  },
  {
    value: "capacity",
    label: "Capacity or diversion",
    hint: "The recommended hospital cannot take this patient",
  },
  {
    value: "continuity",
    label: "Already under care there",
    hint: "Existing notes, consultant or ongoing treatment",
  },
];

export function HospitalsPage() {
  const { shift } = useOutletContext<DriverOutletContext>();
  const [list, setList] = useState<Hospital[]>([]);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [changing, setChanging] = useState<Hospital | null>(null);
  const [error, setError] = useState<string | null>(null);

  const callsign = shift?.vehicle_callsign ?? "";

  const reloadTrip = useCallback(async () => {
    if (!callsign) {
      setTrip(null);
      return;
    }
    try {
      const trips = await dispatchApi.tripsForVehicle(callsign);
      setTrip(trips[0] ?? null);
    } catch {
      setTrip(null);
    }
  }, [callsign]);

  useEffect(() => {
    hospitalApi
      .list()
      .then(setList)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load hospitals."));
  }, []);

  useEffect(() => {
    void reloadTrip();
  }, [reloadTrip]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter(
      (hospital) =>
        hospital.name.toLowerCase().includes(q) ||
        hospital.code.toLowerCase().includes(q) ||
        hospital.city.toLowerCase().includes(q) ||
        hospital.facility_codes.some((code) => code.includes(q.replace(/\s+/g, "_"))),
    );
  }, [list, query]);

  return (
    <div className="dp-page">
      <h2 className="dp-h2">Hospitals</h2>
      <p className="dp-lead">
        {trip?.hospital_name
          ? `You are currently routed to ${trip.hospital_name}.`
          : trip
            ? "No hospital has been assigned to this emergency yet."
            : "No emergency is running, so there is nothing to reroute."}
      </p>

      <DpError error={error} />

      <input
        className="dp-search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search by name, code, city or capability"
      />

      {filtered.map((hospital) => {
        const capacity = hospital.capacity;
        const readiness = capacity ? Math.round((1 - capacity.workload_index) * 100) : null;
        const open = expanded === hospital.id;
        const current = trip?.hospital_code === hospital.code;

        return (
          <section
            key={hospital.id}
            className={`dp-hosp${hospital.is_on_diversion ? " diverted" : ""}${current ? " current" : ""}`}
          >
            <button
              type="button"
              className="dp-hosp-head"
              onClick={() => setExpanded(open ? null : hospital.id)}
            >
              <span className="dp-hosp-id">
                <span className="dp-hosp-name">{hospital.name}</span>
                <span className="dp-note">
                  {hospital.code} · {hospital.city}
                  {hospital.is_trauma_designated ? " · Trauma centre" : ""}
                </span>
              </span>
              <span className="dp-hosp-flags">
                {current && <span className="dp-chip ok">Current destination</span>}
                <span className={`dp-chip ${hospital.is_on_diversion ? "bad" : "ok"}`}>
                  {hospital.is_on_diversion ? "On diversion" : "Accepting"}
                </span>
              </span>
            </button>

            {/* Offered on the row itself, not only inside the expanded detail.
                A patient asking to be taken somewhere else does so while the
                ambulance is moving, and a driver should not have to open a
                card to discover that changing the destination is possible. */}
            {trip && !current && (
              <div className="dp-hosp-choose">
                <button
                  type="button"
                  className="dp-btn primary small"
                  onClick={() => setChanging(hospital)}
                >
                  Take patient here instead
                </button>
              </div>
            )}

            {capacity && (
              <div className="dp-cap">
                <Figure value={capacity.emergency_beds_available} label="ED beds" />
                <Figure value={capacity.icu_beds_available} label="ICU beds" />
                <Figure value={capacity.ventilators_available} label="Ventilators" />
                <Figure value={capacity.operation_theatres_free} label="Theatres" />
                <Figure
                  value={readiness === null ? "—" : `${readiness}%`}
                  label="Readiness"
                  tone={readinessTone(readiness)}
                />
              </div>
            )}

            {open && (
              <div className="dp-hosp-detail">
                {hospital.is_on_diversion && hospital.diversion_reason && (
                  <div className="dp-critical warn">Diversion: {hospital.diversion_reason}</div>
                )}
                <div className="dp-kv">
                  <span>Emergency phone</span>
                  <b>{hospital.emergency_phone || "—"}</b>
                </div>
                {capacity && (
                  <>
                    <div className="dp-kv">
                      <span>ED beds (free / total)</span>
                      <b>
                        {capacity.emergency_beds_available} / {capacity.emergency_beds_total}
                      </b>
                    </div>
                    <div className="dp-kv">
                      <span>ICU beds (free / total)</span>
                      <b>
                        {capacity.icu_beds_available} / {capacity.icu_beds_total}
                      </b>
                    </div>
                    <div className="dp-kv">
                      <span>Patients waiting</span>
                      <b>{capacity.patients_waiting}</b>
                    </div>
                    <div className="dp-kv">
                      <span>Doctors on duty</span>
                      <b>{capacity.doctors_on_duty}</b>
                    </div>
                    <div className="dp-kv">
                      <span>Capacity reported</span>
                      <b>
                        {fmtTime(capacity.reported_at)}
                        {capacity.is_stale ? " (stale)" : ""}
                      </b>
                    </div>
                  </>
                )}
                <div className="dp-facilities">
                  {hospital.facility_codes.map((code) => (
                    <span key={code} className="dp-facility">
                      {code.replaceAll("_", " ")}
                    </span>
                  ))}
                </div>

                {!trip && (
                  <p className="dp-note">
                    Start an emergency before changing a destination.
                  </p>
                )}
              </div>
            )}
          </section>
        );
      })}

      {filtered.length === 0 && !error && (
        <div className="dp-empty">No hospital matches that search.</div>
      )}

      {changing && trip && (
        <ChangeDestination
          hospital={changing}
          trip={trip}
          onClose={() => setChanging(null)}
          onChanged={async () => {
            setChanging(null);
            await reloadTrip();
          }}
        />
      )}
    </div>
  );
}

/**
 * The reason prompt.
 *
 * Blocking, and the confirm button stays disabled until a reason is chosen —
 * the whole value of the question is lost if it can be dismissed. Confirming
 * re-runs the assessment with the new hospital, which is what re-plans the
 * route and re-arms the green corridor; the driver's map then follows the new
 * road without them doing anything else.
 */
function ChangeDestination({
  hospital,
  trip,
  onClose,
  onChanged,
}: {
  hospital: Hospital;
  trip: Trip;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [reason, setReason] = useState<HospitalChoiceReason | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    if (!reason) return;
    setBusy(true);
    setError(null);
    try {
      const chosen = CHANGE_REASONS.find((item) => item.value === reason);
      await dispatchApi.assess(trip.id, {
        // Unchanged - this is a destination change, not a re-triage. Sending
        // the trip's own category keeps the clinical record exactly as the
        // paramedic left it.
        emergency_category: trip.emergency_category,
        ...(trip.symptoms?.length ? { symptoms: trip.symptoms } : {}),
        hospital_id: hospital.id,
        choice_reason: reason,
        // The server requires a reason string whenever a hospital is named.
        override_reason: note.trim() || (chosen?.label ?? "Destination changed by driver"),
      });
      await dispatchApi.reroute(trip.id, "destination changed by driver");
      await onChanged();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not change the destination.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dp-modal-wrap" role="dialog" aria-modal="true">
      <div className="dp-modal">
        <h3>Why are you changing hospital?</h3>
        <p className="dp-lead">
          Moving from <b>{trip.hospital_name ?? "no hospital"}</b> to{" "}
          <b>{hospital.name}</b>. The reason stays on the patient record.
        </p>

        <DpError error={error} />

        <div className="dp-reasons">
          {CHANGE_REASONS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`dp-reason${reason === item.value ? " chosen" : ""}`}
              onClick={() => setReason(item.value)}
            >
              <span className="dp-reason-label">{item.label}</span>
              <span className="dp-note">{item.hint}</span>
            </button>
          ))}
        </div>

        <label htmlFor="dpChangeNote">Anything to add? (optional)</label>
        <input
          id="dpChangeNote"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Free text for the record"
        />

        {hospital.is_on_diversion && (
          <div className="dp-critical warn">
            {hospital.name} is on diversion. Only take the patient there if you have
            agreed it with them directly.
          </div>
        )}

        <div className="dp-actions">
          <button
            type="button"
            className="dp-btn primary"
            disabled={!reason || busy}
            onClick={() => void confirm()}
          >
            {busy ? "Rerouting…" : "Confirm and reroute"}
          </button>
          <button type="button" className="dp-btn ghost" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

function Figure({
  value,
  label,
  tone = "",
}: {
  value: number | string;
  label: string;
  tone?: string;
}) {
  return (
    <div className="dp-figure">
      <b className={tone}>{value}</b>
      <span>{label}</span>
    </div>
  );
}

function readinessTone(readiness: number | null): string {
  if (readiness === null) return "";
  if (readiness >= 60) return "ok";
  if (readiness >= 35) return "warn";
  return "bad";
}
