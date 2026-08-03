/**
 * Paramedic application - the Layer 5 / Layer 6 entry point.
 *
 * Selecting a category is the single action that triggers hospital selection,
 * route optimisation, the green corridor and the light/siren mode. The
 * recommendation is rendered with its full reasoning - per-factor scores,
 * exclusion reasons, relaxation warnings - because a crew overriding it needs
 * to see what they are overriding.
 */
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { ApiError } from "@/api/client";
import { brain, dispatch, hospitals } from "@/api/endpoints";
import type {
  CrewShift,
  EmergencyRuleSummary,
  HospitalCandidate,
  HospitalChoiceReason,
  Preemption,
  Recommendation,
  RoutePreview,
  SymptomCode,
  SymptomSpec,
  Trip,
} from "@/api/types";
import {
  Dot, FitBounds, FollowVehicle, HospitalPin, MapCanvas, ROUTE_BLUE,
  RouteLine, VehicleMarkers,
} from "@/components/MapCanvas";
import { GisLayer } from "@/components/map/GisLayer";
import { TrafficLayer } from "@/components/map/TrafficLayer";
import { useGisLayers } from "@/hooks/useGisLayers";
import { ShiftTakeover } from "@/components/ShiftTakeover";
import { SymptomPicker } from "@/components/SymptomPicker";
import {
  Badge, Card, ConnectionDot, Empty, ErrorNote, OwnershipTag,
  fmtDistance, fmtEta, fmtTime, levelClass, LEVEL_LABEL,
} from "@/components/ui";
import { useAuthStore } from "@/stores/authStore";
import { useSocket } from "@/hooks/useSocket";
import type { VehiclePayload } from "@/api/types";

/** The category the picker shows as "Not sure / Other". */
const UNDETERMINED = "unknown";

/** Reasons a crew may pick a hospital the engine did not recommend. */
const CHOICE_REASONS: { value: HospitalChoiceReason; label: string; legal?: boolean }[] = [
  { value: "patient_request", label: "Patient asked for this hospital", legal: true },
  { value: "family_request", label: "Family asked for this hospital", legal: true },
  { value: "continuity", label: "Patient already under care there" },
  { value: "clinical_judgement", label: "Crew clinical judgement" },
  { value: "capacity", label: "Capacity or diversion" },
];

