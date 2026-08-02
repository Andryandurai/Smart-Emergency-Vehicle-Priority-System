import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Trip, VehiclePayload } from "@/api/types";
import { unwrap } from "@/api/types";
import {
  selectActiveHolds,
  selectOpenPreemptions,
  selectTripList,
  useOpsStore,
} from "@/stores/opsStore";
import { useAuthStore } from "@/stores/authStore";

function vehicle(callsign: string, level = 1): VehiclePayload {
  return {
    id: 1, uuid: "u", callsign, vehicle_type: "ambulance", status: "transporting",
    latitude: 13, longitude: 80, heading_deg: 0, speed_kmh: 40,
    priority_level: level as 1, siren_mode: "continuous", light_pattern: "max",
    last_seen_at: null, is_stale: false,
  };
}

function trip(id: number, level: 1 | 2 | 3 | 4): Trip {
  return {
    id, uuid: `u${id}`, reference: `SEV-${id}`, vehicle: id,
    vehicle_callsign: `AMB-${id}`, vehicle_type: "ambulance",
    vehicle_latitude: 13, vehicle_longitude: 80, vehicle_speed_kmh: 30,
    stage: "to_hospital", stage_display: "En route", emergency_category: "cardiac",
    category_display: "Heart Attack", hospital_code: "APOLLO", hospital_name: "Apollo",
    destination_latitude: 13.1, destination_longitude: 80.1,
    priority_level: level, siren_mode: "continuous", light_pattern: "max",
    eta: null, distance_remaining_m: 1200, active_route: null,
    patient_age: null, patient_notes: null, patient_deteriorating: null,
    incident_address: null, caller_number: null,
    response_time_s: null, transport_time_s: null, created_at: "",
  };
}

describe("unwrap", () => {
  it("handles a bare array, a paginated envelope and nothing at all", () => {
    expect(unwrap([1, 2])).toEqual([1, 2]);
    expect(unwrap({ results: [3] })).toEqual([3]);
    expect(unwrap(null)).toEqual([]);
    expect(unwrap(undefined)).toEqual([]);
    expect(unwrap({})).toEqual([]);
  });
});

describe("opsStore", () => {
  beforeEach(() => useOpsStore.getState().reset());

  it("keys vehicles by callsign so a poll and a socket event converge", () => {
    const store = useOpsStore.getState();
    store.upsertVehicle(vehicle("AMB-1"));
    store.upsertVehicle({ ...vehicle("AMB-1"), speed_kmh: 55 });

    const vehicles = Object.values(useOpsStore.getState().vehicles);
    expect(vehicles).toHaveLength(1);
    expect(vehicles[0]!.speed_kmh).toBe(55);
  });

  it("sorts trips most critical first", () => {
    useOpsStore.getState().applySnapshot({ trips: [trip(1, 3), trip(2, 1), trip(3, 2)] });
    expect(selectTripList(useOpsStore.getState()).map((t) => t.priority_level)).toEqual([1, 2, 3]);
  });

  it("replaces the trip set so completed responses leave the board", () => {
    useOpsStore.getState().applySnapshot({ trips: [trip(1, 1), trip(2, 2)] });
    useOpsStore.getState().applySnapshot({ trips: [trip(2, 2)] });
    expect(selectTripList(useOpsStore.getState())).toHaveLength(1);
  });

  it("counts only active holds as cross-traffic cost", () => {
    useOpsStore.getState().applySnapshot({
      preemptions: [
        { state: "active" }, { state: "planned" }, { state: "released" },
      ] as never,
    });
    const state = useOpsStore.getState();
    expect(selectActiveHolds(state)).toBe(1);
    expect(selectOpenPreemptions(state)).toHaveLength(2);
  });

  it("drops expired driver alerts when new ones arrive", () => {
    const past = new Date(Date.now() - 60_000).toISOString();
    const future = new Date(Date.now() + 60_000).toISOString();
    useOpsStore.getState().pushAlerts([
      { uuid: "old", expires_at: past } as never,
      { uuid: "new", expires_at: future } as never,
    ]);
    expect(useOpsStore.getState().alerts.map((a) => a.uuid)).toEqual(["new"]);
  });

  it("caps the event log so a long shift cannot exhaust memory", () => {
    const store = useOpsStore.getState();
    for (let i = 0; i < 200; i += 1) store.addLog(`entry ${i}`);
    expect(useOpsStore.getState().log.length).toBeLessThanOrEqual(80);
    expect(useOpsStore.getState().log[0]!.message).toBe("entry 199");
  });
});

describe("authStore role checks", () => {
  beforeEach(() => {
    useAuthStore.setState({ user: null, status: "anonymous", error: null, submitting: false });
  });

  it("returns false for every role when signed out", () => {
    expect(useAuthStore.getState().hasRole("administrators")).toBe(false);
    expect(useAuthStore.getState().can("clinical_data")).toBe(false);
  });

  it("matches a held role", () => {
    useAuthStore.setState({
      status: "authenticated",
      user: {
        id: 1, username: "medic", email: "", name: "", is_staff: false, is_superuser: false,
        roles: ["ambulance_drivers"],
        capabilities: { clinical_data: true, traffic_control: false, dispatch_control: false, admin_site: false },
      },
    });
    expect(useAuthStore.getState().hasRole("ambulance_drivers")).toBe(true);
    expect(useAuthStore.getState().hasRole("traffic_police")).toBe(false);
    expect(useAuthStore.getState().can("clinical_data")).toBe(true);
    expect(useAuthStore.getState().can("traffic_control")).toBe(false);
  });

  it("treats a superuser as holding every role", () => {
    useAuthStore.setState({
      status: "authenticated",
      user: {
        id: 1, username: "root", email: "", name: "", is_staff: true, is_superuser: true,
        roles: [],
        capabilities: { clinical_data: true, traffic_control: true, dispatch_control: true, admin_site: true },
      },
    });
    expect(useAuthStore.getState().hasRole("hospital_staff")).toBe(true);
  });

  it("ends the local session even when the logout call fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    useAuthStore.setState({
      status: "authenticated",
      user: { id: 1, username: "medic", email: "", name: "", is_staff: false, is_superuser: false,
        roles: [], capabilities: { clinical_data: false, traffic_control: false, dispatch_control: false, admin_site: false } },
    });

    await useAuthStore.getState().logout();

    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().status).toBe("anonymous");
    vi.unstubAllGlobals();
  });
});
