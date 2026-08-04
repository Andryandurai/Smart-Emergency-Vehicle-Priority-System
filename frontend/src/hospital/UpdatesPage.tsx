/**
 * Tab 3 — Updates.
 *
 * The one place the board's figures change. Everything here is a draft until
 * "Save" is pressed: a ward clerk correcting four numbers should not publish
 * three wrong intermediate states to a recommender that is routing patients on
 * them.
 *
 * Capacity and team readiness save together for the same reason. They are read
 * together on the board, and a partial save would leave it describing two
 * different moments — beds from now, teams from an hour ago.
 */
import { useCallback, useEffect, useState } from "react";
import { useOutletContext } from "react-router-dom";

import { ApiError } from "@/api/client";
import { hospitalPortal } from "@/api/endpoints";
import type { HospitalEditableCapacity, TeamRow } from "@/api/types";
import type { HospitalOutletContext } from "@/hospital/HospitalShell";

/** The headline figures, in the order the dashboard shows them. */
const HEADLINE: { field: keyof HospitalEditableCapacity; label: string }[] = [
  { field: "emergency_cases_today", label: "Emergency Cases Today" },
  { field: "emergency_beds_available", label: "Available Beds" },
  { field: "icu_beds_available", label: "Available ICU Beds" },
  { field: "ventilators_available", label: "Available Ventilators" },
  { field: "operation_theatres_free", label: "Available Operation Theatres" },
  { field: "emergency_staff_on_duty", label: "Emergency Staff On Duty" },
];

/** The ward table: each row is an available/total pair. */
const WARDS: {
  label: string;
  available: keyof HospitalEditableCapacity;
  total: keyof HospitalEditableCapacity;
}[] = [
  { label: "General Beds", available: "general_beds_available", total: "general_beds_total" },
  { label: "ICU Beds", available: "icu_beds_available", total: "icu_beds_total" },
  { label: "Emergency Beds", available: "emergency_beds_available", total: "emergency_beds_total" },
  { label: "Pediatric Beds", available: "pediatric_beds_available", total: "pediatric_beds_total" },
  { label: "Burn Unit", available: "burn_unit_beds_available", total: "burn_unit_beds_total" },
  { label: "Cardiac ICU", available: "cardiac_icu_available", total: "cardiac_icu_total" },
  { label: "Ventilators", available: "ventilators_available", total: "ventilators_total" },
];

/** Floor figures that are not beds. */
const FLOOR: { field: keyof HospitalEditableCapacity; label: string }[] = [
  { field: "operation_theatres_free", label: "Operation theatres free" },
  { field: "operation_theatres_total", label: "Operation theatres total" },
  { field: "doctors_on_duty", label: "Doctors on duty" },
  { field: "patients_waiting", label: "Patients waiting" },
];

