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
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      // The Django admin and the legacy server-rendered screens stay reachable
      // during the migration.
      "/admin": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/static": { target: "http://127.0.0.1:8000", changeOrigin: true },
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
          vendor: ["react", "react-dom", "react-router-dom", "zustand"],
        },
      },
    },
  },
}));
