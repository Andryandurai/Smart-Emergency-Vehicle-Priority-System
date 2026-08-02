/**
 * Wires the service worker, the push subscription and the notification store
 * together, so a component only has to render state and call two functions.
 */
import { useCallback, useEffect, useState } from "react";

import type { PushStatus } from "@/lib/push";
import { currentStatus, disablePush, enablePush, registerServiceWorker } from "@/lib/push";
import { useNotifyStore } from "@/stores/notifyStore";

/** Fallback poll for the inbox. Slow on purpose: the socket and the service
 *  worker deliver in real time, and this only closes the gap after a laptop
 *  wakes from sleep with both of those having quietly died. */
const INBOX_POLL_MS = 60_000;

export function usePushNotifications(enabled = true) {
  const store = useNotifyStore();
  const [status, setStatus] = useState<PushStatus>({
    state: "prompt",
    detail: "",
    endpoint: null,
  });
  const [busy, setBusy] = useState(false);

  // Register the worker on mount regardless of subscription state: it is what
  // receives a push, and a browser that already granted permission in a
  // previous session must not need a click to start working again.
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void (async () => {
      await registerServiceWorker();
      const next = await currentStatus();
      if (!cancelled) {
        setStatus(next);
        store.setPushState(next.state, next.detail);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  // A push received while this tab was backgrounded arrives here rather than
  // over the socket, which the browser may have suspended.
  useEffect(() => {
    if (!enabled || typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
    const onMessage = (event: MessageEvent) => {
      if (event.data?.type === "sevps-push") store.ingest(event.data.payload);
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    return () => navigator.serviceWorker.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void store.refresh(controller.signal);
    const timer = window.setInterval(() => void store.refresh(), INBOX_POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  const enable = useCallback(async () => {
    setBusy(true);
    try {
      const next = await enablePush();
      setStatus(next);
      useNotifyStore.getState().setPushState(next.state, next.detail);
      return next;
    } finally {
      setBusy(false);
    }
  }, []);

  const disable = useCallback(async () => {
    setBusy(true);
    try {
      const next = await disablePush();
      setStatus(next);
      useNotifyStore.getState().setPushState(next.state, next.detail);
      return next;
    } finally {
      setBusy(false);
    }
  }, []);

  return { status, busy, enable, disable, ...store };
}
