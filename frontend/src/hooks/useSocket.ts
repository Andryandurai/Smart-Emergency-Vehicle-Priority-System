/**
 * Auto-reconnecting WebSocket bound to the SEVPS event envelope.
 *
 * The access token is passed on the query string because a browser cannot set
 * an Authorization header on a WebSocket handshake. That is why access tokens
 * are short-lived (15 min) server-side - query strings land in access logs.
 *
 * Three behaviours matter beyond "connect and listen":
 *
 * - **Refusals are terminal.** 4401/4403 mean sign in / wrong role. Reconnecting
 *   cannot fix either, and a backoff loop against a socket that will always
 *   refuse is just noise in the server log.
 * - **Sequence gaps are surfaced.** Every frame carries a monotonic `seq`. A
 *   gap means the client's state may be stale, so callers can resync instead
 *   of rendering a map that quietly stopped updating.
 * - **The `viewer` block is exposed.** A socket that connected anonymously is
 *   visible in the UI rather than silently delivering redacted data.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { getAccessToken } from "@/api/client";
import type { SocketEvent, Viewer } from "@/api/types";

export type SocketStatus = "connecting" | "open" | "closed" | "unauthorised" | "forbidden";

type Handler = (data: unknown, event: string) => void;

interface UseSocketOptions {
  /** Handlers by event name; `*` catches anything unmatched. */
  handlers: Record<string, Handler>;
  enabled?: boolean;
  /** Server-side filters, e.g. `{ event: ["eta_update"] }`. */
  filters?: Record<string, string[]>;
  /** Called when a sequence gap indicates missed frames. */
  onGap?: (missed: number) => void;
}

interface UseSocketResult {
  status: SocketStatus;
  viewer: Viewer | null;
  /** Frames the server sent that this client never saw. */
  missedFrames: number;
  send: (message: unknown) => void;
}

const MAX_BACKOFF_MS = 15_000;
const PING_INTERVAL_MS = 25_000;

/** Matches apps/core/consumers.py. */
const CLOSE_UNAUTHENTICATED = 4401;
const CLOSE_FORBIDDEN = 4403;
const CLOSE_NOT_FOUND = 4404;

export function useSocket(path: string | null, options: UseSocketOptions): UseSocketResult {
  const { handlers, enabled = true, filters, onGap } = options;

  const [status, setStatus] = useState<SocketStatus>("connecting");
  const [viewer, setViewer] = useState<Viewer | null>(null);
  const [missedFrames, setMissedFrames] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  // Read through refs so a parent re-render that recreates these objects does
  // not tear down and rebuild the connection.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const onGapRef = useRef(onGap);
  onGapRef.current = onGap;

  const retryRef = useRef(0);
  const lastSeqRef = useRef(0);
  const timersRef = useRef<{ reconnect?: number; ping?: number }>({});
  const closedByUsRef = useRef(false);

  const filterKey = filters ? JSON.stringify(filters) : "";

  const send = useCallback((message: unknown) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }, []);

  useEffect(() => {
    if (!path || !enabled) {
      setStatus("closed");
      return;
    }

    closedByUsRef.current = false;
    lastSeqRef.current = 0;

    const connect = (): void => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const token = getAccessToken();
      const url = `${scheme}://${window.location.host}${path}${
        token ? `${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : ""
      }`;

      setStatus("connecting");
      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        retryRef.current = 0;
        setStatus("open");
        if (filterKey) socket.send(JSON.stringify({ type: "subscribe", filters }));
        // Application-level keepalive: idle sockets get dropped by proxies.
        timersRef.current.ping = window.setInterval(
          () => send({ type: "ping" }),
          PING_INTERVAL_MS,
        );
      };

      socket.onmessage = (raw) => {
        let payload: SocketEvent & { seq?: number };
        try {
          payload = JSON.parse(raw.data as string) as SocketEvent & { seq?: number };
        } catch {
          return;
        }

        if (typeof payload.seq === "number") {
          const expected = lastSeqRef.current + 1;
          if (lastSeqRef.current > 0 && payload.seq > expected) {
            const missed = payload.seq - expected;
            setMissedFrames((total) => total + missed);
            onGapRef.current?.(missed);
          }
          lastSeqRef.current = payload.seq;
        }

        if (payload.event === "snapshot") {
          const data = payload.data as { viewer?: Viewer };
          if (data?.viewer) setViewer(data.viewer);
        }
        const handler = handlersRef.current[payload.event] ?? handlersRef.current["*"];
        handler?.(payload.data, payload.event);
      };

      socket.onclose = (event) => {
        window.clearInterval(timersRef.current.ping);

        // A refusal will not resolve itself. Reconnecting would loop forever
        // against a socket the server has already decided to reject.
        if (event.code === CLOSE_UNAUTHENTICATED) {
          setStatus("unauthorised");
          return;
        }
        if (event.code === CLOSE_FORBIDDEN || event.code === CLOSE_NOT_FOUND) {
          setStatus("forbidden");
          return;
        }

        setStatus("closed");
        if (closedByUsRef.current) return;
        const delay = Math.min(MAX_BACKOFF_MS, 800 * 2 ** retryRef.current++);
        timersRef.current.reconnect = window.setTimeout(connect, delay);
      };

      socket.onerror = () => socket.close();
    };

    connect();

    return () => {
      closedByUsRef.current = true;
      window.clearTimeout(timersRef.current.reconnect);
      window.clearInterval(timersRef.current.ping);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [path, enabled, send, filterKey, filters]);

  return { status, viewer, missedFrames, send };
}
