/**
 * Notification centre state.
 *
 * Three sources feed one list, which is the whole point of having a store here
 * rather than local state in a component:
 *
 *  1. the REST inbox — durable history, answers "what did I miss";
 *  2. the WebSocket `notification` event — instant, while a tab is open;
 *  3. `postMessage` from the service worker — a push that arrived while the
 *     tab was backgrounded.
 *
 * All three can carry the same notification. They are reconciled on `uuid`,
 * and then collapsed on `dedupe_key`, so three reroutes of one trip occupy one
 * row showing the latest state instead of stacking into a wall the operator
 * scrolls past.
 */
import { create } from "zustand";

import { notify } from "@/api/endpoints";
import type { NotificationRecord } from "@/api/types";

/** Cap on what is held in memory. The server keeps the full record. */
const MAX_ITEMS = 80;

interface NotifyState {
  items: NotificationRecord[];
  unread: number;
  loading: boolean;
  error: string | null;
  pushState: string;
  pushDetail: string;

  refresh: (signal?: AbortSignal) => Promise<void>;
  /** A live notification from the socket or the service worker. */
  ingest: (payload: Partial<NotificationRecord> & { id?: string; title?: string }) => void;
  markRead: (uuid?: string) => Promise<void>;
  setPushState: (state: string, detail: string) => void;
}

function collapse(items: NotificationRecord[]): NotificationRecord[] {
  const seen = new Map<string, NotificationRecord>();
  for (const item of items) {
    const key = item.dedupe_key || item.uuid;
    const existing = seen.get(key);
    // Newest wins: a dedupe_key names a situation, and the situation's current
    // state is what an operator needs to see.
    if (!existing || item.created_at > existing.created_at) seen.set(key, item);
  }
  return [...seen.values()]
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
    .slice(0, MAX_ITEMS);
}

/** Live events carry the push payload shape, not the full record. */
function normalise(payload: Partial<NotificationRecord> & { id?: string }): NotificationRecord {
  return {
    uuid: payload.uuid ?? payload.id ?? crypto.randomUUID(),
    title: payload.title ?? "SEVPS notification",
    body: payload.body ?? "",
    severity: payload.severity ?? "info",
    severity_label: payload.severity_label ?? "",
    category: payload.category ?? "system",
    category_label: payload.category_label ?? "",
    link: payload.link ?? "",
    dedupe_key: payload.dedupe_key ?? "",
    context: payload.context ?? {},
    created_at: payload.created_at ?? new Date().toISOString(),
    delivered_count: payload.delivered_count ?? 0,
    failed_count: payload.failed_count ?? 0,
    is_read: false,
  };
}

export const useNotifyStore = create<NotifyState>((set, get) => ({
  items: [],
  unread: 0,
  loading: false,
  error: null,
  pushState: "prompt",
  pushDetail: "",

  async refresh(signal) {
    set({ loading: true });
    try {
      const inbox = await notify.inbox(signal);
      set({ items: collapse(inbox.notifications), unread: inbox.unread, error: null });
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      // A failed inbox fetch must not blank the list: what is already on
      // screen is still true, and clearing it looks like "all clear".
      set({ error: (err as Error).message });
    } finally {
      set({ loading: false });
    }
  },

  ingest(payload) {
    const record = normalise(payload);
    const items = collapse([record, ...get().items]);
    set({ items, unread: items.filter((item) => !item.is_read).length });
  },

  async markRead(uuid) {
    // Optimistic: the badge should clear on click, not after a round trip.
    const items = get().items.map((item) =>
      !uuid || item.uuid === uuid ? { ...item, is_read: true } : item,
    );
    set({ items, unread: items.filter((item) => !item.is_read).length });
    try {
      await notify.markRead(uuid);
    } catch {
      void get().refresh();
    }
  },

  setPushState(pushState, pushDetail) {
    set({ pushState, pushDetail });
  },
}));

export const selectUnread = (state: NotifyState) => state.unread;
export const selectCritical = (state: NotifyState) =>
  state.items.filter((item) => item.severity === "critical" && !item.is_read);
