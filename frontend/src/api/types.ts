/**
 * Types mirroring the DRF serializers.
 *
 * Hand-written rather than generated: the API has no OpenAPI schema yet, and a
 * generator would produce types for all 199 endpoints when the console uses
 * 20. These cover exactly what is consumed, and the contract tests in
 * `src/api/__tests__` assert the shapes against real fixtures so drift shows up
 * as a test failure rather than a runtime `undefined`.
 */

// ---------------------------------------------------------------------------
// Auth & roles
// ---------------------------------------------------------------------------
export type Role =
  | "administrators"
  | "traffic_police"
  | "hospital_staff"
  | "ambulance_drivers"
  /** Attending clinician. Split out from ambulance_drivers - the group name
   *  avoids "paramedics", which is a legacy alias of the driver role. */
  | "paramedic_crew"
  | "dispatchers"
  | "public_users";

export interface Capabilities {
  clinical_data: boolean;
  traffic_control: boolean;
  dispatch_control: boolean;
  admin_site: boolean;
}

export interface CurrentUser {
  id: number;
  username: string;
  email: string;
  name: string;
  roles: Role[];
  is_superuser: boolean;
  is_staff: boolean;
  capabilities: Capabilities;
  auth_method?: "jwt" | "legacy_token" | "session" | "unknown";
}

export interface TokenPair {
  access: string;
  refresh: string;
  user?: CurrentUser;
}

export interface RoleDescriptor {
  key: Role;
  label: string;
  description: string;
  clinical_access: boolean;
  traffic_control: boolean;
  dispatch_control: boolean;
}

// ---------------------------------------------------------------------------
// Fleet (Layer 1)
// ---------------------------------------------------------------------------
export type PriorityLevel = 1 | 2 | 3 | 4;

export type VehicleStatus =
  | "offline" | "available" | "dispatched" | "on_scene"
  | "transporting" | "at_hospital" | "returning" | "out_of_service";

/** Fire and police were retired - see the VehicleType docstring in enums.py. */
export type VehicleType = "ambulance" | "disaster";

/** Operating sector. See VehicleOwnership in apps/core/enums.py. */
export type VehicleOwnership =
  | "government" | "private_hospital" | "private_service" | "ngo";

export interface VehiclePayload {
  id: number;
  uuid: string;
  callsign: string;
  /** Road registration plate. Blank on vehicles imported without one. */
  registration?: string;
  vehicle_type: VehicleType;
  vehicle_type_display?: string;
  ownership?: VehicleOwnership;
  ownership_display?: string;
  /** Fitness for dispatch — see VehicleReadiness. */
  readiness?: VehicleReadiness;
  readiness_display?: string;
  /** Operating agency, e.g. "108 Emergency Services". */
  operator?: string;
  is_als?: boolean;
  status: VehicleStatus;
  status_display?: string;
  latitude: number;
  longitude: number;
  heading_deg: number;
  speed_kmh: number;
  priority_level: PriorityLevel;
  siren_mode: string;
  light_pattern: string;
  last_seen_at: string | null;
  is_stale: boolean;
  distance_m?: number;
}

export interface LiveVehicles {
  generated_at: string;
  count: number;
  vehicles: VehiclePayload[];
}

// ---------------------------------------------------------------------------
// Dispatch (Layers 3 & 6)
// ---------------------------------------------------------------------------
export type TripStage =
  | "created" | "to_scene" | "on_scene" | "to_hospital"
  | "arrived" | "handover" | "cancelled";

export interface RoutePlanSummary {
  id: number;
  is_active: boolean;
  algorithm: string;
  reason: string;
  /** `[[lat, lon], ...]` - Leaflet's ordering, not GeoJSON's. */
  geometry: [number, number][];
  total_distance_m: number;
  total_duration_s: number;
  computed_at: string;
  predicted_eta: string | null;
}

