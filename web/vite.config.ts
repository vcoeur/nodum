import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The Python process serves the built bundle from `nodum/_web/` at `/`, so the
 * production app is same-origin with the API (no CORS anywhere in the stack).
 * In dev the Vite server stands in for that mount and proxies the two API
 * prefixes to `nodum serve` on 127.0.0.1:8420.
 */
const API_PORT = 8420;

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
    port: 5173,
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
});
