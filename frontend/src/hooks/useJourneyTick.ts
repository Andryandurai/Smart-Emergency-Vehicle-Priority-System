/**
 * Keep the ambulance moving.
 *
 * A route is planned, a hospital assigned and a corridor armed - and then
 * nothing advances the vehicle unless a real GPS device is reporting or the
 * `simulate` command is running in a second terminal. On a pilot machine
 * neither is true, so the marker sat still for the whole journey while the ETA
 * counted down beside it.
 *
 * This asks the server for one movement step at a time. The position it
 * produces comes back over the WebSocket as an ordinary `vehicle_position`
 * event, so nothing downstream knows or cares that the fix was computed rather
 * than received - the same handler moves the marker either way, and the
 * corridor, ETA and driver alerts all react as they would to a real device.
 *
 * Shared by the driver and paramedic navigation screens deliberately. Both
 * crew are in one ambulance and must watch one journey; two portals
 * interpolating positions locally would drift apart within a minute, and the
 * hospital watching the same trip would see a third answer.
 */
import { useEffect } from "react";

import { dispatch as dispatchApi } from "@/api/endpoints";

/**
 * Four seconds, not one.
 *
 * The server ignores any vehicle whose last fix is under three seconds old -
 * that is how it defers to a real device, or to `simulate`, when one is
 * driving. Ticking faster than that window would spend requests on steps the
 * server is bound to refuse, and with both crew screens open the effective
 * rate is already double whatever this is set to.
 */
const DEFAULT_INTERVAL_MS = 4000;

export function useJourneyTick(enabled: boolean, intervalMs = DEFAULT_INTERVAL_MS): void {
  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;
    let inFlight = false;

    const step = async (): Promise<void> => {
      // A slow sweep must not queue up behind itself. On a loaded machine the
      // tick can take longer than the interval, and without this the backlog
      // grows for as long as the screen is open.
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        await dispatchApi.journeyTick();
      } catch {
        // Movement is a convenience layered over the real telemetry path. A
        // failed sweep means the marker pauses, which is not worth surfacing
        // on a screen someone is driving by.
      } finally {
        inFlight = false;
      }
    };

    void step();
    const timer = window.setInterval(() => void step(), intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled, intervalMs]);
}