/**
 * An ad-hoc route from /api/v1/brain/route/.
 *
 * Distinct from `RoutePlanSummary`, which is a route the platform has
 * committed a trip to. This one is a preview: it has no id and nothing is
 * dispatched on the strength of it.
 */
export interface RoutePreview {
  algorithm: string;
  /** `[[lat, lon], ...]` - Leaflet's ordering, not GeoJSON's. */
  geometry: [number, number][];
  total_distance_m: number;
  total_duration_s: number;
  total_duration_min: number;
  eta: string | null;
  signalised_nodes: { intersection_id: number; eta_offset_s: number }[];
}

/**
 * Why the platform would - or would not - move a trip onto another road.
 *
 * Mirrors `RerouteDecision.as_dict` in apps/brain/rerouting.py. `blocked` and
 * `congested` are separate because the driver console words them differently:
 * a closure is a fact, congestion is a judgement the crew may overrule.
 */
export interface RerouteCheck {
  should_reroute: boolean;
  reason: string;
  gain_s: number;
  blocked: boolean;
  congested: boolean;
}

/** What one movement sweep did. See apps/dispatch/journey.py. */
export interface JourneyTick {
  considered: number;
  moved: number;
  journeys: {
    trip_id: number;
    reference: string;
    travelled_m: number;
    remaining_m: number;
    stage: string;
  }[];
  demo_completed?: number;
  demo?: { started: string[]; running: string[]; total: number };
}

export interface Trip {
  id: number;
  uuid: string;
  reference: string;
  vehicle: number;
  vehicle_callsign: string;
  vehicle_type: VehicleType;
  vehicle_latitude: number;
  vehicle_longitude: number;
  vehicle_speed_kmh: number;
  stage: TripStage;
  stage_display: string;
  emergency_category: string;
  category_display: string;
  hospital_code: string | null;
  hospital_name: string | null;
  destination_latitude: number | null;
  destination_longitude: number | null;
  priority_level: PriorityLevel;
  siren_mode: string;
  light_pattern: string;
  eta: string | null;
  distance_remaining_m: number | null;
  active_route: RoutePlanSummary | null;
  /** Observed symptoms. Clinical data, so redacted like patient_notes. */
  symptoms: SymptomCode[] | null;
  symptom_labels: string[] | null;
  hospital_choice_reason: HospitalChoiceReason;
  choice_reason_display: string;
  hospital_choice_note: string;
  /** Null when the caller's role lacks clinical clearance - see below. */
  patient_age: number | null;
  patient_notes: string | null;
  patient_deteriorating: boolean | null;
  incident_address: string | null;
  caller_number: string | null;
  /** Server sets this when PHI has been withheld. Render accordingly. */
  clinical_data_redacted?: boolean;
  response_time_s: number | null;
  transport_time_s: number | null;
  created_at: string;
}

export type PreemptionState =
  | "planned" | "armed" | "active" | "released" | "cancelled" | "failed";

export interface Preemption {
  id: number;
  trip: number;
  signal: number;
  controller_id: string;
  intersection: string;
  latitude: number;
  longitude: number;
  state: PreemptionState;
  planned_green_at: string;
  planned_release_at: string;
  activated_at: string | null;
  released_at: string | null;
  hold_duration_s: number;
  eta_error_s: number | null;
  reason: string;
}

// ---------------------------------------------------------------------------
// Hospitals (Layer 5)
// ---------------------------------------------------------------------------
export interface HospitalCapacity {
  emergency_beds_total: number;
  emergency_beds_available: number;
  icu_beds_total: number;
  icu_beds_available: number;
  ventilators_available: number;
  operation_theatres_free: number;
  patients_waiting: number;
  doctors_on_duty: number;
  reported_at: string;
  workload_index: number;
  is_stale: boolean;
}

export interface Hospital {
  id: number;
  code: string;
  name: string;
  city: string;
  latitude: number;
  longitude: number;
  emergency_phone: string;
  is_active: boolean;
  is_on_diversion: boolean;
  diversion_reason: string;
  is_trauma_designated: boolean;
  facility_codes: string[];
  capacity: HospitalCapacity | null;
}

