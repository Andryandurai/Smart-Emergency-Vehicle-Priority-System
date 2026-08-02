import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

/**
 * Dev runs on Vite (5173) and proxies API + WebSocket traffic to Django (8000).
 *
 * Proxying rather than enabling CORS in dev matters for one specific reason:
 * the JWT refresh token lives in an httpOnly cookie scoped to /api/v1/auth/.
 * Cross-origin that cookie needs SameSite=None + Secure, which does not work
 * over plain http://localhost. Same-origin through the proxy, it just works,
 * and dev behaves like production instead of needing a parallel auth path.
 */
// The E2E suite runs Django on its own port against its own database, so the
// proxy target has to be configurable. Defaults to the normal dev server.
const API_TARGET = process.env.SEVPS_API_TARGET ?? "http://127.0.0.1:8000";
const WS_TARGET = API_TARGET.replace(/^http/, "ws");

export default defineConfig(({ mode }) => ({
  // Production assets are served by Django from /static/, so the hashed URLs
  // baked into index.html must be prefixed to match. In dev Vite serves the
  // app itself from the root, and /static/ is proxied to Django for the
  // legacy screens' stylesheet.
  base: mode === "production" ? "/static/" : "/",
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/ws": { target: WS_TARGET, ws: true },
      // The Django admin and the legacy server-rendered screens stay reachable
      // during the migration.
      "/admin": { target: API_TARGET, changeOrigin: true },
      "/static": { target: API_TARGET, changeOrigin: true },
      // The push service worker is served by Django at the root so its scope
      // covers the whole site (see apps/dashboards/views.py). Proxying it in
      // dev means one copy, and dev push behaves exactly like production.
      "/sw.js": { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    // Django serves the built assets from here; see sevps/settings.py
    // STATICFILES_DIRS and apps/dashboards/views.py spa_index.
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          // Leaflet is large and changes rarely - splitting it keeps the app
          // chunk small enough to re-download on every deploy without cost.
          leaflet: ["leaflet", "react-leaflet"],
          // Same reasoning for Recharts, and more so: only /analytics uses it,
          // and that route is lazy, so a controller who never opens the
          // analytics screen never downloads ~470 kB of charting code.
          charts: ["recharts"],
          vendor: ["react", "react-dom", "react-router-dom", "zustand"],
        },
      },
    },
  },
}));
