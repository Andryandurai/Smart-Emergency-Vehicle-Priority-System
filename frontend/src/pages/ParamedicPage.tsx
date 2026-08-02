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
import { dispatch, hospitals } from "@/api/endpoints";
import type {
  EmergencyRuleSummary,
  HospitalCandidate,
  Preemption,
  Recommendation,
  Trip,
} from "@/api/types";
import { Dot, FitBounds, MapCanvas, RouteLine, VehicleMarkers } from "@/components/MapCanvas";
import {
  Badge, Card, ConnectionDot, Empty, ErrorNote,
  fmtDistance, fmtEta, fmtTime, levelClass, LEVEL_LABEL,
} from "@/components/ui";
import { useSocket } from "@/hooks/useSocket";
import type { VehiclePayload } from "@/api/types";

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
  const [patientAge, setPatientAge] = useState("");
  const [patientNotes, setPatientNotes] = useState("");
  const [deteriorating, setDeteriorating] = useState(false);

  const [busy, setBusy] = useState<"recommend" | "confirm" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    hospitals.ruleCatalogue().then(setCategories).catch(() => setCategories([]));
  }, []);

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

  const findHospital = async (): Promise<void> => {
    if (!selectedCategory || !position) return;
    setBusy("recommend");
    setError(null);
    try {
      const result = await hospitals.recommend(position[0], position[1], selectedCategory);
      setRecommendation(result);
      setChosenHospitalId(result.recommended?.hospital_id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Recommendation failed.");
    } finally {
      setBusy(null);
    }
  };

  const confirm = async (): Promise<void> => {
    if (!trip || !selectedCategory) return;
    const isOverride =
      chosenHospitalId !== null && chosenHospitalId !== recommendation?.recommended?.hospital_id;
    if (isOverride && !overrideReason.trim()) {
      setError("A reason is required when overriding the recommendation.");
      return;
    }

    setBusy("confirm");
    setError(null);
    try {
      const body: Parameters<typeof dispatch.assess>[1] = {
        emergency_category: selectedCategory,
        patient_notes: patientNotes,
        patient_deteriorating: deteriorating,
      };
      if (patientAge) body.patient_age = Number(patientAge);
      if (isOverride && chosenHospitalId) {
        body.hospital_id = chosenHospitalId;
        body.override_reason = overrideReason;
      }
      const result = await dispatch.assess(trip.id, body);
      setTrip(result.trip);
      setCorridor(result.corridor ?? []);
      if (result.trip.active_route) setRouteGeometry(result.trip.active_route.geometry);
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
        </div>
        <ErrorNote error={error} />

        <Card title="Vehicle">
          <div style={{ fontSize: 20, fontWeight: 700 }}>{callsign}</div>
          <div className="muted" style={{ fontSize: 12 }}>
            {vehicle?.vehicle_type} · {vehicle?.status ?? "…"}
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

        <Card title="1. Patient assessment — select emergency category">
          <div className="cat-grid">
            {categories.map((category) => (
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
          </div>

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
          <Card title="3. Rule-based recommendation">
            <RuleBox recommendation={recommendation} />
            {recommendation.candidates
              .filter((candidate) => candidate.eligible)
              .map((candidate, index) => (
                <CandidateCard
                  key={candidate.hospital_id}
                  candidate={candidate}
                  selected={chosenHospitalId === candidate.hospital_id}
                  best={index === 0}
                  onSelect={() => setChosenHospitalId(candidate.hospital_id)}
                />
              ))}

            <ExcludedList candidates={recommendation.candidates} />

            {chosenHospitalId !== recommendation.recommended?.hospital_id && (
              <>
                <label htmlFor="override">Override reason (required)</label>
                <input id="override" value={overrideReason}
                  onChange={(event) => setOverrideReason(event.target.value)}
                  placeholder="Why a different hospital?" />
              </>
            )}

            <div className="btn-row">
              <button type="button" disabled={!trip || busy !== null} onClick={() => void confirm()}>
                {busy === "confirm" ? "Confirming…" : "4. Confirm & start green corridor"}
              </button>
            </div>
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
        {vehicle && <VehicleMarkers vehicles={[vehicle]} />}
        <RouteLine geometry={routeGeometry} />
        {openCorridor.map((row) => (
          <Dot key={row.id} position={[row.latitude, row.longitude]}
            colour={row.state === "active" ? "#2ecc71" : "#ffd166"} radius={7}>
            <b>{row.controller_id}</b>
            <br />
            {row.state}
          </Dot>
        ))}
        {recommendation?.candidates.map((candidate) => (
          <Dot key={candidate.hospital_id} position={[candidate.latitude, candidate.longitude]}
            colour={candidate.eligible ? (candidate.hospital_id === chosenHospitalId ? "#2ecc71" : "#4da3ff") : "#7f8c9b"}
            radius={7}>
            <b>{candidate.name}</b>
            <br />
            {candidate.eligible ? "eligible" : candidate.exclusion_reason}
          </Dot>
        ))}
        <FitBounds points={routeGeometry} />
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
  candidate, selected, best, onSelect,
}: {
  candidate: HospitalCandidate; selected: boolean; best: boolean; onSelect: () => void;
}) {
  return (
    <div className={`trip ${selected ? "l1" : "l4"}`} onClick={onSelect} role="button" tabIndex={0}
      onKeyDown={(event) => { if (event.key === "Enter") onSelect(); }}>
      <div className="head">
        <span className="ref">{best ? "★ " : ""}{candidate.name}</span>
        <Badge tone={best ? "ok" : "warn"}>{(candidate.score * 100).toFixed(0)}</Badge>
      </div>
      <div className="meta">
        {candidate.travel_time_min ? `${candidate.travel_time_min} min` : "-"} in current traffic ·{" "}
        {candidate.distance_km} km
      </div>
      <div className="meta">
        ED beds {candidate.emergency_beds_available} · ICU {candidate.icu_beds_available} · workload{" "}
        {(candidate.workload_index * 100).toFixed(0)}%
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
    </div>
  );
}

function ExcludedList({ candidates }: { candidates: HospitalCandidate[] }) {
  const excluded = candidates.filter((candidate) => !candidate.eligible);
  if (excluded.length === 0) return null;
  return (
    <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
      Excluded:{" "}
      {excluded.map((candidate) => `${candidate.name} (${candidate.exclusion_reason})`).join("; ")}
    </div>
  );
}
