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

export type VehicleType = "ambulance" | "fire_engine" | "police" | "disaster";

export interface VehiclePayload {
  id: number;
  uuid: string;
  callsign: string;
  vehicle_type: VehicleType;
  status: VehicleStatus;
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

export interface Recommendation {
  rule: RecommendationRule;
  recommended: HospitalCandidate | null;
  candidates: HospitalCandidate[];
  considered: number;
  eligible_count: number;
  relaxed: boolean;
  relaxation_note: string;
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
