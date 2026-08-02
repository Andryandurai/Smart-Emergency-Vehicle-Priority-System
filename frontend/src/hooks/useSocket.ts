/**
 * Auto-reconnecting WebSocket bound to the SEVPS event envelope.
 *
 * The access token is passed on the query string because a browser cannot set
 * an Authorization header on a WebSocket handshake. That is why access tokens
 * are short-lived (15 min) server-side - query strings land in access logs.
 *
 * Every SEVPS snapshot carries a `viewer` block stating who the server
 * authenticated. This hook surfaces it, so a socket that silently connected as
 * anonymous is visible in the UI instead of quietly delivering redacted data.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { getAccessToken } from "@/api/client";
import type { SocketEvent, Viewer } from "@/api/types";

export type SocketStatus = "connecting" | "open" | "closed";

type Handler = (data: unknown, event: string) => void;

interface UseSocketOptions {
  /** Handlers by event name; `*` catches anything unmatched. */
  handlers: Record<string, Handler>;
  enabled?: boolean;
}

interface UseSocketResult {
  status: SocketStatus;
  viewer: Viewer | null;
  send: (message: unknown) => void;
}

const MAX_BACKOFF_MS = 15_000;
const PING_INTERVAL_MS = 25_000;

export function useSocket(path: string | null, options: UseSocketOptions): UseSocketResult {
  const { handlers, enabled = true } = options;

  const [status, setStatus] = useState<SocketStatus>("connecting");
  const [viewer, setViewer] = useState<Viewer | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  // Handlers are read through a ref so a parent re-render that recreates the
  // handler object does not tear down and rebuild the connection.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  const retryRef = useRef(0);
  const timersRef = useRef<{ reconnect?: number; ping?: number }>({});
  const closedByUsRef = useRef(false);

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
        // Application-level keepalive: idle sockets get dropped by proxies.
        timersRef.current.ping = window.setInterval(
          () => send({ type: "ping" }),
          PING_INTERVAL_MS,
        );
      };

      socket.onmessage = (raw) => {
        let payload: SocketEvent;
        try {
          payload = JSON.parse(raw.data as string) as SocketEvent;
        } catch {
          return;
        }
        if (payload.event === "snapshot") {
          const data = payload.data as { viewer?: Viewer };
          if (data?.viewer) setViewer(data.viewer);
        }
        const handler = handlersRef.current[payload.event] ?? handlersRef.current["*"];
        handler?.(payload.data, payload.event);
      };

      socket.onclose = () => {
        window.clearInterval(timersRef.current.ping);
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
  }, [path, enabled, send]);

  return { status, viewer, send };
}