export function ParamedicPage() {
  const { callsign = "" } = useParams();

  const [vehicle, setVehicle] = useState<VehiclePayload | null>(null);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [corridor, setCorridor] = useState<Preemption[]>([]);
  const [categories, setCategories] = useState<EmergencyRuleSummary[]>([]);
  const [routeGeometry, setRouteGeometry] = useState<[number, number][]>([]);

  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null);
  const [chosenHospitalId, setChosenHospitalId] = useState<number | null>(null);
  const [overrideReason, setOverrideReason] = useState("");
  const [choiceReason, setChoiceReason] = useState<HospitalChoiceReason>("clinical_judgement");
  const [patientAge, setPatientAge] = useState("");
  const [patientNotes, setPatientNotes] = useState("");
  const [deteriorating, setDeteriorating] = useState(false);

  // What the crew can see. Always recorded; the only clinical input when the
  // category is "Not sure / Other".
  const [symptomCatalogue, setSymptomCatalogue] = useState<SymptomSpec[]>([]);
  const [symptoms, setSymptoms] = useState<SymptomCode[]>([]);

  // Crew pairing for this vehicle. The map and the clinical steps do not
  // depend on it - an unpaired crew must still be able to work - but the
  // screen shows the takeover first because that is the start of the day.
  const [shift, setShift] = useState<CrewShift | null>(null);
  const username = useAuthStore((state) => state.user?.username ?? "");

  // Live GIS so the crew sees the same traffic and signals the ops room does.
  const gis = useGisLayers();

  // Preview of the road to whichever hospital is currently selected. Held
  // apart from `routeGeometry` (the trip's committed route) because nothing
  // has been dispatched on the strength of it yet - it exists so the crew can
  // see what they are about to agree to.
  const [previewRoute, setPreviewRoute] = useState<RoutePreview | null>(null);
  const [previewFor, setPreviewFor] = useState<HospitalCandidate | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [busy, setBusy] = useState<"recommend" | "confirm" | "route" | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Name of the hospital the trip was last committed to, for the confirmation. */
  const [dispatched, setDispatched] = useState<string | null>(null);

  useEffect(() => {
    hospitals.ruleCatalogue().then(setCategories).catch(() => setCategories([]));
    hospitals
      .symptomCatalogue()
      .then((result) => setSymptomCatalogue(result.symptoms))
      .catch(() => setSymptomCatalogue([]));
  }, []);

  const toggleSymptom = (code: SymptomCode) =>
    setSymptoms((current) =>
      current.includes(code) ? current.filter((s) => s !== code) : [...current, code],
    );

  /** Symptoms are mandatory when the crew could not name a category. */
  const symptomsRequired = selectedCategory === UNDETERMINED;

  const loadTrip = useCallback(async () => {
    try {
      const active = await dispatch.tripsForVehicle(callsign);
      const current = active[0] ?? null;
      setTrip(current);
      if (current?.active_route) setRouteGeometry(current.active_route.geometry);
      if (current) setCorridor((await dispatch.corridor(current.id)).corridor);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the current response.");
    }
  }, [callsign]);

  useEffect(() => {
    void loadTrip();
  }, [loadTrip]);

  const { status } = useSocket(`/ws/vehicle/${encodeURIComponent(callsign)}/`, {
    handlers: {
      snapshot: (data) => {
        const snapshot = data as { vehicle: VehiclePayload; trip: Trip | null; corridor: Preemption[]; route: { geometry: [number, number][] } | null };
        setVehicle(snapshot.vehicle);
        setTrip(snapshot.trip);
        setCorridor(snapshot.corridor ?? []);
        if (snapshot.route?.geometry) setRouteGeometry(snapshot.route.geometry);
      },
      vehicle_position: (data) => setVehicle(data as VehiclePayload),
      route_updated: (data) => {
        setRouteGeometry((data as { geometry: [number, number][] }).geometry);
        void loadTrip();
      },
      priority_directive: () => void loadTrip(),
      trip_stage: () => void loadTrip(),
    },
  });

  const position: [number, number] | null = vehicle
    ? [vehicle.latitude, vehicle.longitude]
    : null;

  /**
   * Route preview to one candidate.
   *
   * Uses the vehicle's live position as the origin, and the rule's priority
   * level so the router weights signals and contraflow the same way the real
   * dispatch will - a preview computed at a different priority would show a
   * road the actual trip is not going to take.
   */
  const previewRouteTo = useCallback(
    async (candidate: HospitalCandidate, priorityLevel: number): Promise<void> => {
      if (!position) return;
      setPreviewFor(candidate);
      setPreviewError(null);
      setBusy("route");
      try {
        const result = await brain.route(
          position,
          [candidate.latitude, candidate.longitude],
          priorityLevel,
        );
        setPreviewRoute(result);
      } catch (err) {
        setPreviewRoute(null);
        setPreviewError(
          err instanceof Error ? err.message : "Could not compute a route to that hospital.",
        );
      } finally {
        setBusy(null);
      }
    },
    [position],
  );

  /**
   * Step 2 - find a hospital and show the road to it.
   *
   * The recommendation and the route are one action from the crew's point of
   * view: "where am I taking this patient, and how do I get there". Splitting
   * them into two button presses made the map sit empty at the moment the
   * decision was actually being made.
   */
  const findHospital = async (): Promise<void> => {
    if (!selectedCategory || !position) return;
    if (symptomsRequired && symptoms.length === 0) {
      setError(
        "Tick at least one symptom — with no category and no observations there is " +
        "nothing to match a hospital on.",
      );
      return;
    }
    setBusy("recommend");
    setError(null);
    setPreviewRoute(null);
    setPreviewFor(null);
    try {
      const result = await hospitals.recommend(
        position[0], position[1], selectedCategory, symptoms,
      );
      setRecommendation(result);
      setChosenHospitalId(result.recommended?.hospital_id ?? null);
      setChoiceReason("clinical_judgement");
      if (result.recommended) {
        await previewRouteTo(result.recommended, result.rule.priority_level);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Recommendation failed.");
    } finally {
      setBusy(null);
    }
  };

  /**
   * Step 3 - clicking a candidate re-routes the preview to it.
   *
   * Preview only. Committing needs the override reason when this is not the
   * recommended hospital, and a single click is not consent to skip a
   * clinical-governance record.
   */
  const chooseHospital = (candidate: HospitalCandidate): void => {
    setChosenHospitalId(candidate.hospital_id);
    void previewRouteTo(candidate, recommendation?.rule.priority_level ?? 1);
  };

  const isOverride =
    chosenHospitalId !== null && chosenHospitalId !== recommendation?.recommended?.hospital_id;

  const chosenCandidate =
    recommendation?.candidates.find((c) => c.hospital_id === chosenHospitalId) ?? null;

  /**
   * Commit the destination.
   *
   * This is what makes the choice real: `assess` persists the hospital, plans
   * the optimised route, arms the green corridor and broadcasts - so the
   * operations map redraws this vehicle's route without anyone refreshing it.
   */
  const confirm = async (): Promise<void> => {
    if (!trip || !selectedCategory) return;
    if (isOverride && !overrideReason.trim()) {
      setError("A reason is required when sending the patient to a hospital the rules did not pick.");
      return;
    }

    setBusy("confirm");
    setError(null);
    try {
      const body: Parameters<typeof dispatch.assess>[1] = {
        emergency_category: selectedCategory,
        symptoms,
        patient_notes: patientNotes,
        patient_deteriorating: deteriorating,
      };
      if (patientAge) body.patient_age = Number(patientAge);
      if (isOverride && chosenHospitalId) {
        body.hospital_id = chosenHospitalId;
        body.override_reason = overrideReason;
        body.choice_reason = choiceReason;
      }
      const result = await dispatch.assess(trip.id, body);
      setTrip(result.trip);
      setCorridor(result.corridor ?? []);
      if (result.trip.active_route) setRouteGeometry(result.trip.active_route.geometry);
      // The committed route supersedes the preview; leaving both drawn would
      // show two routes to the same place.
      setPreviewRoute(null);
      setPreviewFor(null);
      setDispatched(result.trip.hospital_name ?? "the receiving hospital");
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 403
          ? "Your role cannot confirm an assessment. Sign in as ambulance crew."
          : err instanceof Error
            ? err.message
            : "Assessment failed.",
      );
    } finally {
      setBusy(null);
    }
  };

  const changeStage = async (stage: string): Promise<void> => {
    if (!trip) return;
    try {
      setTrip(stage === "handover" ? await dispatch.handover(trip.id) : await dispatch.setStage(trip.id, stage));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Stage change failed.");
    }
  };

  const openCorridor = corridor.filter((row) =>
    ["planned", "armed", "active"].includes(row.state),
  );

  return (
    <div className="split wide">
      <aside className="sidebar">
        <div className="sidebar-head">
          <ConnectionDot status={status} />
          {shift?.status === "active" && <Badge tone="ok">on duty</Badge>}
        </div>

        {/* Start of day: crew pairing and the vehicle check. */}
        <ShiftTakeover
          callsign={callsign}
          currentUsername={username}
          onShiftChange={setShift}
        />
        <ErrorNote error={error} />

        <Card title="Vehicle">
          <div style={{ fontSize: 20, fontWeight: 700 }}>{callsign}</div>

          <div className="veh-ident" style={{ marginTop: 8 }}>
            {vehicle?.registration && <span className="veh-reg">{vehicle.registration}</span>}
            {vehicle && <OwnershipTag vehicle={vehicle} />}
          </div>

          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            {vehicle?.vehicle_type_display ?? vehicle?.vehicle_type ?? "…"}
            {vehicle?.operator ? ` · ${vehicle.operator}` : ""}
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {vehicle?.status_display ?? vehicle?.status ?? "…"}
            {vehicle?.is_als ? " · Advanced Life Support" : ""}
          </div>

          {vehicle && (
            <div style={{ marginTop: 10 }}>
              <Badge tone={levelClass(vehicle.priority_level) as "l1"}>
                {LEVEL_LABEL[vehicle.priority_level]}
              </Badge>
              <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                Siren: <b>{vehicle.siren_mode}</b> · Lights: <b>{vehicle.light_pattern}</b>
              </div>
            </div>
          )}
        </Card>

        <Card title="Current response">
          {!trip ? (
            <Empty>No active trip for this vehicle.</Empty>
          ) : (
            <>
              <div style={{ fontWeight: 700 }}>{trip.reference}</div>
              <div className="muted" style={{ fontSize: 12 }}>
                {trip.stage_display} · {trip.category_display}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {trip.hospital_name ? `Destination: ${trip.hospital_name}` : "Destination not set"}
              </div>
              <div style={{ marginTop: 6 }}>
                <span className="eta">ETA {fmtEta(trip.eta)}</span>
                <span className="muted"> · {fmtDistance(trip.distance_remaining_m)} remaining</span>
              </div>
              <div className="btn-row">
                <button className="ghost" type="button" onClick={() => void changeStage("on_scene")}>
                  On scene
                </button>
                <button className="ghost" type="button" onClick={() => void changeStage("handover")}>
                  Handover
                </button>
              </div>
            </>
          )}
        </Card>

        <Card title="1. Patient assessment">
          <p className="hint">What is wrong with the patient?</p>
          <div className="cat-grid">
            {categories
              .filter((category) => category.category !== UNDETERMINED)
              .map((category) => (
                <button
                  key={category.category}
                  type="button"
                  className={`cat${selectedCategory === category.category ? " selected" : ""}`}
                  data-level={category.default_priority_level}
                  onClick={() => setSelectedCategory(category.category)}
                >
                  {category.display_name}
                  <small>
                    {category.required_facilities
                      .map((facility) => facility.replaceAll("_", " "))
                      .join(", ") || "Emergency department"}
                  </small>
                </button>
              ))}
            {/* Pulled out of the list and given its own affordance: choosing
                it is a legitimate clinical answer, not a failure to answer. */}
            <button
              type="button"
              className={`cat other${selectedCategory === UNDETERMINED ? " selected" : ""}`}
              onClick={() => setSelectedCategory(UNDETERMINED)}
            >
              Not sure / Other
              <small>Record what you can see instead</small>
            </button>
          </div>

          {/* Always available, mandatory when the category is undetermined. */}
          <SymptomPicker
            catalogue={symptomCatalogue}
            selected={symptoms}
            onToggle={toggleSymptom}
            assessment={recommendation?.symptom_assessment}
            required={symptomsRequired}
          />

          <label htmlFor="age">Patient age (optional)</label>
          <input id="age" type="number" min={0} max={130} value={patientAge}
            onChange={(event) => setPatientAge(event.target.value)} />

          <label htmlFor="notes">Clinical notes</label>
          <textarea id="notes" value={patientNotes}
            onChange={(event) => setPatientNotes(event.target.value)} />

          <label className="checkbox">
            <input type="checkbox" checked={deteriorating}
              onChange={(event) => setDeteriorating(event.target.checked)} />
            Patient condition deteriorating
          </label>

          <div className="btn-row">
            <button type="button" disabled={!selectedCategory || busy !== null}
              onClick={() => void findHospital()}>
              {busy === "recommend" ? "Evaluating hospitals…" : "2. Find suitable hospital"}
            </button>
          </div>
        </Card>

        {recommendation && (
          <Card
            title="3. Choose the receiving hospital"
            actions={
              <Badge tone="warn">{recommendation.candidates.length} in range</Badge>
            }
          >
            <RuleBox recommendation={recommendation} />
            <p className="hint">Tap a hospital to see it and its route on the map.</p>
            <ErrorNote error={previewError} />

            <div className="hosp-table-head">
              <span>Hospital</span>
              <span>ETA</span>
              <span>Distance</span>
              <span>Beds</span>
              <span>Ready</span>
            </div>

            {recommendation.candidates
              .filter((candidate) => candidate.eligible)
              .map((candidate, index) => (
                <CandidateCard
                  key={candidate.hospital_id}
                  candidate={candidate}
                  selected={chosenHospitalId === candidate.hospital_id}
                  best={index === 0}
                  onSelect={() => chooseHospital(candidate)}
                  route={
                    previewFor?.hospital_id === candidate.hospital_id ? previewRoute : null
                  }
                  routeLoading={
                    busy === "route" && previewFor?.hospital_id === candidate.hospital_id
                  }
                />
              ))}

            {/* A patient's choice of hospital must be honoured even when the
                rules excluded it - so the excluded list is selectable rather
                than a footnote, and says plainly what is being accepted. */}
            <ExcludedHospitals
              candidates={recommendation.candidates}
              chosenHospitalId={chosenHospitalId}
              onSelect={(candidate) => {
                chooseHospital(candidate);
                setChoiceReason("patient_request");
              }}
            />

            {isOverride && (
              <div className="override-block">
                <label htmlFor="choiceReason">Why this hospital?</label>
                <select
                  id="choiceReason"
                  value={choiceReason}
                  onChange={(event) =>
                    setChoiceReason(event.target.value as HospitalChoiceReason)
                  }
                >
                  {CHOICE_REASONS.map((reason) => (
                    <option key={reason.value} value={reason.value}>
                      {reason.label}
                    </option>
                  ))}
                </select>

                {CHOICE_REASONS.find((r) => r.value === choiceReason)?.legal && (
                  <p className="legal-note">
                    A patient&rsquo;s choice of hospital must be honoured. This is recorded
                    as the patient exercising that right, not as a clinical override.
                  </p>
                )}

                <label htmlFor="override">Details (required)</label>
                <input id="override" value={overrideReason}
                  onChange={(event) => setOverrideReason(event.target.value)}
                  placeholder="e.g. patient is under cardiology there" />
              </div>
            )}

            <div className="btn-row">
              <button
                type="button"
                className="primary-action"
                disabled={!trip || !chosenCandidate || busy !== null}
                onClick={() => void confirm()}
              >
                {busy === "confirm"
                  ? "Dispatching…"
                  : chosenCandidate
                    ? `Send ${callsign} to ${chosenCandidate.name}`
                    : "Choose a hospital above"}
              </button>
            </div>
            {!trip && (
              <p className="hint">
                This vehicle has no active response, so there is nothing to dispatch yet.
              </p>
            )}
            {dispatched && (
              <div className="dispatched-note">
                ✓ Routed to <b>{dispatched}</b>. Green corridor arming — the operations
                map is showing this route now.
              </div>
            )}
          </Card>
        )}

        <Card title="Green corridor ahead">
          {openCorridor.length === 0 ? (
            <Empty>No signals under priority control.</Empty>
          ) : (
            openCorridor.map((row) => (
              <div key={row.id} className={`trip ${row.state === "active" ? "l1" : "l3"}`}>
                <div className="head">
                  <span className="ref">{row.intersection}</span>
                  <Badge tone={row.state === "active" ? "ok" : "warn"}>{row.state}</Badge>
                </div>
                <div className="meta">
                  green at {fmtTime(row.planned_green_at)} · hold {Math.round(row.hold_duration_s)}s
                </div>
              </div>
            ))
          )}
        </Card>
      </aside>

      <MapCanvas centre={position ?? undefined} zoom={15}>
        {/* The crew see the same road picture the control room does: live
            congestion, every signal's aspect, and what is blocking the way.
            Driving a green corridor without being able to see the signals is
            exactly the situation this platform exists to remove. */}
        <TrafficLayer collection={gis.data.road_network ?? null} />
        <GisLayer
          layer="traffic_signals"
          collection={gis.data.traffic_signals ?? null}
        />
        <GisLayer
          layer="road_closures"
          collection={gis.data.road_closures ?? null}
        />

        {vehicle && (
          <VehicleMarkers
            vehicles={[vehicle]}
            {...(trip ? { tripsByCallsign: { [vehicle.callsign]: trip } } : {})}
          />
        )}

        {/* The committed route, in the same vibrant blue the ops map uses. */}
        <RouteLine geometry={routeGeometry} colour={ROUTE_BLUE} />

        {/* The preview to the hospital under consideration, in green so it is
            never confused with the route the vehicle is already following. */}
        {previewRoute && previewRoute.geometry.length > 1 && (
          <RouteLine geometry={previewRoute.geometry} colour="#2ecc71" />
        )}

        {openCorridor.map((row) => (
          <Dot key={row.id} position={[row.latitude, row.longitude]}
            colour={row.state === "active" ? "#2ecc71" : "#ffd166"} radius={7}>
            <b>{row.controller_id}</b>
            <br />
            {row.state}
          </Dot>
        ))}

        {/* Hospitals are pins here too, so the paramedic map and the ops map
            speak the same visual language. */}
        {recommendation?.candidates.map((candidate) => (
          <HospitalPin
            key={candidate.hospital_id}
            position={[candidate.latitude, candidate.longitude]}
            name={candidate.name}
            onDiversion={!candidate.eligible}
            isTrauma={candidate.hospital_id === chosenHospitalId}
            onClick={() => candidate.eligible && chooseHospital(candidate)}
            detail={
              <>
                <div className="veh-tip-row">
                  <span className="k">Distance</span>
                  <b>{candidate.distance_km} km</b>
                </div>
                <div className="veh-tip-row">
                  <span className="k">ED beds</span>
                  <b>{candidate.emergency_beds_available}</b>
                </div>
                <div className="veh-tip-row">
                  <span className="k">Status</span>
                  <b>{candidate.eligible ? "Eligible" : candidate.exclusion_reason}</b>
                </div>
              </>
            }
          />
        ))}

        {/* Frame whichever route is the current subject of the decision. */}
        <FitBounds
          points={previewRoute?.geometry.length ? previewRoute.geometry : routeGeometry}
        />

        {/* Keep the moving ambulance on screen. `FollowVehicle` only recentres
            once the vehicle drifts near the edge, and stands down while the
            crew is panning, so watching the run does not fight the map. */}
        <FollowVehicle position={position} />
      </MapCanvas>
    </div>
  );
}