// ---------------------------------------------------------------------------
// Hospital portal (the receiving hospital's own console)
// ---------------------------------------------------------------------------
/** Derived, never declared - see HospitalCapacity.status. */
export type HospitalStatus = "ready" | "busy" | "full";

export interface TeamRow {
  field: string;
  label: string;
  ready: boolean;
}

export interface BedRow {
  key: string;
  label: string;
  available: number;
  total: number;
}

export interface HospitalDashboard {
  hospital: {
    id: number;
    code: string;
    name: string;
    city: string;
    emergency_phone: string;
    is_trauma_designated: boolean;
    is_on_diversion: boolean;
    diversion_reason: string;
    latitude: number;
    longitude: number;
  };
  status: HospitalStatus;
  active_ambulances_coming: number;
  emergency_cases_today: number;
  available_beds: number;
  available_icu_beds: number;
  available_ventilators: number;
  available_operation_theatres: number;
  emergency_staff_on_duty: number;
  teams: TeamRow[];
  teams_ready: number;
  teams_total: number;
  beds: BedRow[];
  workload_index: number;
  patients_waiting: number;
  doctors_on_duty: number;
  reported_at: string;
  is_stale: boolean;
}

/** Clinical block, or `{ redacted: true }` for a role without clearance. */
export interface InboundPatient {
  redacted: boolean;
  emergency_category?: string;
  assessment?: string;
  symptoms?: string[];
  priority_level?: PriorityLevel;
  patient_age?: number | null;
  deteriorating?: boolean | null;
  eta?: string | null;
}

export interface InboundAmbulance {
  /** Journey completion, for the board's progress bar. */
  progress: { percent: number; total_m: number | null; is_moving: boolean };
  trip_id: number;
  reference: string;
  ambulance_number: string;
  registration: string;
  driver_name: string | null;
  paramedic_name: string | null;
  current_location: {
    latitude: number;
    longitude: number;
    heading_deg: number;
    speed_kmh: number;
  };
  eta: string | null;
  distance_remaining_m: number | null;
  current_status: string;
  stage: TripStage;
  vehicle_status: string;
  emergency_level: PriorityLevel;
  patient_category: string;
  emergency_category: string;
  patient: InboundPatient;
  /** `[[lat, lon], ...]` for the per-ambulance navigation view. */
  route_geometry: [number, number][];
  destination: { latitude: number | null; longitude: number | null };
  has_arrived: boolean;
}

export interface InboundBoard {
  hospital: { id: number; code: string; name: string };
  count: number;
  ambulances: InboundAmbulance[];
  breakdowns: Breakdown[];
}

/** Everything the Updates tab edits. Mirrors CapacityUpdateSerializer. */
export interface HospitalEditableCapacity {
  emergency_cases_today: number;
  emergency_beds_total: number;
  emergency_beds_available: number;
  icu_beds_total: number;
  icu_beds_available: number;
  general_beds_total: number;
  general_beds_available: number;
  pediatric_beds_total: number;
  pediatric_beds_available: number;
  burn_unit_beds_total: number;
  burn_unit_beds_available: number;
  cardiac_icu_total: number;
  cardiac_icu_available: number;
  ventilators_total: number;
  ventilators_available: number;
  operation_theatres_total: number;
  operation_theatres_free: number;
  emergency_staff_on_duty: number;
  doctors_on_duty: number;
  patients_waiting: number;
}

export interface HospitalUpdateForm {
  hospital: { id: number; code: string; name: string };
  capacity: HospitalEditableCapacity;
  teams: Record<string, boolean>;
  team_rows: TeamRow[];
  dashboard?: HospitalDashboard;
}

/** One resource an admission will occupy. See apps/hospitals/admission.py. */
export interface AdmissionResource {
  field: string;
  label: string;
  units: number;
  reason: string;
  /** Present only on the applied result. */
  before?: number;
  after?: number;
  /** Present only on a shortfall. */
  available?: number;
}

