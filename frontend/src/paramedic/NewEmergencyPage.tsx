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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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

/** How long the crew have to overrule the recommendation before it commits. */
const AUTO_START_SECONDS = 10;

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

  /**
   * Auto-start countdown on the recommended hospital.
   *
   * The engine has already decided; the crew's job on this screen is to
   * *disagree*, not to confirm. Making them press a button to accept an answer
   * they were always going to accept costs seconds in a cardiac transport, so
   * the recommendation commits itself unless somebody intervenes.
   *
   * `null` means no countdown is running - either because the crew touched the
   * list (they are choosing, so the clock has no business rushing them), or
   * because it has already fired.
   */
  const [autoIn, setAutoIn] = useState<number | null>(null);

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

  /** Stop the clock. Any deliberate act on this screen counts. */
  const holdCountdown = useCallback(() => setAutoIn(null), []);

  /** Kept in a ref so the countdown effect does not depend on form state. */
  const submitRef = useRef<(() => Promise<void>) | null>(null);

  const eligible = useMemo(
    () => recommendation?.candidates.filter((c) => c.eligible) ?? [],
    [recommendation],
  );
  const excluded = useMemo(
    () => recommendation?.candidates.filter((c) => !c.eligible) ?? [],
    [recommendation],
  );

  /**
   * Tick the auto-start clock.
   *
   * Fires `submitRef` rather than `submit` directly: the effect must not
   * re-subscribe every time a piece of form state changes, or the countdown
   * restarts from ten on each keystroke and never reaches zero.
   */
  useEffect(() => {
    if (autoIn === null || step !== "hospital") return;
    if (autoIn <= 0) {
      setAutoIn(null);
      void submitRef.current?.();
      return;
    }
    const timer = window.setTimeout(() => setAutoIn((value) => (value ?? 1) - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [autoIn, step]);

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

  /**
   * Rank the hospitals for this patient, and start the clock.
   *
   * This is the route optimisation: `recommend` scores every capable hospital
   * on live travel time from the vehicle's own position, so by the time this
   * returns the road to each candidate has already been costed. Confirming
   * only commits the trip to the winner and arms the corridor.
   */
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
      // Only when there is something to commit to. With no eligible hospital
      // the crew must choose from the excluded list, and a countdown would be
      // counting down to nothing.
      setAutoIn(result.recommended ? AUTO_START_SECONDS : null);
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

  submitRef.current = submit;

  const reset = () => {
    setAutoIn(null);
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
          {/*
            Two ways forward, side by side.

            The assessment is worth having and is the default. But a crew with
            a patient who is arresting in front of them will not fill in an age
            and a notes box first, and a workflow that insists gets abandoned
            or falsified. "Skip to route" takes the category - which is what
            the recommender actually matches on - and goes straight to finding
            a hospital.
          */}
          <div className="pm-btn-row">
            <button
              type="button"
              className="pm-btn primary"
              disabled={!category}
              onClick={() => setStep("observe")}
            >
              Next — patient assessment
            </button>
            <button
              type="button"
              className="pm-btn secondary"
              disabled={!category || busy !== null}
              onClick={() => void findHospitals()}
              title="Go straight to hospital selection without recording an assessment"
            >
              {busy === "find" ? "Finding…" : "Skip to route"}
            </button>
          </div>
          {category === UNDETERMINED && (
            <p className="pm-note">
              “Not sure” needs at least one observation before a hospital can be matched,
              so the assessment step cannot be skipped for it.
            </p>
          )}
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

          {/* The countdown lives inside the recommended hospital's own card,
              not in a corner of the screen: it is that hospital that is about
              to be committed to, and the crew have to be able to see which. */}
          {eligible.map((candidate, index) => (
            <HospitalOption
              key={candidate.hospital_id}
              candidate={candidate}
              best={index === 0}
              selected={chosen?.hospital_id === candidate.hospital_id}
              countdown={
                index === 0 && autoIn !== null && recommended?.hospital_id === candidate.hospital_id
                  ? autoIn
                  : null
              }
              onHold={holdCountdown}
              onSelect={() => {
                // Choosing anything is an intervention, so the clock stops -
                // including choosing the recommendation itself, which means
                // "I have read this" rather than "hurry me along".
                holdCountdown();
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
                  countdown={null}
                  onHold={holdCountdown}
                  onSelect={() => {
                    holdCountdown();
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
  countdown,
  onHold,
  onSelect,
}: {
  candidate: HospitalCandidate;
  best: boolean;
  selected: boolean;
  /** Seconds until this hospital commits itself, or null when not counting. */
  countdown: number | null;
  onHold: () => void;
  onSelect: () => void;
}) {
  const readiness = Math.round((1 - candidate.workload_index) * 100);
  return (
    <button
      type="button"
      className={`pm-hospital${selected ? " selected" : ""}${best ? " best" : ""}${
        countdown !== null ? " counting" : ""
      }`}
      onClick={onSelect}
    >
      <div className="pm-hospital-head">
        <span className="pm-hospital-name">
          {best && <span className="pm-best-tag">Recommended</span>}
          {candidate.name}
        </span>
        {selected && <span className="pm-tick">✓</span>}
      </div>

      {countdown !== null && (
        <div className="pm-countdown">
          <span className="pm-countdown-ring" style={{ "--pm-cd": countdown } as never}>
            {countdown}
          </span>
          <span className="pm-countdown-copy">
            Starting the route here in {countdown}s unless you choose another hospital.
          </span>
          <span
            className="pm-countdown-hold"
            role="button"
            tabIndex={0}
            onClick={(event) => {
              // The card itself is a button, so this must not select the
              // hospital as well - holding the clock is the opposite intent.
              event.stopPropagation();
              onHold();
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                event.stopPropagation();
                onHold();
              }
            }}
          >
            Hold
          </span>
        </div>
      )}
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
