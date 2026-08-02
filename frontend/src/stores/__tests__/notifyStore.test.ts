import { beforeEach, describe, expect, it } from "vitest";

import { useNotifyStore } from "@/stores/notifyStore";
import { urlBase64ToUint8Array } from "@/lib/push";

const base = {
  uuid: "a",
  title: "Corridor failed",
  body: "",
  severity: "warning" as const,
  severity_label: "",
  category: "corridor",
  category_label: "",
  link: "",
  dedupe_key: "",
  context: {},
  created_at: "2026-08-02T09:00:00Z",
  delivered_count: 0,
  failed_count: 0,
  is_read: false,
};

describe("notifyStore", () => {
  beforeEach(() => {
    useNotifyStore.setState({ items: [], unread: 0, error: null });
  });

  it("collapses notifications sharing a dedupe key", () => {
    // Three reroutes of one trip are one story, not three rows.
    const store = useNotifyStore.getState();
    store.ingest({ ...base, uuid: "1", dedupe_key: "reroute:7", title: "Rerouted" });
    store.ingest({
      ...base, uuid: "2", dedupe_key: "reroute:7",
      title: "Rerouted again", created_at: "2026-08-02T09:05:00Z",
    });

    const { items } = useNotifyStore.getState();
    expect(items).toHaveLength(1);
    expect(items[0]?.title).toBe("Rerouted again");
  });

  it("keeps notifications with different dedupe keys apart", () => {
    const store = useNotifyStore.getState();
    store.ingest({ ...base, uuid: "1", dedupe_key: "reroute:7" });
    store.ingest({ ...base, uuid: "2", dedupe_key: "reroute:8" });
    expect(useNotifyStore.getState().items).toHaveLength(2);
  });

  it("treats a missing dedupe key as unique rather than merging everything", () => {
    const store = useNotifyStore.getState();
    store.ingest({ ...base, uuid: "1", dedupe_key: "" });
    store.ingest({ ...base, uuid: "2", dedupe_key: "" });
    expect(useNotifyStore.getState().items).toHaveLength(2);
  });

  it("sorts newest first", () => {
    const store = useNotifyStore.getState();
    store.ingest({ ...base, uuid: "1", created_at: "2026-08-02T08:00:00Z" });
    store.ingest({ ...base, uuid: "2", created_at: "2026-08-02T10:00:00Z" });
    expect(useNotifyStore.getState().items[0]?.uuid).toBe("2");
  });

  it("normalises a push payload that lacks record fields", () => {
    // The service worker forwards the push body, which is trimmed by design.
    useNotifyStore.getState().ingest({ id: "abc", title: "From push" });
    const item = useNotifyStore.getState().items[0];
    expect(item?.uuid).toBe("abc");
    expect(item?.severity).toBe("info");
    expect(item?.is_read).toBe(false);
  });

  it("counts unread", () => {
    const store = useNotifyStore.getState();
    store.ingest({ ...base, uuid: "1" });
    store.ingest({ ...base, uuid: "2", dedupe_key: "other" });
    expect(useNotifyStore.getState().unread).toBe(2);
  });
});

describe("urlBase64ToUint8Array", () => {
  it("decodes unpadded url-safe base64 the way the Push API needs", () => {
    // "-_" are the url-safe substitutes for "+/"; a naive atob rejects them
    // and the subscribe call fails with an opaque InvalidCharacterError.
    const decoded = urlBase64ToUint8Array("q-_a");
    expect(Array.from(decoded)).toEqual([171, 239, 218]);
  });

  it("round-trips a realistic VAPID key length", () => {
    const key = "B".repeat(87);
    expect(urlBase64ToUint8Array(key).length).toBe(65);
  });
});