export interface AdmissionPlan {
  resources: AdmissionResource[];
  notes: string[];
}

export interface AdmissionResult {
  trip: string;
  applied: AdmissionResource[];
  notes: string[];
  dashboard: HospitalDashboard;
}

/**
 * The crew board behind the admin's Drivers and Paramedics tabs.
 *
 * One shape for both seats: the pairing is a property of the shift, so two
 * separate payloads would be two chances to disagree about who is crewing with
 * whom.
 */
export interface CrewMember {
  id: number;
  username: string;
  name: string;
  email: string;
  staff_id: string;
  qualification: string;
  base_station: string;
  phone: string;
  blood_group: string;
  avatar_url: string | null;
  on_duty: boolean;
  /**
   * No open shift at all.
   *
   * Not `!on_duty`: a driver mid-takeover — vehicle claimed, inspection
   * running — is neither on duty nor off it, and the Off Duty board must not
   * claim somebody at work is at home.
   */
  off_duty: boolean;
  shift_status: string;
  vehicle: string | null;
  vehicle_registration: string;
  partner: string | null;
  on_duty_since: string | null;
  mission: string | null;
  mission_category: string | null;
  mission_stage: string | null;
  mission_hospital: string | null;
  mission_eta: string | null;
  mission_priority: PriorityLevel | null;
  /**
   * Where this person is in their shift right now, in one phrase.
   *
   * Derived server-side from the shift and the live trip on every read, never
   * stored: Available, On Duty, On Route, On Scene, Treating Patient,
   * Transporting, At Hospital, Shift Ended, Off Duty. See `_live_status` in
   * apps/fleet/crew_api.py — that function owns the vocabulary.
   */
  status: string;
  monitoring: {
    latitude: number | null;
    longitude: number | null;
    speed_kmh: number | null;
    heading_deg: number | null;
    last_seen_at: string | null;
    is_stale: boolean | null;
    vehicle_readiness: string | null;
    inspection: string | null;
    distance_remaining_m: number | null;
  };
}

export interface CrewRoster {
  generated_at: string;
  drivers: CrewMember[];
  paramedics: CrewMember[];
}

export interface HospitalChoice {
  id: number;
  code: string;
  name: string;
  city: string;
}

export interface EmergencyRuleSummary {
  category: string;
  display_name: string;
  required_facilities: string[];
  default_priority_level: PriorityLevel;
  guidance: string;
}

export interface RecommendationRule {
  category: string;
  display_name: string;
  required_facilities: string[];
  preferred_facilities: string[];
  priority_level: PriorityLevel;
  requires_icu: boolean;
  golden_window_min: number | null;
  time_critical: boolean;
  guidance: string;
}

export interface HospitalCandidate {
  hospital_id: number;
  code: string;
  name: string;
  latitude: number;
  longitude: number;
  distance_km: number;
  travel_time_min: number | null;
  score: number;
  factors: Record<string, number>;
  eligible: boolean;
  exclusion_reason: string;
  warnings: string[];
  missing_facilities: string[];
  within_golden_window: boolean | null;
  emergency_beds_available: number;
  icu_beds_available: number;
  workload_index: number;
  is_on_diversion: boolean;
}

/** What the crew observed. See apps/core/enums.py PatientSymptom. */
export type SymptomCode =
  | "unconscious" | "bleeding" | "breathing_difficulty" | "seizure" | "vomiting"
  | "fracture" | "burns" | "paralysis" | "chest_pain" | "fever";

export interface SymptomSpec {
  code: SymptomCode;
  label: string;
  priority_level: PriorityLevel;
  /** Facilities this observation alone makes mandatory. */
  required_facilities: string[];
  note: string;
}

/** What a set of observations implies, echoed back by /recommend/. */
export interface SymptomAssessment {
  symptoms: SymptomCode[];
  labels: string[];
  required_facilities: string[];
  preferred_facilities: string[];
  priority_level: PriorityLevel;
  notes: string[];
}

