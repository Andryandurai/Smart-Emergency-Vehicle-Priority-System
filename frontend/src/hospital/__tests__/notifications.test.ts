/**
 * The Alerts division's contract.
 *
 * Two rules that are easy to break by accident and impossible to notice
 * afterwards: an entry must never leave on its own, and removing one must
 * never take another with it. Both are asserted here rather than left to a
 * reading of the reducer.
 */
import { beforeEach, describe, expect, it } from "vitest";

import { useHospitalNotices } from "@/hospital/notifications";

const notice = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  kind: "inbound" as const,
  title: "Ambulance inbound",
  body: "AMB-101 is on the way.",
  tripId: 1,
  callsign: "AMB-101",
  at: "2026-08-04T09:00:00Z",
  ...extra,
});

describe("hospital alerts", () => {
  beforeEach(() => {
    useHospitalNotices.setState({ items: [] });
  });

  it("keeps an alert after its popup has settled", () => {
    const store = useHospitalNotices.getState();
    store.push(notice("inbound:1"));

    useHospitalNotices.getState().settle("inbound:1");

    const items = useHospitalNotices.getState().items;
    // Off the popup surface...
    expect(items[0]!.fresh).toBe(false);
    // ...but still on the board, and still actionable.
    expect(items).toHaveLength(1);
    expect(items[0]!.acknowledged).toBe(false);
  });

  it("keeps an acknowledged alert listed", () => {
    const store = useHospitalNotices.getState();
    store.push(notice("inbound:1"));

    useHospitalNotices.getState().acknowledge("inbound:1");

    const items = useHospitalNotices.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]!.acknowledged).toBe(true);
  });

  it("removes only the alert asked for", () => {
    const store = useHospitalNotices.getState();
    store.push(notice("inbound:1"));
    store.push(notice("arrived:1", { kind: "arrived" as const, title: "Ambulance arrived" }));
    store.push(notice("breakdown:2", { kind: "breakdown" as const, title: "Ambulance breakdown" }));
    expect(useHospitalNotices.getState().items).toHaveLength(3);

    useHospitalNotices.getState().remove("arrived:1");

    const ids = useHospitalNotices.getState().items.map((item) => item.id);
    expect(ids).toHaveLength(2);
    expect(ids).not.toContain("arrived:1");
    // The neighbours are untouched - not acknowledged, not settled, not gone.
    expect(ids).toEqual(expect.arrayContaining(["inbound:1", "breakdown:2"]));
    for (const item of useHospitalNotices.getState().items) {
      expect(item.acknowledged).toBe(false);
    }
  });

  it("removing an alert that is not there changes nothing", () => {
    useHospitalNotices.getState().push(notice("inbound:1"));
    useHospitalNotices.getState().remove("inbound:999");
    expect(useHospitalNotices.getState().items).toHaveLength(1);
  });

  it("does not stack repeats of the same event", () => {
    const store = useHospitalNotices.getState();
    store.push(notice("inbound:1"));
    store.push(notice("inbound:1"));
    store.push(notice("inbound:1"));
    expect(useHospitalNotices.getState().items).toHaveLength(1);
  });

  it("does not revive an acknowledged alert when the socket re-sends it", () => {
    const store = useHospitalNotices.getState();
    store.push(notice("inbound:1"));
    useHospitalNotices.getState().acknowledge("inbound:1");

    useHospitalNotices.getState().push(notice("inbound:1"));

    const items = useHospitalNotices.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]!.acknowledged).toBe(true);
  });

  it("clears an outstanding admission once the patient is in", () => {
    useHospitalNotices.getState().push(
      notice("received:1", { kind: "received" as const, needsAdmission: true }),
    );

    useHospitalNotices.getState().markAdmitted("received:1");

    const item = useHospitalNotices.getState().items[0]!;
    expect(item.needsAdmission).toBe(false);
    // Still listed: the admission is part of the shift's record.
    expect(useHospitalNotices.getState().items).toHaveLength(1);
  });
});
