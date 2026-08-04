/**
 * The hospital's alert queue.
 *
 * Deliberately not the platform's `notifyStore`. That one is an *inbox*: a
 * durable, role-scoped history that collapses repeats and marks things read on
 * a click. This is an emergency department's alert board, and it has one rule
 * the inbox does not: nothing leaves it on a timer. A toast that fades after
 * four seconds is worse than useless to a charge nurse who was across the room
 * when an ambulance was assigned — they would never know it had happened.
 *
 * So every entry stays, visibly, until someone acts on it. "Acted on" is a
 * real state rather than a dismissal: acknowledging an inbound alert is the
 * hospital saying it has seen the patient coming.
 *
 * Lives in a store rather than in the dashboard's local state because the
 * events arrive on a socket the shell holds, and they must survive a tab
 * switch — an alert raised while the Updates tab was open is exactly the one
 * that must not be lost.
 */
import { create } from "zustand";

export type NoticeKind = "inbound" | "arrived" | "breakdown" | "received";

export interface HospitalNotice {
  /** Stable across repeats of the same event - see `push`. */
  id: string;
  kind: NoticeKind;
  title: string;
  body: string;
  tripId: number | null;
  callsign: string;
  at: string;
  acknowledged: boolean;
  /** Raised in this browser session, so it should still pop up. */
  fresh: boolean;
}

interface HospitalNoticeState {
  items: HospitalNotice[];
  push: (notice: Omit<HospitalNotice, "acknowledged" | "fresh">) => void;
  acknowledge: (id: string) => void;
  acknowledgeAll: () => void;
  /** Stop the popup without clearing the panel entry. */
  settle: (id: string) => void;
  reset: () => void;
}

/** Newest first, and capped - a night's worth of arrivals is not a board. */
const MAX_ITEMS = 40;

export const useHospitalNotices = create<HospitalNoticeState>((set) => ({
  items: [],

  /**
   * Add an alert, or leave the existing one alone.
   *
   * Keyed on kind + trip, because the socket re-sends: `trip_stage` fires for
   * every stage change and a reconnect replays the snapshot. Without this, one
   * ambulance arriving would stack four identical banners, and acknowledging
   * them one at a time is precisely the busywork an ED will not do.
   *
   * An already-acknowledged alert is *not* revived by a repeat. Seeing it once
   * is the whole contract.
   */
  push(notice) {
    set((state) => {
      if (state.items.some((item) => item.id === notice.id)) return state;
      return {
        items: [{ ...notice, acknowledged: false, fresh: true }, ...state.items].slice(
          0,
          MAX_ITEMS,
        ),
      };
    });
  },

  acknowledge(id) {
    set((state) => ({
      items: state.items.map((item) =>
        item.id === id ? { ...item, acknowledged: true, fresh: false } : item,
      ),
    }));
  },

  acknowledgeAll() {
    set((state) => ({
      items: state.items.map((item) => ({ ...item, acknowledged: true, fresh: false })),
    }));
  },

  settle(id) {
    set((state) => ({
      items: state.items.map((item) => (item.id === id ? { ...item, fresh: false } : item)),
    }));
  },

  reset() {
    set({ items: [] });
  },
}));

export const selectUnacknowledged = (state: HospitalNoticeState): HospitalNotice[] =>
  state.items.filter((item) => !item.acknowledged);
