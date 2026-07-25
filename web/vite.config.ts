import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The Python process serves the built bundle from `nodum/_web/` at `/`, so the
 * production app is same-origin with the API (no CORS anywhere in the stack).
 * In dev the Vite server stands in for that mount and proxies the two API
 * prefixes to `nodum serve` on 127.0.0.1:8420.
 */
const API_PORT = 8420;

/** Vite dev server. nodum owns 57xx; see the note on `server.port` below. */
const DEV_PORT = 5700;

/** `npm run preview` — the built bundle, served without the Python process. */
const PREVIEW_PORT = 5701;

export default defineConfig({
  plugins: [react()],
  base: "/",
  // Pinned: left to the default, the dep-optimizer cache has been observed
  // landing in a stray `.vite/` at the repository root rather than under web/.
  cacheDir: "node_modules/.vite",
  build: {
    outDir: "../nodum/_web",
    emptyOutDir: true,
    // Off on purpose: this output is packaged into the wheel, and the maps for
    // CodeMirror + Mermaid + Cytoscape would dominate it. Frontend work happens
    // against `npm run dev`, which has full sourcemaps regardless of this.
    sourcemap: false,
  },
  server: {
    // 5700, not Vite's default 5173: every vcoeur app owns a port range so two
    // of them can run side by side, and 5173 is explicitly avoided because any
    // unrelated Vite project grabs it first. strictPort so a collision fails
    // loudly instead of silently landing on another app's port.
    port: DEV_PORT,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${API_PORT}`,
        changeOrigin: false,
      },
      "/healthz": {
        target: `http://127.0.0.1:${API_PORT}`,
        changeOrigin: false,
      },
    },
  },
  preview: {
    port: PREVIEW_PORT,
    strictPort: true,
  },
});
