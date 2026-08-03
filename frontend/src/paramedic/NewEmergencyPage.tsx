/**
 * New emergency — the paramedic's clinical workflow.
 *
 * Four steps, one screen at a time, because a crew working a patient cannot
 * hold a form in their head: what is wrong, what can you see, where are we
 * taking them, confirm. Submitting the last step is what starts the
 * transport, plans the optimised route and arms the green corridor - and the
 * driver's navigation console picks it up over the socket without anyone
 * telling it to.
 *
 * The re-route reasons are buttons rather than a text box. A crew asked to
 * type a justification one-handed in a moving ambulance types "other" - so
 * the common answers are one tap, and free text exists only behind "Other
 * reason", where it is genuinely the only option left.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError } from "@/api/client";
import { dispatch as dispatchApi, hospitals as hospitalApi, shifts as shiftApi } from "@/api/endpoints";
import type {
  CrewShift,
  EmergencyRuleSummary,
  HospitalCandidate,
  HospitalChoiceReason,
  Recommendation,
  SymptomCode,
  SymptomSpec,
  Trip,
} from "@/api/types";
import { fmtDistance, fmtEta } from "@/components/ui";

const UNDETERMINED = "unknown";

/** Tap-only re-route reasons. "Other" is the single typed escape hatch. */
const REROUTE_REASONS: { value: HospitalChoiceReason; label: string; hint: string }[] = [
  {
    value: "patient_request",
    label: "Patient's request",
    hint: "The patient asked for this hospital. This must be honoured.",
  },
  {
    value: "family_request",
    label: "Family's request",
    hint: "Family present asked for this hospital.",
  },
  {
    value: "continuity",
    label: "Already under care there",
    hint: "Patient's records and treating team are at this hospital.",
  },
  {
    value: "capacity",
    label: "Capacity or diversion",
    hint: "The recommended hospital cannot take this patient now.",
  },
  {
    value: "clinical_judgement",
    label: "Clinical judgement",
    hint: "Crew assessment differs from the rule engine.",
  },
];

type Step = "category" | "observe" | "hospital" | "done";