export function UpdatesPage() {
  const { code, refreshBoard } = useOutletContext<HospitalOutletContext>();
  const [capacity, setCapacity] = useState<HospitalEditableCapacity | null>(null);
  const [teams, setTeams] = useState<Record<string, boolean>>({});
  const [rows, setRows] = useState<TeamRow[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    if (!code) return;
    try {
      const form = await hospitalPortal.updateForm(code);
      setCapacity(form.capacity);
      setTeams(form.teams);
      setRows(form.team_rows);
      setDirty(false);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the current figures.");
    }
  }, [code]);

  useEffect(() => {
    void load();
  }, [load]);

  const setNumber = (field: keyof HospitalEditableCapacity, raw: string) => {
    const value = Math.max(0, Math.floor(Number(raw) || 0));
    setCapacity((current) => (current ? { ...current, [field]: value } : current));
    setDirty(true);
    setSaved(false);
  };

  const toggleTeam = (field: string) => {
    setTeams((current) => ({ ...current, [field]: !current[field] }));
    setDirty(true);
    setSaved(false);
  };

  const save = async () => {
    if (!capacity) return;
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      const result = await hospitalPortal.save({
        hospital: code ?? undefined,
        capacity,
        teams,
      });
      setCapacity(result.capacity);
      setTeams(result.teams);
      setRows(result.team_rows);
      setDirty(false);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
      // The header pill and the Dashboard tab read the same figures.
      await refreshBoard();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        const errors: Record<string, string> = {};
        for (const [field, messages] of Object.entries(err.fieldErrors)) {
          errors[field] = Array.isArray(messages) ? messages.join(" ") : String(messages);
        }
        setFieldErrors(errors);
        setError("Some figures were rejected — see the fields marked below.");
      } else {
        setError(err instanceof Error ? err.message : "Could not save.");
      }
    } finally {
      setBusy(false);
    }
  };

  if (!capacity) {
    return (
      <div className="hp-page">
        {error ? <div className="hp-error">{error}</div> : <div className="hp-loading">Loading…</div>}
      </div>
    );
  }

  return (
    <div className="hp-page">
      <h1 className="hp-h1">
        Updates
        {dirty && <span className="hp-count unsaved">Unsaved changes</span>}
      </h1>
      <p className="hp-lead">
        These figures decide where SEVPS sends the next patient. Nothing is published
        until you save.
      </p>

      {error && <div className="hp-error">{error}</div>}

      <section className="hp-card">
        <h2>Dashboard figures</h2>
        <div className="hp-edit-grid">
          {HEADLINE.map((item) => (
            <NumberField
              key={item.field}
              label={item.label}
              value={capacity[item.field]}
              error={fieldErrors[item.field]}
              onChange={(raw) => setNumber(item.field, raw)}
            />
          ))}
        </div>
      </section>

      <section className="hp-card">
        <h2>Beds / Ventilators</h2>
        <table className="hp-table edit">
          <thead>
            <tr>
              <th>Resource</th>
              <th>Available</th>
              <th>Total</th>
            </tr>
          </thead>
          <tbody>
            {WARDS.map((ward) => (
              <tr key={ward.label}>
                <td>{ward.label}</td>
                <td>
                  <input
                    type="number"
                    min={0}
                    value={capacity[ward.available]}
                    aria-label={`${ward.label} available`}
                    className={fieldErrors[ward.available] ? "bad" : ""}
                    onChange={(event) => setNumber(ward.available, event.target.value)}
                  />
                </td>
                <td>
                  <input
                    type="number"
                    min={0}
                    value={capacity[ward.total]}
                    aria-label={`${ward.label} total`}
                    onChange={(event) => setNumber(ward.total, event.target.value)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {WARDS.some((ward) => fieldErrors[ward.available]) && (
          <div className="hp-warn small">
            {WARDS.filter((ward) => fieldErrors[ward.available])
              .map((ward) => fieldErrors[ward.available])
              .join(" ")}
          </div>
        )}
      </section>

      <section className="hp-card">
        <h2>Emergency floor</h2>
        <div className="hp-edit-grid">
          {FLOOR.map((item) => (
            <NumberField
              key={item.field}
              label={item.label}
              value={capacity[item.field]}
              error={fieldErrors[item.field]}
              onChange={(raw) => setNumber(item.field, raw)}
            />
          ))}
        </div>
      </section>

      <section className="hp-card">
        <h2>Hospital Team Readiness</h2>
        <p className="hp-lead">
          A team marked not ready still appears on the board — crews need to know what
          cannot be fielded, not just what can.
        </p>
        <div className="hp-team-edit">
          {rows.map((row) => (
            <button
              key={row.field}
              type="button"
              className={`hp-toggle${teams[row.field] ? " on" : ""}`}
              aria-pressed={Boolean(teams[row.field])}
              onClick={() => toggleTeam(row.field)}
            >
              <span className="hp-toggle-track" aria-hidden>
                <span className="hp-toggle-knob" />
              </span>
              <span className="hp-toggle-label">{row.label}</span>
              <span className="hp-toggle-state">{teams[row.field] ? "Ready" : "Not ready"}</span>
            </button>
          ))}
        </div>
      </section>

      <div className="hp-save-bar">
        <button
          type="button"
          className="hp-btn primary"
          disabled={busy || !dirty}
          onClick={() => void save()}
        >
          {busy ? "Saving…" : saved ? "✓ Saved" : "Save updates"}
        </button>
        <button
          type="button"
          className="hp-btn ghost"
          disabled={busy || !dirty}
          onClick={() => void load()}
        >
          Discard changes
        </button>
      </div>
    </div>
  );
}

function NumberField({
  label,
  value,
  error,
  onChange,
}: {
  label: string;
  value: number;
  error?: string;
  onChange: (raw: string) => void;
}) {
  return (
    <label className="hp-field">
      <span>{label}</span>
      <input
        type="number"
        min={0}
        value={value}
        className={error ? "bad" : ""}
        onChange={(event) => onChange(event.target.value)}
      />
      {error && <em className="hp-field-error">{error}</em>}
    </label>
  );
}
