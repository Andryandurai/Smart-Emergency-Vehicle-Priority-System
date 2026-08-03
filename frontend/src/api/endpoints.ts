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
  Breakdown,
  CategoryDistribution,
  CorridorOutcomes,
  CrewPerson,
  CrewShift,
  CurrentUser,
  DailyTrends,
  DemandProfile,
  DemoAccount,
  DisplayBoardLive,
  DriverAlert,
  EmergencyRuleSummary,
  EquipmentAnswer,
  EquipmentCheckPayload,
  EquipmentItemSpec,
  FailureReason,
  FleetBoard,
  FleetRow,
  Hospital,
  HospitalCapacity,
  ExportDataset,
  HospitalLoad,
  HospitalChoiceReason,
  Hotspot,
  Inbox,
  LiveVehicles,
  MaintenanceReport,
  MyShift,
  NotificationPreferences,
  Paginated,
  Preemption,
  PushSubscriptionSummary,
  PushTestResult,
  ReadinessOutcome,
  Recommendation,
  ResponseDistribution,
  RoadEvent,
  RoleDescriptor,
  RoutePreview,
  SegmentCollection,
  SelectableVehicle,
  ServiceInfo,
  StaffProfile,
  SymptomCode,
  SymptomSpec,
  TokenPair,
  TransferOffer,
  TrendSummary,
  Trip,
  VehicleReadiness,
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

  /** Seeded credentials for the login screen. Empty outside DEBUG. */
  demoAccounts: (signal?: AbortSignal) =>
    api.get<{ accounts: DemoAccount[]; available: boolean }>(
      "/api/v1/auth/demo-accounts/",
      signal,
    ),
};

