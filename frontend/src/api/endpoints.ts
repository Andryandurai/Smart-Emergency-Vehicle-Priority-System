/**
 * Every SEVPS endpoint the console consumes, in one place.
 *
 * Components never build URLs. Keeping them here means the API surface the
 * frontend depends on is greppable in a single file - which is what makes it
 * possible to tell, before changing a serializer, whether the console cares.
 */
import { api } from "./client";
import type {
  AnalyticsSummary,
  CurrentUser,
  DisplayBoardLive,
  DriverAlert,
  EmergencyRuleSummary,
  Hospital,
  HospitalCapacity,
  Hotspot,
  LiveVehicles,
  Paginated,
  Preemption,
  Recommendation,
  RoadEvent,
  RoleDescriptor,
  SegmentCollection,
  ServiceInfo,
  TokenPair,
  Trip,
} from "./types";
import { unwrap } from "./types";

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export const auth = {
  login: (username: string, password: string) =>
    api.raw<TokenPair>("/api/v1/auth/jwt/create/", { username, password }),
  logout: () => api.post<{ detail: string }>("/api/v1/auth/jwt/logout/", {}),
  me: (signal?: AbortSignal) => api.get<CurrentUser>("/api/v1/auth/me/", signal),
  roles: () => api.get<{ roles: RoleDescriptor[] }>("/api/v1/auth/roles/"),
};

// ---------------------------------------------------------------------------
// Service
// ---------------------------------------------------------------------------
export const service = {
  info: () => api.get<ServiceInfo>("/api/v1/info/"),
  health: () => api.get<{ status: string; checks: Record<string, string> }>("/api/v1/health/"),
};

// ---------------------------------------------------------------------------
// Fleet (Layer 1)
// ---------------------------------------------------------------------------
export const fleet = {
  live: (signal?: AbortSignal) => api.get<LiveVehicles>("/api/v1/fleet/vehicles/live/", signal),
  telemetry: (
    vehicleId: number,
    fix: { latitude: number; longitude: number; speed_kmh?: number; heading_deg?: number },
  ) => api.post<unknown>(`/api/v1/fleet/vehicles/${vehicleId}/telemetry/`, fix),
};

// ---------------------------------------------------------------------------
// Dispatch (Layers 3 & 6)
// ---------------------------------------------------------------------------
export const dispatch = {
  liveTrips: (signal?: AbortSignal) =>
    api.get<{ count: number; trips: Trip[] }>("/api/v1/dispatch/trips/live/", signal),

  tripsForVehicle: async (callsign: string): Promise<Trip[]> =>
    unwrap(
      await api.get<Paginated<Trip>>(
        `/api/v1/dispatch/trips/?vehicle=${encodeURIComponent(callsign)}&active=1`,
      ),
    ),

  openPreemptions: async (signal?: AbortSignal): Promise<Preemption[]> =>
    unwrap(
      await api.get<Paginated<Preemption>>(
        "/api/v1/dispatch/preemptions/?open=1&limit=200",
        signal,
      ),
    ),

  corridor: (tripId: number) =>
    api.get<{ corridor: Preemption[] }>(`/api/v1/dispatch/trips/${tripId}/corridor/`),

  assess: (
    tripId: number,
    body: {
      emergency_category: string;
      patient_age?: number;
      patient_notes?: string;
      patient_deteriorating?: boolean;
      hospital_id?: number;
      override_reason?: string;
    },
  ) =>
    api.post<{ trip: Trip; recommendation: Recommendation | null; corridor: Preemption[] }>(
      `/api/v1/dispatch/trips/${tripId}/assess/`,
      body,
    ),

  setStage: (tripId: number, stage: string, reason = "") =>
    api.post<Trip>(`/api/v1/dispatch/trips/${tripId}/stage/`, { stage, reason }),

  handover: (tripId: number) => api.post<Trip>(`/api/v1/dispatch/trips/${tripId}/handover/`, {}),

  create: (body: {
    incident_latitude: number;
    incident_longitude: number;
    emergency_category: string;
    incident_address?: string;
    caller_number?: string;
    vehicle_callsign?: string;
    auto_assign?: boolean;
  }) => api.post<Trip>("/api/v1/dispatch/trips/", body),

  cancel: (tripId: number, reason: string) =>
    api.post<Trip>(`/api/v1/dispatch/trips/${tripId}/cancel/`, { reason }),

  releaseCorridor: (tripId: number, reason: string) =>
    api.post<unknown>(`/api/v1/dispatch/trips/${tripId}/corridor/release/`, { reason }),
};

// ---------------------------------------------------------------------------
// Hospitals (Layer 5)
// ---------------------------------------------------------------------------
export const hospitals = {
  list: async (signal?: AbortSignal): Promise<Hospital[]> =>
    unwrap(await api.get<Paginated<Hospital>>("/api/v1/hospitals/hospitals/?limit=200", signal)),

  detail: (id: number) => api.get<Hospital>(`/api/v1/hospitals/hospitals/${id}/`),

  inbound: async (id: number): Promise<Trip[]> =>
    unwrap(await api.get<Paginated<Trip>>(`/api/v1/hospitals/hospitals/${id}/inbound/`)),

  updateCapacity: (id: number, body: Partial<HospitalCapacity>) =>
    api.patch<HospitalCapacity>(`/api/v1/hospitals/hospitals/${id}/capacity/`, body),

  setDiversion: (id: number, isOnDiversion: boolean, reason = "") =>
    api.post<Hospital>(`/api/v1/hospitals/hospitals/${id}/diversion/`, {
      is_on_diversion: isOnDiversion,
      reason,
    }),

  recommend: (latitude: number, longitude: number, emergencyCategory: string) =>
    api.post<Recommendation>("/api/v1/hospitals/recommend/", {
      latitude,
      longitude,
      emergency_category: emergencyCategory,
    }),

  ruleCatalogue: () => api.get<EmergencyRuleSummary[]>("/api/v1/hospitals/rules/catalogue/"),

  acknowledgeAlert: (alertId: number, body: { acknowledged_by?: string; preparation_notes?: string }) =>
    api.post<unknown>(`/api/v1/hospitals/alerts/${alertId}/acknowledge/`, body),
};

// ---------------------------------------------------------------------------
// Network & alerts
// ---------------------------------------------------------------------------
export const network = {
  segments: (limit = 4000, signal?: AbortSignal) =>
    api.get<SegmentCollection>(`/api/v1/network/segments/geojson/?limit=${limit}`, signal),

  activeEvents: async (signal?: AbortSignal): Promise<RoadEvent[]> =>
    unwrap(await api.get<Paginated<RoadEvent>>("/api/v1/network/events/?active=1&limit=100", signal)),
};

export const alerts = {
  boards: (signal?: AbortSignal) =>
    api.get<DisplayBoardLive[]>("/api/v1/alerts/boards/live/", signal),

  nearby: (latitude: number, longitude: number, radiusM = 1500) =>
    api.get<{ alerts: DriverAlert[] }>(
      `/api/v1/alerts/nearby/?lat=${latitude}&lon=${longitude}&radius_m=${radiusM}`,
    ),
};

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------
export const analytics = {
  summary: (days = 30, signal?: AbortSignal) =>
    api.get<AnalyticsSummary>(`/api/v1/analytics/summary/?days=${days}`, signal),

  accidentHotspots: (signal?: AbortSignal) =>
    api.get<{ hotspots: Hotspot[] }>("/api/v1/analytics/accident-hotspots/", signal),
};