function RuleBox({ recommendation }: { recommendation: Recommendation }) {
  const { rule } = recommendation;
  return (
    <>
      <div className="card inner">
        <div style={{ fontWeight: 700 }}>{rule.display_name}</div>
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          Required: <b>{rule.required_facilities.join(", ") || "emergency department"}</b>
          <br />
          Priority: <b>Level {rule.priority_level}</b>
          {rule.golden_window_min ? ` · golden window ${rule.golden_window_min} min` : ""}
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>{rule.guidance}</div>
      </div>
      {recommendation.relaxed && (
        <div className="badge bad" style={{ display: "block", padding: 9, marginBottom: 8 }}>
          {recommendation.relaxation_note}
        </div>
      )}
    </>
  );
}

function CandidateCard({
  candidate, selected, best, onSelect, route, routeLoading,
}: {
  candidate: HospitalCandidate;
  selected: boolean;
  best: boolean;
  onSelect: () => void;
  /** The optimised route to this hospital, once computed. */
  route: RoutePreview | null;
  routeLoading: boolean;
}) {
  // Readiness is workload inverted: what a crew wants to know is "can they
  // take my patient right now", not "how busy are they".
  const readiness = Math.round((1 - candidate.workload_index) * 100);
  const readinessTone = readiness >= 60 ? "ok" : readiness >= 35 ? "warn" : "bad";

  return (
    <div className={`trip candidate ${selected ? "l1" : "l4"}`} onClick={onSelect} role="button" tabIndex={0}
      onKeyDown={(event) => { if (event.key === "Enter") onSelect(); }}>
      <div className="head">
        <span className="ref">{best ? "★ " : ""}{candidate.name}</span>
        <Badge tone={best ? "ok" : "warn"}>{(candidate.score * 100).toFixed(0)}</Badge>
      </div>

      {/* The comparison row - the five numbers a crew actually decides on. */}
      <div className="hosp-metrics">
        <div>
          <span className="k">ETA</span>
          <b>{candidate.travel_time_min ? `${candidate.travel_time_min} min` : "—"}</b>
        </div>
        <div>
          <span className="k">Distance</span>
          <b>{candidate.distance_km} km</b>
        </div>
        <div>
          <span className="k">ED beds</span>
          <b>{candidate.emergency_beds_available}</b>
        </div>
        <div>
          <span className="k">ICU</span>
          <b>{candidate.icu_beds_available}</b>
        </div>
        <div>
          <span className="k">Ready</span>
          <b className={`ready ${readinessTone}`}>{readiness}%</b>
        </div>
      </div>

      <div className="meta muted">
        {Object.entries(candidate.factors)
          .map(([name, value]) => `${name.replaceAll("_", " ")} ${(value * 100).toFixed(0)}`)
          .join(" · ")}
        {candidate.within_golden_window === false && (
          <> · <Badge tone="bad">outside golden window</Badge></>
        )}
      </div>
      {candidate.warnings?.map((warning) => (
        <div className="meta" key={warning}>
          <Badge tone="bad">warning</Badge> {warning}
        </div>
      ))}

      {routeLoading && <div className="hosp-route-note">Computing optimised route…</div>}
      {!routeLoading && route && (
        <div className="hosp-route-note">
          <span>
            Optimised route <b>{(route.total_distance_m / 1000).toFixed(1)} km</b>
          </span>
          <span>
            <b>{route.total_duration_min} min</b> in current traffic
          </span>
          <span>
            <b>{route.signalised_nodes.length}</b> signals on corridor
          </span>
        </div>
      )}
    </div>
  );
}

