/**
 * HTTP client with transparent JWT refresh.
 *
 * Token handling mirrors the decision made server-side in Phase 2:
 *
 * - The **access token lives in memory only** (module scope, not localStorage).
 *   It dies with the tab, so an XSS cannot exfiltrate a durable credential.
 * - The **refresh token is an httpOnly cookie** the browser holds and JS cannot
 *   read. Reload recovers the session by calling /auth/jwt/refresh/ with no
 *   token in hand - the cookie does the work.
 *
 * Consequently there is no "remember me" checkbox and no token in devtools'
 * Application tab. That is intentional.
 */
import type { TokenPair } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly payload: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /** Field errors from a DRF serializer, if this was a 400. */
  get fieldErrors(): Record<string, string[]> {
    if (this.status !== 400 || !this.payload || typeof this.payload !== "object") return {};
    return this.payload as Record<string, string[]>;
  }
}

let accessToken: string | null = null;
/** Single-flight guard: a burst of 401s must trigger one refresh, not N. */
let refreshInFlight: Promise<boolean> | null = null;
let onAuthLost: (() => void) | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

/** Called when refresh fails - the app uses this to bounce to /login. */
export function setAuthLostHandler(handler: (() => void) | null): void {
  onAuthLost = handler;
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match?.[1] ?? "";
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Skip the refresh-and-retry dance (used by the refresh call itself). */
  raw?: boolean;
  signal?: AbortSignal;
}

async function parse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function describe(status: number, payload: unknown): string {
  if (payload && typeof payload === "object") {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    const entries = Object.entries(payload as Record<string, unknown>);
    if (entries.length) {
      const [field, messages] = entries[0]!;
      const text = Array.isArray(messages) ? messages.join(", ") : String(messages);
      return `${field}: ${text}`;
    }
  }
  if (typeof payload === "string" && payload) return payload;
  return `Request failed (${status})`;
}

async function performRefresh(): Promise<boolean> {
  // The refresh token is in an httpOnly cookie, so the body is empty and the
  // browser supplies the credential.
  try {
    const response = await fetch("/api/v1/auth/jwt/refresh/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      credentials: "same-origin",
      body: "{}",
    });
    if (!response.ok) return false;
    const pair = (await response.json()) as TokenPair;
    accessToken = pair.access;
    return true;
  } catch {
    return false;
  }
}

async function refreshOnce(): Promise<boolean> {
  refreshInFlight ??= performRefresh().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, raw = false, signal } = options;

  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken(),
    };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

    return fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  };

  let response = await send();

  // One transparent retry after a refresh. Only on 401: a 403 means the token
  // is valid and the *role* is insufficient, which refreshing cannot fix.
  if (response.status === 401 && !raw) {
    const refreshed = await refreshOnce();
    if (refreshed) {
      response = await send();
    } else {
      accessToken = null;
      onAuthLost?.();
    }
  }

  const payload = await parse(response);
  if (!response.ok) {
    throw new ApiError(describe(response.status, payload), response.status, payload);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  /** Used by login and refresh, which must not recurse into the retry path. */
  raw: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body, raw: true }),
};
