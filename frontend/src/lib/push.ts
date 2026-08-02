/**
 * Web Push registration.
 *
 * The failure chain here is unusually long — secure context, service worker
 * registration, notification permission, VAPID key, subscribe, server
 * registration — and each link fails in its own way. So every function returns
 * a discriminated reason rather than a boolean: "push is off" is not actionable
 * and "your browser blocked notifications for this site" is.
 */

export type PushState =
  | "unsupported"
  | "insecure"
  | "unconfigured"
  | "denied"
  | "prompt"
  | "subscribed"
  | "error";

export interface PushStatus {
  state: PushState;
  detail: string;
  endpoint: string | null;
}

const SUBSCRIBE_URL = "/api/v1/notify/subscribe/";
const UNSUBSCRIBE_URL = "/api/v1/notify/unsubscribe/";
const VAPID_URL = "/api/v1/notify/vapid-key/";

/** Push requires HTTPS, with localhost exempted for development. */
export function isSecureContextForPush(): boolean {
  if (typeof window === "undefined") return false;
  return (
    window.isSecureContext ||
    ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)
  );
}

export function isSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** base64url (what the server sends) -> Uint8Array (what subscribe() wants). */
export function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalised = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(normalised);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!isSupported()) return null;
  try {
    // Scope "/" is why the worker is served from the root by Django rather
    // than bundled under /static/ — see apps/dashboards/views.py.
    return await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch {
    return null;
  }
}

export async function currentStatus(): Promise<PushStatus> {
  if (!isSupported()) {
    return { state: "unsupported", detail: "This browser has no Push API.", endpoint: null };
  }
  if (!isSecureContextForPush()) {
    return {
      state: "insecure",
      detail: "Push requires HTTPS. Serve the console over TLS to enable alerts.",
      endpoint: null,
    };
  }
  if (Notification.permission === "denied") {
    return {
      state: "denied",
      detail: "Notifications are blocked for this site in browser settings.",
      endpoint: null,
    };
  }

  const registration = await navigator.serviceWorker.getRegistration("/");
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription) {
    return { state: "subscribed", detail: "Alerts are enabled on this device.", endpoint: subscription.endpoint };
  }
  return { state: "prompt", detail: "Alerts are not enabled on this device.", endpoint: null };
}

async function fetchVapidKey(): Promise<{ key: string; hint: string }> {
  const response = await fetch(VAPID_URL, { credentials: "include" });
  if (!response.ok) throw new Error(`VAPID key unavailable (${response.status})`);
  const body = (await response.json()) as { public_key: string; hint: string };
  return { key: body.public_key, hint: body.hint };
}

/**
 * Enable push on this device.
 *
 * `anonymous` registers a road-user device for approaching-ambulance warnings
 * without an account — the Layer 4 path — sending a coarse position so the
 * server can target the right geohash cell.
 */
export async function enablePush(
  options: { anonymous?: boolean; deviceId?: string; position?: GeolocationCoordinates } = {},
): Promise<PushStatus> {
  const status = await currentStatus();
  if (status.state === "unsupported" || status.state === "insecure" || status.state === "denied") {
    return status;
  }

  let vapid: { key: string; hint: string };
  try {
    vapid = await fetchVapidKey();
  } catch (err) {
    return { state: "error", detail: (err as Error).message, endpoint: null };
  }
  if (!vapid.key) {
    return {
      state: "unconfigured",
      detail: vapid.hint || "Push is not configured on this server.",
      endpoint: null,
    };
  }

  const registration = (await registerServiceWorker()) ?? (await navigator.serviceWorker.ready);
  if (!registration) {
    return { state: "error", detail: "Service worker registration failed.", endpoint: null };
  }

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    return {
      state: permission === "denied" ? "denied" : "prompt",
      detail: "Notification permission was not granted.",
      endpoint: null,
    };
  }

  let subscription: PushSubscription;
  try {
    subscription =
      (await registration.pushManager.getSubscription()) ??
      (await registration.pushManager.subscribe({
        // Required by Chrome: a push may not be used for anything the user
        // cannot see. SEVPS shows every push, so this costs nothing.
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapid.key) as BufferSource,
      }));
  } catch (err) {
    return { state: "error", detail: `Subscribe failed: ${(err as Error).message}`, endpoint: null };
  }

  const body: Record<string, unknown> = { ...subscription.toJSON(), backend: "webpush" };
  if (options.anonymous && options.deviceId) body.device_id = options.deviceId;
  if (options.position) {
    body.latitude = options.position.latitude;
    body.longitude = options.position.longitude;
  }

  const response = await fetch(SUBSCRIBE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    // The browser now holds a subscription the server does not know about,
    // which would look enabled and deliver nothing. Roll it back.
    await subscription.unsubscribe().catch(() => undefined);
    return {
      state: "error",
      detail: `Server rejected the subscription (${response.status}).`,
      endpoint: null,
    };
  }

  return { state: "subscribed", detail: "Alerts are enabled on this device.", endpoint: subscription.endpoint };
}

export async function disablePush(): Promise<PushStatus> {
  const registration = await navigator.serviceWorker.getRegistration("/");
  const subscription = await registration?.pushManager.getSubscription();
  if (!subscription) {
    return { state: "prompt", detail: "Alerts were not enabled here.", endpoint: null };
  }

  // Tell the server first. If the order were reversed and the request failed,
  // the server would keep pushing to an endpoint the browser had discarded —
  // failures that only surface as a growing dead-subscription count.
  await fetch(UNSUBSCRIBE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ endpoint: subscription.endpoint }),
  }).catch(() => undefined);

  await subscription.unsubscribe().catch(() => undefined);
  return { state: "prompt", detail: "Alerts are disabled on this device.", endpoint: null };
}