// ---------------------------------------------------------------------------
// Staff profile — self-service only, there is no user id in any of these URLs
// ---------------------------------------------------------------------------
export const profile = {
  mine: (signal?: AbortSignal) => api.get<StaffProfile>("/api/v1/auth/profile/", signal),

  save: (body: Partial<Pick<StaffProfile, "phone" | "blood_group" | "emergency_contact">>) =>
    api.patch<StaffProfile>("/api/v1/auth/profile/", body),

  /**
   * Upload a profile picture.
   *
   * Sent as multipart with `fetch` rather than through `api.post`, which
   * JSON-encodes its body — a File would arrive as "[object File]".
   */
  uploadAvatar: async (file: File): Promise<StaffProfile> => {
    const form = new FormData();
    form.append("avatar", file);
    const token = getAccessToken();
    const response = await fetch("/api/v1/auth/profile/avatar/", {
      method: "POST",
      credentials: "include",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new ApiError(detail.detail ?? `Upload failed (${response.status})`, response.status);
    }
    return response.json() as Promise<StaffProfile>;
  },

  removeAvatar: () => api.delete<StaffProfile>("/api/v1/auth/profile/avatar/"),
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
// Crew takeover and the start-of-shift vehicle check
// ---------------------------------------------------------------------------
export const shifts = {
  /** My open shift plus any takeover waiting on my acceptance. */
  mine: (signal?: AbortSignal) => api.get<MyShift>("/api/v1/fleet/shifts/mine/", signal),

  /** Who a driver may name as their paramedic - ambulance role only. */
  crew: (signal?: AbortSignal) =>
    api.get<{ crew: CrewPerson[] }>("/api/v1/fleet/shifts/crew/", signal),

  equipmentCatalogue: (signal?: AbortSignal) =>
    api.get<{ items: EquipmentItemSpec[] }>(
      "/api/v1/fleet/shifts/equipment-catalogue/",
      signal,
    ),

  /**
   * Step 1 — the driver takes the vehicle. No paramedic named yet.
   *
   * The inspection happens against this draft shift; only once it passes does
   * the driver call a colleague to the ambulance.
   */
  claim: (vehicleCallsign: string) =>
    api.post<CrewShift>("/api/v1/fleet/shifts/claim/", {
      vehicle_callsign: vehicleCallsign,
    }),

  /** Step 3 — having inspected it, ask a paramedic to crew it. */
  requestParamedic: (shiftId: number, paramedicUsername: string) =>
    api.post<CrewShift>(`/api/v1/fleet/shifts/${shiftId}/request-paramedic/`, {
      paramedic_username: paramedicUsername,
    }),

  /** Claim and request in one call. Superseded by claim + requestParamedic. */
  open: (vehicleCallsign: string, paramedicUsername: string) =>
    api.post<CrewShift>("/api/v1/fleet/shifts/open/", {
      vehicle_callsign: vehicleCallsign,
      paramedic_username: paramedicUsername,
    }),

  accept: (shiftId: number) => api.post<CrewShift>(`/api/v1/fleet/shifts/${shiftId}/accept/`, {}),

  decline: (shiftId: number, reason: string) =>
    api.post<CrewShift>(`/api/v1/fleet/shifts/${shiftId}/decline/`, { reason }),

  end: (shiftId: number) => api.post<CrewShift>(`/api/v1/fleet/shifts/${shiftId}/end/`, {}),

  /** Merges into whatever is already recorded - partial progress is kept. */
  saveChecklist: (
    shiftId: number,
    items: Record<string, EquipmentAnswer>,
    notes?: string,
  ) =>
    api.post<EquipmentCheckPayload & ReadinessOutcome>(
      `/api/v1/fleet/shifts/${shiftId}/checklist/`,
      { items, ...(notes === undefined ? {} : { notes }) },
    ),

  /** Emergency skip: go now, complete the check later. Not a waiver. */
  skipChecklist: (shiftId: number, reason: string) =>
    api.post<EquipmentCheckPayload & ReadinessOutcome>(
      `/api/v1/fleet/shifts/${shiftId}/checklist/skip/`,
      { reason },
    ),

  /** Ambulances this driver may take over: available, uncrewed, not grounded. */
  selectableVehicles: (signal?: AbortSignal) =>
    api.get<{ vehicles: SelectableVehicle[] }>(
      "/api/v1/fleet/shifts/selectable-vehicles/",
      signal,
    ),

  /**
   * Open a response for this crew's own ambulance.
   *
   * Returns the existing trip if there already is one, so the caller can use
   * it unconditionally rather than having to check first.
   */
  newEmergency: (shiftId: number, incidentAddress = "") =>
    api.post<Trip>(`/api/v1/fleet/shifts/${shiftId}/new-emergency/`, {
      incident_address: incidentAddress,
    }),
};

// ---------------------------------------------------------------------------
// Driver module: fleet board, maintenance, breakdown transfer
// ---------------------------------------------------------------------------
export const driverOps = {
  /** Every ambulance with crew, readiness, inspection and current job. */
  board: (signal?: AbortSignal) => api.get<FleetBoard>("/api/v1/fleet/board/", signal),

  /** Administrator override — attributed and broadcast, never silent. */
  overrideReadiness: (callsign: string, readiness: VehicleReadiness, note = "") =>
    api.post<FleetRow>(`/api/v1/fleet/vehicles/${callsign}/readiness/`, {
      readiness,
      note,
    }),

  maintenance: (openOnly = true, signal?: AbortSignal) =>
    api.get<{ reports: MaintenanceReport[] }>(
      `/api/v1/fleet/maintenance/${openOnly ? "?open=1" : ""}`,
      signal,
    ),

  /** A driver raising a fault outside the inspection flow. */
  reportFault: (vehicleCallsign: string, reasons: FailureReason[], remarks = "") =>
    api.post<MaintenanceReport>("/api/v1/fleet/maintenance/report/", {
      vehicle_callsign: vehicleCallsign,
      reasons,
      remarks,
    }),

  acknowledgeFault: (id: number) =>
    api.post<MaintenanceReport>(`/api/v1/fleet/maintenance/${id}/acknowledge/`, {}),

  resolveFault: (id: number, notes = "") =>
    api.post<MaintenanceReport>(`/api/v1/fleet/maintenance/${id}/resolve/`, { notes }),

  /**
   * The Emergency Breakdown button.
   *
   * One press notifies admin, dispatch, the receiving hospital and the
   * nearest crews — a crew with a deteriorating patient and a dead engine
   * cannot be asked to also contact four parties.
   */
  declareBreakdown: (
    vehicleCallsign: string,
    reasons: FailureReason[],
    remarks = "",
    position?: [number, number],
  ) =>
    api.post<Breakdown>("/api/v1/fleet/breakdowns/declare/", {
      vehicle_callsign: vehicleCallsign,
      reasons,
      remarks,
      ...(position ? { latitude: position[0], longitude: position[1] } : {}),
    }),

  openBreakdowns: (signal?: AbortSignal) =>
    api.get<{ breakdowns: Breakdown[] }>("/api/v1/fleet/breakdowns/?open=1", signal),

  /** Transfer requests waiting on the vehicles this user crews. */
  myOffers: (signal?: AbortSignal) =>
    api.get<{ offers: TransferOffer[] }>("/api/v1/fleet/breakdowns/offers/", signal),

  acceptTransfer: (breakdownId: number, vehicleCallsign: string) =>
    api.post<Breakdown>(`/api/v1/fleet/breakdowns/${breakdownId}/accept/`, {
      vehicle_callsign: vehicleCallsign,
    }),

  rejectTransfer: (breakdownId: number, vehicleCallsign: string, reason = "") =>
    api.post<TransferOffer>(`/api/v1/fleet/breakdowns/${breakdownId}/reject/`, {
      vehicle_callsign: vehicleCallsign,
      reason,
    }),
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
      symptoms?: SymptomCode[];
      patient_age?: number;
      patient_notes?: string;
      patient_deteriorating?: boolean;
      hospital_id?: number;
      override_reason?: string;
      /** Distinguishes a patient exercising their right to choose from the
       *  crew disagreeing with the engine. Review must not conflate them. */
      choice_reason?: HospitalChoiceReason;
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

  /**
   * Rank hospitals for a patient at this location.
   *
   * `symptoms` tighten the category's rule rather than replacing it, and are
   * the only clinical input when the category is undetermined.
   */
  recommend: (
    latitude: number,
    longitude: number,
    emergencyCategory: string,
    symptoms: SymptomCode[] = [],
  ) =>
    api.post<Recommendation>("/api/v1/hospitals/recommend/", {
      latitude,
      longitude,
      emergency_category: emergencyCategory,
      symptoms,
    }),

  ruleCatalogue: () => api.get<EmergencyRuleSummary[]>("/api/v1/hospitals/rules/catalogue/"),

  symptomCatalogue: (signal?: AbortSignal) =>
    api.get<{ symptoms: SymptomSpec[] }>("/api/v1/hospitals/symptoms/", signal),

  acknowledgeAlert: (alertId: number, body: { acknowledged_by?: string; preparation_notes?: string }) =>
    api.post<unknown>(`/api/v1/hospitals/alerts/${alertId}/acknowledge/`, body),
};

// ---------------------------------------------------------------------------
// AI Traffic Intelligence Engine (Layer 2)
// ---------------------------------------------------------------------------
export const brain = {
  /**
   * Optimised route between two points, in current traffic.
   *
   * Used by the paramedic screen to preview the road to a candidate hospital
   * before the crew commits to it - the trip's own route is not planned until
   * the assessment is confirmed, so without this the map has nothing to draw.
   */
  route: (
    origin: [number, number],
    destination: [number, number],
    priorityLevel = 1,
  ) =>
    api.post<RoutePreview>("/api/v1/brain/route/", {
      origin_lat: origin[0],
      origin_lon: origin[1],
      dest_lat: destination[0],
      dest_lon: destination[1],
      priority_level: priorityLevel,
      include_geometry: true,
    }),
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