/**
 * Hospitals the rules ruled out - and a way to pick one anyway.
 *
 * Collapsed by default, because on a normal run this is noise. But a patient
 * has the right to choose where they are treated, and that right does not
 * evaporate because the engine scored the hospital badly - so the list is
 * selectable, states plainly what capability is being given up, and records
 * the choice as the patient's rather than as a crew override.
 */
function ExcludedHospitals({
  candidates,
  chosenHospitalId,
  onSelect,
}: {
  candidates: HospitalCandidate[];
  chosenHospitalId: number | null;
  onSelect: (candidate: HospitalCandidate) => void;
}) {
  const [open, setOpen] = useState(false);
  const excluded = candidates.filter((candidate) => !candidate.eligible);
  if (excluded.length === 0) return null;

  return (
    <div className="excluded-block">
      <button type="button" className="linklike" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} {excluded.length} hospital{excluded.length === 1 ? "" : "s"} the
        rules ruled out — patient may still choose one
      </button>

      {open && (
        <>
          <p className="legal-note">
            If the patient or their family asks for one of these, take them there. The
            reason is recorded and the hospital is warned about the capability gap.
          </p>
          {excluded.map((candidate) => (
            <div
              key={candidate.hospital_id}
              className={`trip candidate excluded ${
                chosenHospitalId === candidate.hospital_id ? "l1" : "l4"
              }`}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(candidate)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onSelect(candidate);
              }}
            >
              <div className="head">
                <span className="ref">{candidate.name}</span>
                <Badge tone="bad">not recommended</Badge>
              </div>
              <div className="hosp-metrics">
                <div>
                  <span className="k">Distance</span>
                  <b>{candidate.distance_km} km</b>
                </div>
                <div>
                  <span className="k">ED beds</span>
                  <b>{candidate.emergency_beds_available}</b>
                </div>
                <div>
                  <span className="k">ICU</span>
                  <b>{candidate.icu_beds_available}</b>
                </div>
              </div>
              <div className="meta warn-text">{candidate.exclusion_reason}</div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
