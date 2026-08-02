/**
 * SEVPS service worker - Web Push receiver.
 *
 * Served from the site root (see apps/dashboards/views.py `service_worker`)
 * because a service worker's scope cannot rise above its own URL. Shipped as a
 * Vite asset it would live under /static/ and control only /static/, which is
 * the one part of the site with no pages in it.
 *
 * Kept deliberately small and dependency-free: this file runs outside the app,
 * survives reloads, and is the last thing between a push and a person who
 * needs to see it. It does not cache the application - SEVPS shows live
 * operational state, and a stale cached dashboard that looks current is worse
 * than one that fails to load.
 */

const TAG_PREFIX = "sevps";

self.addEventListener("install", () => {
  // Take over immediately. The alternative is a push arriving while an old
  // worker is still in control, which during an incident is not a trade worth
  // making for the usual safety of a staged activation.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (err) {
    // A push that arrives unreadable still means something happened. Showing
    // a generic prompt beats swallowing it silently.
    payload = { title: "SEVPS alert", body: "Open the console for details." };
  }

  const severity = payload.severity || "info";
  const critical = severity === "critical";

  const options = {
    body: payload.body || "",
    icon: "/static/js/notify-icon.png",
    badge: "/static/js/notify-badge.png",
    // Collapsing on dedupe_key is the point: three reroutes of one trip are
    // one story. Without it a crew gets three banners for one situation and
    // learns to swipe them all away.
    tag: `${TAG_PREFIX}:${payload.dedupe_key || payload.id || Date.now()}`,
    renotify: critical,
    // Critical alerts stay on screen until acted on. Everything else follows
    // the platform's own dismissal timing.
    requireInteraction: critical,
    silent: false,
    timestamp: payload.issued_at ? Date.parse(payload.issued_at) : Date.now(),
    data: {
      link: payload.link || "/ops",
      id: payload.id || "",
      severity,
      category: payload.category || "",
    },
    actions: payload.link ? [{ action: "open", title: "Open" }] : [],
  };

  event.waitUntil(
    Promise.all([
      self.registration.showNotification(payload.title || "SEVPS", options),
      // Tell any open tab as well, so the in-app notification centre updates
      // without waiting for its next poll.
      broadcastToClients(payload),
    ]),
  );
});

async function broadcastToClients(payload) {
  const clients = await self.clients.matchAll({
    type: "window",
    includeUncontrolled: true,
  });
  for (const client of clients) {
    client.postMessage({ type: "sevps-push", payload });
  }
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const link = (event.notification.data && event.notification.data.link) || "/ops";

  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      // Focus an existing tab rather than opening a fourth copy of the console
      // on a controller's screen.
      for (const client of clients) {
        if ("focus" in client) {
          await client.focus();
          if ("navigate" in client) {
            try {
              await client.navigate(link);
            } catch (err) {
              /* cross-origin or unsupported; focusing was the important part */
            }
          }
          return;
        }
      }
      if (self.clients.openWindow) await self.clients.openWindow(link);
    })(),
  );
});

/**
 * A push service can invalidate a subscription at any time. Without this the
 * browser silently stops receiving alerts and nothing indicates why - the user
 * still sees permission granted.
 */
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    (async () => {
      const applicationServerKey =
        (event.oldSubscription && event.oldSubscription.options.applicationServerKey) || null;
      if (!applicationServerKey) return;

      const subscription = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey,
      });
      await fetch("/api/v1/notify/subscribe/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(subscription.toJSON()),
        credentials: "include",
      });
    })(),
  );
});
