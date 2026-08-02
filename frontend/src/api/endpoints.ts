/**
 * Every SEVPS endpoint the console consumes, in one place.
 *
 * Components never build URLs. Keeping them here means the API surface the
 * frontend depends on is greppable in a single file - which is what makes it
 * possible to tell, before changing a serializer, whether the console cares.
 */
import { ApiError, api, getAccessToken } from "./client";
import type {
  AnalyticsSummary,
  CategoryDistribution,
  CorridorOutcomes,
  CurrentUser,
  DailyTrends,
  DemandProfile,
  DisplayBoardLive,
  DriverAlert,
  EmergencyRuleSummary,
  Hospital,
  HospitalCapacity,
  ExportDataset,
  HospitalLoad,
  Hotspot,
  Inbox,
  LiveVehicles,
  NotificationPreferences,
  Paginated,
  Preemption,
  PushSubscriptionSummary,
  PushTestResult,
  Recommendation,
  ResponseDistribution,
  RoadEvent,
  RoleDescriptor,
  SegmentCollection,
  ServiceInfo,
  TokenPair,
  TrendSummary,
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

// ---------------------------------------------------------------------------
// Notifications (Phase 9)
// ---------------------------------------------------------------------------
export const notify = {
  inbox: (signal?: AbortSignal) => api.get<Inbox>("/api/v1/notify/inbox/", signal),

  markRead: (uuid?: string) =>
    api.post<{ read: number }>(
      uuid ? `/api/v1/notify/read/${uuid}/` : "/api/v1/notify/read/",
      {},
    ),

  preferences: (signal?: AbortSignal) =>
    api.get<NotificationPreferences>("/api/v1/notify/preferences/", signal),

  savePreferences: (body: Partial<NotificationPreferences>) =>
    api.patch<NotificationPreferences>("/api/v1/notify/preferences/", body),

  subscriptions: (signal?: AbortSignal) =>
    api.get<{ subscriptions: PushSubscriptionSummary[]; push_configured: boolean }>(
      "/api/v1/notify/subscriptions/",
      signal,
    ),

  sendTest: () => api.post<PushTestResult>("/api/v1/notify/test/", {}),
};

// ---------------------------------------------------------------------------
// Analytics charts (Phase 10)
// ---------------------------------------------------------------------------
export const charts = {
  trends: (days = 30, signal?: AbortSignal) =>
    api.get<DailyTrends>(`/api/v1/analytics/trends/?days=${days}`, signal),

  trendSummary: (days = 30, signal?: AbortSignal) =>
    api.get<TrendSummary>(`/api/v1/analytics/trends/summary/?days=${days}`, signal),

  demand: (days = 30, signal?: AbortSignal) =>
    api.get<DemandProfile>(`/api/v1/analytics/demand/?days=${days}`, signal),

  distribution: (days = 30, signal?: AbortSignal) =>
    api.get<CategoryDistribution>(`/api/v1/analytics/distribution/?days=${days}`, signal),

  corridorOutcomes: (days = 30, signal?: AbortSignal) =>
    api.get<CorridorOutcomes>(`/api/v1/analytics/corridor-outcomes/?days=${days}`, signal),

  responseDistribution: (days = 30, signal?: AbortSignal) =>
    api.get<ResponseDistribution>(`/api/v1/analytics/response-distribution/?days=${days}`, signal),

  hospitalLoad: (days = 30, signal?: AbortSignal) =>
    api.get<HospitalLoad>(`/api/v1/analytics/hospital-load/?days=${days}`, signal),

  exports: (signal?: AbortSignal) =>
    api.get<{ datasets: ExportDataset[] }>("/api/v1/analytics/export/", signal),

  exportUrl: (dataset: string, days: number) =>
    `/api/v1/analytics/export/${dataset}.csv?days=${days}`,

  /**
   * Download an export.
   *
   * Deliberately not a plain `<a href download>`. SEVPS authenticates with a
   * bearer token held in memory, and a browser navigation carries no such
   * header - the refresh cookie is scoped to /api/v1/auth/ and is not a
   * session. Every export link therefore 401'd, which presented as a browser
   * downloading a file called `daily.csv` containing an error page. Found by
   * the Playwright suite in Phase 12.
   *
   * So: fetch with credentials, then hand the browser a blob.
   */
  downloadExport: async (dataset: string, days: number): Promise<void> => {
    const token = getAccessToken();
    const response = await fetch(charts.exportUrl(dataset, days), {
      credentials: "include",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
      throw new ApiError(`Export failed (${response.status})`, response.status);
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filenameFrom(response) ?? `sevps-${dataset}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Revoked on the next tick: revoking synchronously can cancel the
    // download in some browsers before it has read the blob.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  },
};

/** Honour the server's Content-Disposition, which carries the date stamp. */
function filenameFrom(response: Response): string | null {
  const disposition = response.headers.get("Content-Disposition") ?? "";
  return /filename="([^"]+)"/.exec(disposition)?.[1] ?? null;
}