export function NewEmergencyPage() {
  const [shift, setShift] = useState<CrewShift | null>(null);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [step, setStep] = useState<Step>("category");

  const [categories, setCategories] = useState<EmergencyRuleSummary[]>([]);
  const [symptomCatalogue, setSymptomCatalogue] = useState<SymptomSpec[]>([]);

  const [category, setCategory] = useState<string | null>(null);
  const [symptoms, setSymptoms] = useState<SymptomCode[]>([]);
  const [age, setAge] = useState("");
  const [notes, setNotes] = useState("");
  const [deteriorating, setDeteriorating] = useState(false);

  const [recommendation, setRecommendation] = useState<Recommendation | null>(null);
  const [chosen, setChosen] = useState<HospitalCandidate | null>(null);
  const [reason, setReason] = useState<HospitalChoiceReason | null>(null);
  const [otherReason, setOtherReason] = useState("");

  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const mine = await shiftApi.mine();
      setShift(mine.shift);
      if (mine.shift?.status === "active") {
        const active = await dispatchApi.tripsForVehicle(mine.shift.vehicle_callsign);
        setTrip(active[0] ?? null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your shift.");
    }
  }, []);

  useEffect(() => {
    void load();
    hospitalApi.ruleCatalogue().then(setCategories).catch(() => setCategories([]));
    hospitalApi
      .symptomCatalogue()
      .then((r) => setSymptomCatalogue(r.symptoms))
      .catch(() => setSymptomCatalogue([]));
  }, [load]);

  const recommended = recommendation?.recommended ?? null;
  const isReroute = Boolean(chosen && recommended && chosen.hospital_id !== recommended.hospital_id);

  const eligible = useMemo(
    () => recommendation?.candidates.filter((c) => c.eligible) ?? [],
    [recommendation],
  );
  const excluded = useMemo(
    () => recommendation?.candidates.filter((c) => !c.eligible) ?? [],
    [recommendation],
  );

  // --- guards -------------------------------------------------------------
  if (!shift || shift.status !== "active") {
    return (
      <div className="pm-page">
        <div className="pm-hero idle">
          <span className="pm-hero-tag">No shift</span>
          <h1>Start your shift first</h1>
          <p>You need an ambulance and a driver before you can record an emergency.</p>
        </div>
      </div>
    );
  }

  /**
   * An emergency already under way blocks a new one.
   *
   * Not a nag: two live trips on one ambulance means two ETAs and two
   * hospitals expecting the same patient. The only ways out are the two real
   * ones - hand the patient over, or cancel.
   */
  const transportUnderWay =
    step !== "done" &&
    trip !== null &&
    Boolean(trip.hospital_name) &&
    ["to_hospital", "arrived"].includes(trip.stage);

  if (transportUnderWay && trip) {
    return (
      <div className="pm-page">
        <div className="pm-hero request">
          <span className="pm-hero-tag">Emergency in progress</span>
          <h1>{trip.hospital_name}</h1>
          <p>{trip.reference} · {trip.stage_display}</p>
        </div>

        <div className="pm-info">
          You cannot start another emergency while this one is running. Complete the
          handover at the hospital, or cancel this response first.
        </div>

        <section className="pm-card">
          <div className="pm-kv">
            <span>Emergency</span>
            <b>{trip.category_display}</b>
          </div>
          <div className="pm-kv">
            <span>ETA</span>
            <b>{fmtEta(trip.eta)}</b>
          </div>
          <div className="pm-kv">
            <span>Distance</span>
            <b>{fmtDistance(trip.distance_remaining_m)}</b>
          </div>
        </section>

        <ErrorNoteInline error={error} />

        <button
          type="button"
          className="pm-btn primary"
          disabled={busy !== null}
          onClick={() => void completeHandover(trip.id)}
        >
          {busy === "handover" ? "Completing…" : "Patient handed over"}
        </button>
        <button
          type="button"
          className="pm-btn danger"
          disabled={busy !== null}
          onClick={() => void cancelTrip(trip.id)}
        >
          {busy === "cancel" ? "Cancelling…" : "Cancel this emergency"}
        </button>
      </div>
    );
  }

  // --- actions ------------------------------------------------------------
  const completeHandover = async (tripId: number) => {
    setBusy("handover");
    setError(null);
    try {
      await dispatchApi.handover(tripId);
      setTrip(null);
      reset();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not complete the handover.");
    } finally {
      setBusy(null);
    }
  };

  const cancelTrip = async (tripId: number) => {
    setBusy("cancel");
    setError(null);
    try {
      await dispatchApi.cancel(tripId, "Cancelled by crew");
      setTrip(null);
      reset();
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 403
          ? "Only an administrator can cancel a response. Complete the handover instead."
          : err instanceof Error
            ? err.message
            : "Could not cancel.",
      );
    } finally {
      setBusy(null);
    }
  };

  const findHospitals = async () => {
    if (!category) return;
    if (category === UNDETERMINED && symptoms.length === 0) {
      setError("Tick at least one thing you can see — that is what the hospital match uses.");
      return;
    }
    setBusy("find");
    setError(null);
    try {
      const at: [number, number] = [
        trip?.vehicle_latitude ?? 13.0604,
        trip?.vehicle_longitude ?? 80.2496,
      ];
      const result = await hospitalApi.recommend(at[0], at[1], category, symptoms);
      setRecommendation(result);
      setChosen(result.recommended);
      setReason(null);
      setOtherReason("");
      setStep("hospital");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not find a hospital.");
    } finally {
      setBusy(null);
    }
  };

  const submit = async () => {
    if (!shift || !category || !chosen) return;
    if (isReroute && !reason) {
      setError("Choose why you are going somewhere other than the recommended hospital.");
      return;
    }
    if (isReroute && reason === "clinical_judgement" && !otherReason.trim()) {
      setError("Add a short note for the clinical judgement.");
      return;
    }

    setBusy("submit");
    setError(null);
    try {
      // A crew flagged down at the roadside has a patient and no dispatch
      // record. Open one against this shift's own ambulance rather than
      // leaving the button pressed with nothing happening - which is what it
      // used to do, and is the worst possible response to "start transport".
      const active = trip ?? (await shiftApi.newEmergency(shift.id));
      setTrip(active);

      const body: Parameters<typeof dispatchApi.assess>[1] = {
        emergency_category: category,
        symptoms,
        patient_notes: notes,
        patient_deteriorating: deteriorating,
      };
      if (age) body.patient_age = Number(age);
      if (isReroute) {
        body.hospital_id = chosen.hospital_id;
        body.choice_reason = reason!;
        body.override_reason =
          otherReason.trim() ||
          REROUTE_REASONS.find((r) => r.value === reason)?.label ||
          "Crew choice";
      }
      const result = await dispatchApi.assess(active.id, body);
      setTrip(result.trip);
      setStep("done");
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 403
          ? "Your role cannot confirm an assessment."
          : err instanceof Error
            ? err.message
            : "Could not start the transport.",
      );
    } finally {
      setBusy(null);
    }
  };

  const reset = () => {
    setStep("category");
    setCategory(null);
    setSymptoms([]);
    setAge("");
    setNotes("");
    setDeteriorating(false);
    setRecommendation(null);
    setChosen(null);
    setReason(null);
    setOtherReason("");
    void load();
  };

  // --- render -------------------------------------------------------------
  return (
    <div className="pm-page">
      <Steps current={step} />
      {error && <div className="pm-error">{error}</div>}

      {step === "category" && (
        <>
          <h2 className="pm-h2">What is the emergency?</h2>
          <div className="pm-choice-grid">
            {categories
              .filter((c) => c.category !== UNDETERMINED)
              .map((c) => (
                <button
                  key={c.category}
                  type="button"
                  className={`pm-choice${category === c.category ? " selected" : ""}`}
                  data-level={c.default_priority_level}
                  onClick={() => setCategory(c.category)}
                >
                  <span className="pm-choice-label">{c.display_name}</span>
                  <span className="pm-choice-sub">Level {c.default_priority_level}</span>
                </button>
              ))}
            <button
              type="button"
              className={`pm-choice other${category === UNDETERMINED ? " selected" : ""}`}
              onClick={() => setCategory(UNDETERMINED)}
            >
              <span className="pm-choice-label">Not sure</span>
              <span className="pm-choice-sub">Record what you can see instead</span>
            </button>
          </div>
          <button
            type="button"
            className="pm-btn primary"
            disabled={!category}
            onClick={() => setStep("observe")}
          >
            Next — patient assessment
          </button>
        </>
      )}

      {step === "observe" && (
        <>
          <h2 className="pm-h2">Patient assessment</h2>

          <section className="pm-card">
            <label htmlFor="age">Patient age</label>
            <input
              id="age"
              type="number"
              inputMode="numeric"
              min={0}
              max={130}
              value={age}
              onChange={(e) => setAge(e.target.value)}
              placeholder="Optional"
            />
            <label htmlFor="notes">Clinical notes</label>
            <textarea
              id="notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Optional"
            />
            <button
              type="button"
              className={`pm-toggle${deteriorating ? " on" : ""}`}
              onClick={() => setDeteriorating((v) => !v)}
            >
              {deteriorating ? "✓ " : ""}Patient is deteriorating
            </button>
          </section>

          <h2 className="pm-h2">
            What can you see?
            {category === UNDETERMINED && <span className="pm-required">required</span>}
          </h2>
          <div className="pm-symptom-grid">
            {symptomCatalogue.map((s) => {
              const on = symptoms.includes(s.code);
              return (
                <button
                  key={s.code}
                  type="button"
                  className={`pm-symptom${on ? " selected" : ""}`}
                  onClick={() =>
                    setSymptoms((cur) =>
                      cur.includes(s.code) ? cur.filter((x) => x !== s.code) : [...cur, s.code],
                    )
                  }
                >
                  {on ? "✓ " : ""}
                  {s.label}
                </button>
              );
            })}
          </div>

          <button
            type="button"
            className="pm-btn primary"
            disabled={busy !== null}
            onClick={() => void findHospitals()}
          >
            {busy === "find" ? "Finding hospitals…" : "Find a hospital"}
          </button>
          <button type="button" className="pm-btn ghost" onClick={() => setStep("category")}>
            Back
          </button>
        </>
      )}

      {step === "hospital" && recommendation && (
        <>
          <h2 className="pm-h2">Where are we taking them?</h2>

          {eligible.map((candidate, index) => (
            <HospitalOption
              key={candidate.hospital_id}
              candidate={candidate}
              best={index === 0}
              selected={chosen?.hospital_id === candidate.hospital_id}
              onSelect={() => {
                setChosen(candidate);
                setReason(null);
              }}
            />
          ))}

          {excluded.length > 0 && (
            <details className="pm-excluded">
              <summary>
                {excluded.length} hospital{excluded.length === 1 ? "" : "s"} the rules ruled
                out — the patient may still choose one
              </summary>
              {excluded.map((candidate) => (
                <HospitalOption
                  key={candidate.hospital_id}
                  candidate={candidate}
                  best={false}
                  selected={chosen?.hospital_id === candidate.hospital_id}
                  onSelect={() => {
                    setChosen(candidate);
                    setReason("patient_request");
                  }}
                />
              ))}
            </details>
          )}

          {isReroute && (
            <section className="pm-card reroute">
              <h3>Why this hospital?</h3>
              <p className="pm-note">
                You are not going to <b>{recommended?.name}</b>. Choose a reason — no typing
                needed.
              </p>
              <div className="pm-reason-grid">
                {REROUTE_REASONS.map((r) => (
                  <button
                    key={r.value}
                    type="button"
                    className={`pm-reason${reason === r.value ? " selected" : ""}`}
                    onClick={() => setReason(r.value)}
                  >
                    <span className="pm-reason-label">{r.label}</span>
                    <span className="pm-reason-hint">{r.hint}</span>
                  </button>
                ))}
              </div>

              <label htmlFor="other">Other reason</label>
              <input
                id="other"
                value={otherReason}
                onChange={(e) => setOtherReason(e.target.value)}
                placeholder="Type only if none of the above fits"
              />
              {(reason === "patient_request" || reason === "family_request") && (
                <div className="pm-legal">
                  A patient&rsquo;s choice of hospital must be honoured. This is recorded as
                  the patient exercising that right, not as a clinical override.
                </div>
              )}
            </section>
          )}

          <button
            type="button"
            className="pm-btn primary"
            disabled={!chosen || busy !== null}
            onClick={() => void submit()}
          >
            {busy === "submit"
              ? "Starting transport…"
              : chosen
                ? `Start transport to ${chosen.name}`
                : "Choose a hospital"}
          </button>
          <button type="button" className="pm-btn ghost" onClick={() => setStep("observe")}>
            Back
          </button>
        </>
      )}

      {step === "done" && trip && (
        <>
          <div className="pm-hero on-duty">
            <span className="pm-hero-tag">Transport started</span>
            <h1>{trip.hospital_name}</h1>
            <p>Navigation has begun in your driver&rsquo;s console.</p>
          </div>
          <section className="pm-card">
            <div className="pm-kv">
              <span>Reference</span>
              <b>{trip.reference}</b>
            </div>
            <div className="pm-kv">
              <span>Emergency</span>
              <b>{trip.category_display}</b>
            </div>
            {trip.symptom_labels && trip.symptom_labels.length > 0 && (
              <div className="pm-kv">
                <span>Observed</span>
                <b>{trip.symptom_labels.join(", ")}</b>
              </div>
            )}
            <div className="pm-kv">
              <span>Priority</span>
              <b>Level {trip.priority_level}</b>
            </div>
            <div className="pm-kv">
              <span>ETA</span>
              <b>{fmtEta(trip.eta)}</b>
            </div>
            <div className="pm-kv">
              <span>Distance</span>
              <b>{fmtDistance(trip.distance_remaining_m)}</b>
            </div>
            <div className="pm-kv">
              <span>Hospital choice</span>
              <b>{trip.choice_reason_display}</b>
            </div>
          </section>
          <div className="pm-info">
            The receiving hospital has the assessment and is preparing. The green corridor
            is arming along the route.
          </div>
          <button type="button" className="pm-btn ghost" onClick={reset}>
            Record another emergency
          </button>
        </>
      )}
    </div>
  );
}

