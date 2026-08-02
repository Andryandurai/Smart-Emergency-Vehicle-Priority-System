/**
 * Interval polling that stops cleanly on auth loss and unmount.
 *
 * The console polls even when a WebSocket is open. That is not redundancy for
 * its own sake: with the default in-memory channel layer, events published by
 * `sevps_worker` or `simulate` never reach a socket held by the web process,
 * so polling is the correctness floor and the socket is the latency
 * improvement. Set SEVPS_REDIS_URL and the socket carries everything.
 */
import { useEffect, useRef } from "react";

import { ApiError } from "@/api/client";

interface PollingOptions {
  /** Run immediately on mount as well as on the interval. Default true. */
  immediate?: boolean;
  enabled?: boolean;
}

export function usePolling(
  task: (signal: AbortSignal) => Promise<unknown>,
  intervalMs: number,
  options: PollingOptions = {},
): void {
  const { immediate = true, enabled = true } = options;

  // Read the task through a ref so an inline arrow function in the caller
  // does not restart the interval on every render.
  const taskRef = useRef(task);
  taskRef.current = task;

  useEffect(() => {
    if (!enabled) return;

    const controller = new AbortController();
    let stopped = false;
    let timer: number | undefined;

    const run = async (): Promise<void> => {
      if (stopped) return;
      try {
        await taskRef.current(controller.signal);
      } catch (error) {
        // A 401/403 will not fix itself by retrying, and hammering a
        // protected endpoint from a signed-out tab is just noise in the log.
        if (error instanceof ApiError && error.isAuthError) {
          stopped = true;
          window.clearInterval(timer);
        }
      }
    };

    if (immediate) void run();
    timer = window.setInterval(() => void run(), intervalMs);

    return () => {
      stopped = true;
      window.clearInterval(timer);
      controller.abort();
    };
  }, [intervalMs, immediate, enabled]);
}