export interface Recommendation {
  rule: RecommendationRule;
  recommended: HospitalCandidate | null;
  candidates: HospitalCandidate[];
  considered: number;
  eligible_count: number;
  relaxed: boolean;
  relaxation_note: string;
  symptom_assessment?: SymptomAssessment;
}

/** Why a hospital was chosen. See HospitalChoiceReason in enums.py. */
export type HospitalChoiceReason =
  | "recommended" | "patient_request" | "family_request"
  | "clinical_judgement" | "capacity" | "continuity";

// ---------------------------------------------------------------------------
// Crew shift & the start-of-shift vehicle check
// ---------------------------------------------------------------------------
/** `draft` = vehicle claimed, inspection running, no paramedic asked yet. */
export type ShiftStatus = "draft" | "pending" | "active" | "declined" | "ended";

export interface CrewPerson {
  id: number;
  username: string;
  name: string;
}

export interface EquipmentItemSpec {
  code: string;
  label: string;
  group: string;
  /** Absence makes the vehicle unfit for a Level 1 response. */
  critical: boolean;
}

export interface EquipmentAnswer {
  present: boolean;
  note?: string;
}

export interface EquipmentCheckPayload {
  id: number;
  shift_id: number;
  items: Record<string, EquipmentAnswer>;
  answered: number;
  total: number;
  is_complete: boolean;
  /** Still owed - either skipped, or started and not finished. */
  is_outstanding: boolean;
  missing: string[];
  missing_critical: string[];
  /** Derived from the answers, never asserted — see EquipmentCheck.derived_readiness. */
  readiness: VehicleReadiness;
  /** Maintenance triage categories implied by whatever failed. */
  failed_reasons: FailureReason[];
  skipped: boolean;
  skip_reason: string;
  skipped_at: string | null;
  completed_at: string | null;
  completed_by: string | null;
  notes: string;
}

export interface CrewShift {
  id: number;
  uuid: string;
  vehicle: number;
  vehicle_callsign: string;
  vehicle_registration: string;
  driver: number;
  driver_detail: CrewPerson | null;
  paramedic: number;
  paramedic_detail: CrewPerson | null;
  status: ShiftStatus;
  status_display: string;
  requested_at: string;
  accepted_at: string | null;
  ended_at: string | null;
  decline_reason: string;
  equipment_check: EquipmentCheckPayload | null;
}

/**
 * A skipped readiness check that has come due.
 *
 * Null while the crew are still running the emergency the check was skipped
 * for - the skip buys that one response. Non-null once it has ended, at which
 * point the server refuses to open another until the 21 items are answered.
 */
export interface ChecklistDue {
  detail: string;
  checklist_outstanding: true;
  answered: number;
  total: number;
  skip_reason: string;
  skipped_at: string | null;
}

export interface MyShift {
  shift: CrewShift | null;
  awaiting_my_acceptance: CrewShift[];
  role_hint: "driver" | "paramedic";
  checklist_due?: ChecklistDue | null;
}

// ---------------------------------------------------------------------------
// Driver module: readiness, maintenance, breakdown transfer, fleet board
// ---------------------------------------------------------------------------
/** Fitness for dispatch. Distinct from VehicleStatus, which is what it's doing. */
export type VehicleReadiness =
  | "unchecked" | "ready" | "temporarily_ready" | "not_ready" | "maintenance";

export type FailureReason =
  | "engine" | "battery" | "tyres" | "brakes" | "gps" | "siren"
  | "emergency_lights" | "oxygen" | "medical_equipment" | "other";

export type MaintenanceState = "open" | "acknowledged" | "in_progress" | "resolved";
export type BreakdownState =
  | "open" | "transfer_accepted" | "transfer_complete" | "resolved" | "cancelled";
export type TransferOfferState = "offered" | "accepted" | "rejected" | "withdrawn";

