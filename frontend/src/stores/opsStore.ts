/**
 * Live operational state for the Emergency Operations console.
 *
 * Fed from two directions, and both are necessary:
 *
 * - **WebSocket** for instant updates within the server process.
 * - **Polling** as the floor, because with the default in-memory channel layer
 *   events raised by `sevps_worker` or `simulate` never reach a socket held by
 *   the web process. Set SEVPS_REDIS_URL and the sockets carry everything;
 *   without it the console is still correct, just a couple of seconds behind.
 *
 * Trips are keyed by id so a socket event and a poll converge on one row
 * rather than duplicating it.
 */
import { create } from "zustand";

import { dispatch, fleet, network } from "@/api/endpoints";
import type { DriverAlert, Preemption, RoadEvent, Trip, VehiclePayload } from "@/api/types";

export interface LogEntry {
  id: number;
  at: Date;
  message: string;
  tone: "info" | "ok" | "warn" | "bad";
}

/** Pushed by the server's live sweep, not by a GPS fix. */
export interface EtaUpdate {
  trip_id: number;
  eta: string;
  remaining_s: number;
  remaining_m: number;
  is_stalled: boolean;
}

interface OpsState {
  vehicles: Record<string, VehiclePayload>;
  trips: Record<number, Trip>;
  preemptions: Preemption[];
  events: RoadEvent[];
  alerts: DriverAlert[];
  log: LogEntry[];
  followedCallsign: string | null;
  lastError: string | null;

  refreshAll: (signal?: AbortSignal) => Promise<void>;
  refreshVehicles: (signal?: AbortSignal) => Promise<void>;
  refreshTrips: (signal?: AbortSignal) => Promise<void>;
  refreshPreemptions: (signal?: AbortSignal) => Promise<void>;
  refreshEvents: (signal?: AbortSignal) => Promise<void>;

  applySnapshot: (data: {
    vehicles?: VehiclePayload[];
    trips?: Trip[];
    preemptions?: Preemption[];
    events?: RoadEvent[];
    recent_alerts?: DriverAlert[];
  }) => void;
  upsertVehicle: (vehicle: VehiclePayload) => void;
  applyEta: (update: EtaUpdate) => void;
  pushAlerts: (alerts: DriverAlert[]) => void;
  addLog: (message: string, tone?: LogEntry["tone"]) => void;
  follow: (callsign: string | null) => void;
  reset: () => void;
}

const LOG_LIMIT = 80;
let logSequence = 0;

/** An aborted fetch is a normal consequence of unmounting, not an error. */
function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export const useOpsStore = create<OpsState>((set, get) => ({
  vehicles: {},
  trips: {},
  preemptions: [],
  events: [],
  alerts: [],
  log: [],
  followedCallsign: null,
  lastError: null,

  async refreshAll(signal) {
    await Promise.all([
      get().refreshVehicles(signal),
      get().refreshTrips(signal),
      get().refreshPreemptions(signal),
      get().refreshEvents(signal),
    ]);
  },

  async refreshVehicles(signal) {
    try {
      const live = await fleet.live(signal);
      set((state) => {
        const vehicles = { ...state.vehicles };
        for (const vehicle of live.vehicles) vehicles[vehicle.callsign] = vehicle;
        return { vehicles, lastError: null };
      });
    } catch (error) {
      if (!isAbort(error)) set({ lastError: (error as Error).message });
    }
  },

  async refreshTrips(signal) {
    try {
      const { trips } = await dispatch.liveTrips(signal);
      // Replace wholesale: a trip that ended must disappear, and merging
      // would leave completed responses on the board forever.
      set({ trips: Object.fromEntries(trips.map((trip) => [trip.id, trip])), lastError: null });
    } catch (error) {
      if (!isAbort(error)) set({ lastError: (error as Error).message });
    }
  },

  async refreshPreemptions(signal) {
    try {
      set({ preemptions: await dispatch.openPreemptions(signal) });
    } catch (error) {
      if (!isAbort(error)) set({ lastError: (error as Error).message });
    }
  },

  async refreshEvents(signal) {
    try {
      set({ events: await network.activeEvents(signal) });
    } catch (error) {
      if (!isAbort(error)) set({ lastError: (error as Error).message });
    }
  },

  applySnapshot(data) {
    set((state) => ({
      vehicles: {
        ...state.vehicles,
        ...Object.fromEntries((data.vehicles ?? []).map((v) => [v.callsign, v])),
      },
      trips: data.trips
        ? Object.fromEntries(data.trips.map((t) => [t.id, t]))
        : state.trips,
      preemptions: data.preemptions ?? state.preemptions,
      events: data.events ?? state.events,
      alerts: data.recent_alerts ?? state.alerts,
    }));
  },

  upsertVehicle(vehicle) {
    set((state) => ({ vehicles: { ...state.vehicles, [vehicle.callsign]: vehicle } }));
  },

  /**
   * Apply a server-pushed ETA without refetching the trip.
   *
   * ETA decays continuously and is pushed by the worker's live sweep; a full
   * trip refetch per update would be wasteful and would also clobber any
   * fields the socket knows nothing about.
   */
  applyEta(update) {
    set((state) => {
      const trip = state.trips[update.trip_id];
      if (!trip) return state;
      return {
        trips: {
          ...state.trips,
          [update.trip_id]: {
            ...trip,
            eta: update.eta,
            distance_remaining_m: update.remaining_m,
          },
        },
      };
    });
  },

  pushAlerts(incoming) {
    set((state) => {
      const byUuid = new Map(state.alerts.map((alert) => [alert.uuid, alert]));
      for (const alert of incoming) byUuid.set(alert.uuid, alert);
      const now = Date.now();
      return {
        alerts: [...byUuid.values()].filter(
          (alert) => new Date(alert.expires_at).getTime() > now,
        ),
      };
    });
  },

  addLog(message, tone = "info") {
    set((state) => ({
      log: [{ id: ++logSequence, at: new Date(), message, tone }, ...state.log].slice(0, LOG_LIMIT),
    }));
  },

  follow(callsign) {
    set({ followedCallsign: callsign });
  },

  reset() {
    set({
      vehicles: {}, trips: {}, preemptions: [], events: [],
      alerts: [], log: [], followedCallsign: null, lastError: null,
    });
  },
}));

// -------------------------------- selectors --------------------------------
/**
 * Derived selectors.
 *
 * The three below build a new array on every call, which means a component
 * MUST subscribe to them through `useShallow`. Zustand 5 sits on React's
 * `useSyncExternalStore`, which compares snapshots with `Object.is`: a fresh
 * array is never equal to the previous one, so the component re-renders, the
 * selector runs again, and the page locks into an infinite render loop.
 *
 * It fails hard and completely - a blank screen and "Maximum update depth
 * exceeded" - so it cannot ship unnoticed, but it also cannot be caught by any
 * test below the browser. It was found by the Playwright suite in Phase 12.
 *
 *     const trips = useOpsStore(useShallow(selectTripList));   // correct
 *     const trips = useOpsStore(selectTripList);               // infinite loop
 *
 * `selectActiveHolds` returns a number and is safe to use directly.
 */
export const selectTripList = (state: OpsState): Trip[] =>
  Object.values(state.trips).sort((a, b) => a.priority_level - b.priority_level);

export const selectVehicleList = (state: OpsState): VehiclePayload[] =>
  Object.values(state.vehicles);

export const selectOpenPreemptions = (state: OpsState): Preemption[] =>
  state.preemptions.filter((p) => ["planned", "armed", "active"].includes(p.state));

export const selectActiveHolds = (state: OpsState): number =>
  state.preemptions.filter((p) => p.state === "active").length;
