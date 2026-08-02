/* Shared SEVPS front-end helpers: auto-reconnecting socket, map setup,
   formatting.  Deliberately dependency-free apart from Leaflet. */
(function (global) {
  "use strict";

  const LEVEL_CLASS = { 1: "l1", 2: "l2", 3: "l3", 4: "l4" };
  const LEVEL_LABEL = {
    1: "Level 1 - Critical",
    2: "Level 2 - High",
    3: "Level 3 - Moderate",
    4: "Level 4 - Non-critical",
  };

  /** WebSocket wrapper that reconnects with backoff and reports status. */
  class Socket {
    constructor(path, handlers) {
      this.path = path;
      this.handlers = handlers || {};
      this.retry = 0;
      this.ws = null;
      this.connect();
    }

    connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      this.setStatus("connecting");
      this.ws = new WebSocket(`${scheme}://${location.host}${this.path}`);

      this.ws.onopen = () => {
        this.retry = 0;
        this.setStatus("open");
        // Application-level keepalive: proxies drop idle sockets silently.
        this.ping = setInterval(() => this.send({ type: "ping" }), 25000);
        if (this.handlers.open) this.handlers.open(this);
      };

      this.ws.onmessage = (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch (err) {
          return;
        }
        const fn = this.handlers[payload.event] || this.handlers["*"];
        if (fn) fn(payload.data, payload.event);
      };

      this.ws.onclose = () => {
        clearInterval(this.ping);
        this.setStatus("closed");
        // Exponential backoff, capped, so a restarting server is not hammered.
        const delay = Math.min(15000, 800 * Math.pow(2, this.retry++));
        setTimeout(() => this.connect(), delay);
      };

      this.ws.onerror = () => this.ws.close();
    }

    send(message) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(message));
      }
    }

    setStatus(state) {
      const el = document.getElementById("conn-status");
      if (!el) return;
      el.dataset.state = state;
      el.querySelector(".label").textContent = state;
    }
  }

  function createMap(elementId, center, zoom) {
    const map = L.map(elementId, { zoomControl: true, preferCanvas: true }).setView(center, zoom);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
      maxZoom: 20,
      subdomains: "abcd",
    }).addTo(map);
    return map;
  }

  function vehicleIcon(level, type) {
    const glyph = { ambulance: "A", fire_engine: "F", police: "P", disaster: "D" }[type] || "E";
    return L.divIcon({
      className: "",
      html: `<div class="veh-marker ${LEVEL_CLASS[level] || "l4"}">${glyph}</div>`,
      iconSize: [26, 26],
      iconAnchor: [13, 13],
    });
  }

  function dotIcon(color, size) {
    const s = size || 10;
    return L.divIcon({
      className: "",
      html: `<div style="width:${s}px;height:${s}px;border-radius:50%;background:${color};border:1px solid #0d1117"></div>`,
      iconSize: [s, s],
      iconAnchor: [s / 2, s / 2],
    });
  }

  function fmtTime(value) {
    if (!value) return "--:--";
    const d = value instanceof Date ? value : new Date(value);
    if (isNaN(d)) return "--:--";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function fmtEta(value) {
    if (!value) return "no ETA";
    const seconds = (new Date(value) - new Date()) / 1000;
    if (isNaN(seconds)) return "no ETA";
    if (seconds < 0) return "arriving";
    if (seconds < 90) return `${Math.round(seconds)}s`;
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  }

  function fmtDistance(metres) {
    if (metres === null || metres === undefined) return "-";
    return metres >= 1000 ? `${(metres / 1000).toFixed(1)} km` : `${Math.round(metres)} m`;
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "-";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    return m < 60 ? `${m}m ${Math.round(seconds % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  /** Small rolling activity log used by several dashboards. */
  function logger(containerId, limit) {
    const el = document.getElementById(containerId);
    const max = limit || 60;
    return function log(message, tone) {
      if (!el) return;
      const entry = document.createElement("div");
      entry.className = "entry";
      entry.innerHTML =
        `<span class="t">${fmtTime(new Date())}</span>` +
        `<span${tone ? ` class="badge ${tone}"` : ""}>${message}</span>`;
      el.prepend(entry);
      while (el.children.length > max) el.removeChild(el.lastChild);
    };
  }

  function csrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? match[1] : "";
  }

  async function api(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-CSRFToken"] = csrfToken();
    opts.credentials = "same-origin";
    if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
    const response = await fetch(path, opts);
    const text = await response.text();
    let data;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (err) {
      data = { detail: text };
    }
    if (!response.ok) {
      const error = new Error((data && (data.detail || JSON.stringify(data))) || response.statusText);
      error.status = response.status;
      error.data = data;
      if (response.status === 401 || response.status === 403) {
        error.isAuthError = true;
        notifyAuthRequired(error);
      }
      throw error;
    }
    return data;
  }

  /* Several feeds became authenticated in the RBAC phase (trips carry patient
     data). Rather than let a page break on a 401, show one banner and stop the
     pollers - a control room seeing a stale map with no explanation is worse
     than one told plainly that it needs to sign in. */
  let authBannerShown = false;
  function notifyAuthRequired(error) {
    if (authBannerShown) return;
    authBannerShown = true;

    const banner = document.createElement("div");
    banner.className = "auth-banner";
    const forbidden = error.status === 403;
    banner.innerHTML = forbidden
      ? `<b>Not permitted.</b> Your role cannot view this data.
         <a href="/login/?next=${encodeURIComponent(location.pathname)}">Sign in as another user</a>`
      : `<b>Sign in required.</b> This view contains operational and patient data.
         <a href="/login/?next=${encodeURIComponent(location.pathname)}">Sign in</a>`;
    document.body.appendChild(banner);

    window.dispatchEvent(new CustomEvent("sevps:auth-required", { detail: error }));
  }

  /** Wraps a poller so it stops cleanly once authentication is the blocker. */
  function poll(fn, intervalMs) {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        await fn();
      } catch (err) {
        if (err && err.isAuthError) stopped = true;
      }
    };
    tick();
    const handle = setInterval(() => {
      if (stopped) clearInterval(handle);
      else tick();
    }, intervalMs);
    window.addEventListener("sevps:auth-required", () => { stopped = true; });
    return () => { stopped = true; clearInterval(handle); };
  }

  function congestionColour(index) {
    if (index >= 0.8) return "#e74c3c";
    if (index >= 0.55) return "#e67e22";
    if (index >= 0.35) return "#f1c40f";
    if (index >= 0.15) return "#9acd32";
    return "#2ecc71";
  }

  global.SEVPS = {
    Socket,
    createMap,
    vehicleIcon,
    dotIcon,
    fmtTime,
    fmtEta,
    fmtDistance,
    fmtDuration,
    logger,
    api,
    poll,
    congestionColour,
    LEVEL_CLASS,
    LEVEL_LABEL,
  };
})(window);
