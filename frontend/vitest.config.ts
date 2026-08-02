import path from "node:path";
import { defineConfig } from "vitest/config";

/**
 * Kept separate from `vite.config.ts` on purpose.
 *
 * Vitest bundles its own copy of Vite, so a single config importing
 * `defineConfig` from `vitest/config` while `@vitejs/plugin-react` resolves
 * against the top-level Vite produces two distinct `Plugin` type identities
 * and a wall of unassignable-type errors. Two files, two resolutions, no
 * conflict - and the build config stays free of test concerns.
 */
export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