/** The extra fields a checklist save returns beyond the check itself. */
export interface ReadinessOutcome {
  vehicle_readiness: VehicleReadiness;
  vehicle_readiness_display: string;
  /** False when a failed critical item has grounded the vehicle. */
  may_dispatch: boolean;
  maintenance_report: MaintenanceReport | null;
}

export interface MaintenanceReport {
  id: number;
  uuid: string;
  vehicle: string;
  registration: string;
  reasons: FailureReason[];
  failed_items: string[];
  remarks: string;
  state: MaintenanceState;
  state_display: string;
  from_inspection: boolean;
  reported_by: string | null;
  reported_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  is_open: boolean;
}

export interface Breakdown {
  id: number;
  uuid: string;
  state: BreakdownState;
  state_display: string;
  vehicle: string;
  registration: string;
  latitude: number;
  longitude: number;
  reasons: FailureReason[];
  remarks: string;
  reported_at: string;
  trip_id: number;
  reference: string;
  emergency_category: string;
  emergency_category_display: string;
  priority_level: PriorityLevel;
  destination_hospital: string | null;
  destination_latitude: number | null;
  destination_longitude: number | null;
  replacement: string | null;
  accepted_at: string | null;
  transferred_at: string | null;
  offers?: TransferOffer[];
}

export interface TransferOffer {
  id: number;
  breakdown_id: number;
  vehicle: string;
  distance_m: number;
  state: TransferOfferState;
  state_display: string;
  responded_at: string | null;
  reject_reason: string;
  /** Present on /breakdowns/offers/ so a crew can decide without a second call. */
  breakdown?: Breakdown;
}

/** One row of the admin fleet board. */
export interface FleetRow extends VehiclePayload {
  readiness: VehicleReadiness;
  readiness_display: string;
  /** A permanently-running demonstration unit — never takeable by a driver. */
  is_demo: boolean;
  /** `no_shift`, or one of draft / pending / active. Draft counts as crewed. */
  shift_status: string;
  shift_status_display: string;
  driver_name: string | null;
  paramedic_name: string | null;
  on_duty_since: string | null;
  inspection_status: string;
  current_trip_reference: string | null;
  current_emergency: string | null;
  current_priority_level: PriorityLevel | null;
  current_destination: string | null;
  current_eta: string | null;
  updated_at: string;
}

export interface FleetBoard {
  generated_at: string;
  count: number;
  vehicles: FleetRow[];
  summary: {
    total: number;
    ready: number;
    temporarily_ready: number;
    not_ready: number;
    maintenance: number;
    on_duty: number;
    on_call: number;
    inspection_pending: number;
  };
}

/** An ambulance a driver may take over right now. */
export interface SelectableVehicle extends VehiclePayload {
  home_station: string | null;
  last_inspected_at: string | null;
}

// ---------------------------------------------------------------------------
// Staff identity
// ---------------------------------------------------------------------------
export interface StaffProfile {
  username: string;
  name: string;
  first_name: string;
  last_name: string;
  email: string;
  staff_id: string;
  phone: string;
  qualification: string;
  base_station: string;
  blood_group: string;
  emergency_contact: string;
  avatar_url: string | null;
  roles?: string[];
  role_labels?: string[];
  is_paramedic?: boolean;
  is_driver?: boolean;
}

/** Seeded sign-in credentials, shown on the login screen in DEBUG only. */
export interface DemoAccount {
  username: string;
  password: string;
  name: string;
  roles: string[];
  role_labels: string[];
  is_paramedic: boolean;
  is_driver: boolean;
  is_hospital: boolean;
  /** The ward a hospital login opens. Blank for every other role. */
  hospital: string;
  staff_id: string;
  qualification: string;
  base_station: string;
}

// ---------------------------------------------------------------------------
// Network & alerts
// ---------------------------------------------------------------------------
export interface RoadEvent {
  id: number;
  event_type: string;
  event_type_display: string;
  description: string;
  severity: number;
  confidence: number;
  source: string;
  is_active: boolean;
  latitude: number;
  longitude: number;
  blocks_road: boolean;
}