function ErrorNoteInline({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="pm-error">{error}</div>;
}

function Steps({ current }: { current: Step }) {
  const order: Step[] = ["category", "observe", "hospital", "done"];
  const labels = ["Emergency", "Assessment", "Hospital", "Transport"];
  const index = order.indexOf(current);
  return (
    <ol className="pm-steps">
      {labels.map((label, i) => (
        <li key={label} className={i < index ? "done" : i === index ? "now" : ""}>
          <span className="pm-step-dot">{i < index ? "✓" : i + 1}</span>
          {label}
        </li>
      ))}
    </ol>
  );
}

function HospitalOption({
  candidate,
  best,
  selected,
  onSelect,
}: {
  candidate: HospitalCandidate;
  best: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  const readiness = Math.round((1 - candidate.workload_index) * 100);
  return (
    <button
      type="button"
      className={`pm-hospital${selected ? " selected" : ""}${best ? " best" : ""}`}
      onClick={onSelect}
    >
      <div className="pm-hospital-head">
        <span className="pm-hospital-name">
          {best && <span className="pm-best-tag">Recommended</span>}
          {candidate.name}
        </span>
        {selected && <span className="pm-tick">✓</span>}
      </div>
      <div className="pm-hospital-metrics">
        <span>
          <b>{candidate.travel_time_min ?? "—"}</b> min
        </span>
        <span>
          <b>{candidate.distance_km}</b> km
        </span>
        <span>
          <b>{candidate.emergency_beds_available}</b> ED beds
        </span>
        <span>
          <b>{readiness}%</b> ready
        </span>
      </div>
      {!candidate.eligible && (
        <div className="pm-hospital-warn">{candidate.exclusion_reason}</div>
      )}
    </button>
  );
}