export interface SegmentFeature {
  type: "Feature";
  geometry: { type: "LineString"; coordinates: [number, number][] };
  properties: {
    id: number;
    name: string;
    road_class: string;
    congestion_level: string;
    congestion_index: number;
    speed_kmh: number | null;
    is_open: boolean;
  };
}

export interface SegmentCollection {
  type: "FeatureCollection";
  features: SegmentFeature[];
}

export interface DriverAlert {
  id: number;
  uuid: string;
  trip_id: number;
  message: string;
  instruction: string;
  eta_seconds: number;
  latitude: number;
  longitude: number;
  radius_m: number;
  approach_bearing_deg: number;
  priority_level: PriorityLevel;
  expires_at: string;
}

export interface DisplayBoardLive {
  code: string;
  name: string;
  latitude: number;
  longitude: number;
  message: string;
  expires_at: string | null;
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------
export interface DurationStats {
  count: number;
  avg_s: number | null;
  avg_min: number | null;
  median_s: number | null;
  p90_s: number | null;
  best_s: number | null;
  worst_s: number | null;
}

export interface AnalyticsSummary {
  generated_at: string;
  response_times: {
    window_days: number;
    trips: number;
    completed: number;
    cancelled: number;
    response_time: DurationStats;
    transport_time: DurationStats;
    total_time: DurationStats;
  };
  corridor_usage: {
    preemptions_requested: number;
    activated: number;
    failed: number;
    yielded: number;
    total_hold_seconds: number;
    avg_hold_seconds: number | null;
    trips_with_corridor: number;
    eta_accuracy: {
      samples: number;
      mean_abs_error_s: number | null;
      p90_abs_error_s: number | null;
    };
  };
  congestion_hotspots: Array<{
    segment_id: number; name: string; latitude: number; longitude: number;
    samples: number; avg_speed_kmh: number; heavy_share: number; score: number;
  }>;
  high_delay_intersections: Array<{
    intersection_id: number; name: string; controller_id: string;
    latitude: number; longitude: number; events: number;
    avg_hold_s: number; avg_clearance_s: number; avg_eta_error_s: number; score: number;
  }>;
  movement: {
    trips: number;
    trips_by_category: Record<string, number>;
    trips_by_priority_level: Record<string, number>;
    reroutes: number;
    planned_distance_km: number;
    telemetry_points: number;
    hospital_overrides: number;
    fleet: { total: number; online: number; on_mission: number; available: number };
  };
}

export interface Hotspot {
  id: number;
  kind: string;
  label: string;
  latitude: number;
  longitude: number;
  incident_count: number;
  score: number;
}

// ---------------------------------------------------------------------------
// Service info & WebSocket envelope
// ---------------------------------------------------------------------------
export interface ServiceInfo {
  platform: string;
  api_version: string;
  layers: Record<string, string>;
  backends: {
    database: string;
    spatial: { backend: string; postgis: boolean; postgis_version?: string };
    channel_layer: string;
    routing_algorithm: string;
    congestion_predictor: string;
    computer_vision: string;
    traffic_provider: string;
  };
  websockets: Record<string, string>;
}

export interface Viewer {
  authenticated: boolean;
  username: string | null;
  roles: Role[];
  clinical_access?: boolean;
  auth_method?: string;
}

/** Every socket message shares this envelope; see apps/core/consumers.py. */
export interface SocketEvent<T = unknown> {
  event: string;
  data: T;
}

export interface Paginated<T> {
  count?: number;
  results?: T[];
}

/** DRF returns a bare array or a paginated envelope depending on the view. */
export function unwrap<T>(payload: Paginated<T> | T[] | undefined | null): T[] {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  return payload.results ?? [];
}

// ---------------------------------------------------------------------------
// Notifications and push (Phase 9)
// ---------------------------------------------------------------------------
export type NotificationSeverity = "info" | "success" | "warning" | "critical";

export interface NotificationRecord {
  uuid: string;
  title: string;
  body: string;
  severity: NotificationSeverity;
  severity_label: string;
  category: string;
  category_label: string;
  link: string;
  dedupe_key: string;
  context: Record<string, unknown>;
  created_at: string;
  delivered_count: number;
  failed_count: number;
  is_read: boolean;
}

export interface Inbox {
  notifications: NotificationRecord[];
  unread: number;
  window_hours: number;
}

export interface NotificationPreferences {
  muted_categories: string[];
  quiet_hours_start: number | null;
  quiet_hours_end: number | null;
  push_enabled: boolean;
  available_categories: { value: string; label: string }[];
  critical_always_delivered: boolean;
  note: string;
}

export interface PushSubscriptionSummary {
  id: number;
  backend: string;
  endpoint_hint: string;
  user_agent: string;
  is_active: boolean;
  failure_count: number;
  is_healthy: boolean;
  last_success_at: string | null;
  last_failure_reason: string;
  created_at: string;
}

export interface PushTestResult {
  notification_id: string | null;
  attempted: number;
  delivered: number;
  failed: number;
  skipped: number;
  truncated: boolean;
  backend_note: string;
}

// ---------------------------------------------------------------------------
// Analytics charts (Phase 10)
// ---------------------------------------------------------------------------
export interface SeriesSpec {
  key: string;
  label: string;
  unit: string;
  /** "count" -> a missing day is 0. "measure" -> a missing day is null. */
  kind: "count" | "measure";
  colour: string;
  /** null = neither direction is an improvement (demand volume). */
  higher_is_better: boolean | null;
  description: string;
}

/** One day. Metric keys are dynamic, hence the index signature. */
export interface DailyPoint {
  date: string;
  label: string;
  [metric: string]: string | number | null;
}

export interface DailyTrends {
  window_days: number;
  start: string;
  end: string;
  city: string;
  points: DailyPoint[];
  series: SeriesSpec[];
  default_series: string[];
  materialised_days: number;
  computed_live_days: number;
}

export interface TrendMetric {
  key: string;
  label: string;
  unit: string;
  previous: number | null;
  current: number | null;
  change_pct: number | null;
  improving: boolean | null;
  higher_is_better: boolean | null;
}

export interface TrendSummary {
  window_days: number;
  comparable: boolean;
  split_at?: string;
  metrics: TrendMetric[];
}

export interface DemandProfile {
  window_days: number;
  trips: number;
  hours: {
    hour: number; label: string; trips: number; share: number;
    avg_response_s: number | null; level_1: number;
  }[];
  weekdays: { weekday: number; label: string; trips: number; share: number }[];
  peak_hour: string | null;
  timezone: string;
}

export interface Slice {
  key: string;
  label: string;
  value: number;
  colour: string;
}

export interface CategoryDistribution {
  window_days: number;
  total: number;
  categories: Slice[];
  levels: Slice[];
}

export interface CorridorOutcomes {
  window_days: number;
  points: {
    date: string; label: string; activated: number; yielded: number;
    failed: number; cancelled: number; pending: number;
  }[];
  legend: { key: string; label: string; colour: string }[];
}

export interface ResponseDistribution {
  window_days: number;
  samples: number;
  buckets: {
    label: string; low_min: number; high_min: number | null;
    count: number; share: number; within_target: boolean;
  }[];
  target_minutes: number;
  within_target: number;
  within_target_share: number | null;
  median_s: number | null;
  p90_s: number | null;
}

export interface HospitalLoad {
  window_days: number;
  hospitals: {
    code: string; name: string; trips: number; overrides: number;
    override_share: number; avg_transport_s: number | null;
  }[];
  total_routed: number;
  truncated: boolean;
}

export interface ExportDataset {
  key: string;
  title: string;
  description: string;
  columns: string[];
  url: string;
}
