import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production build is served same-origin by FastAPI (nodum.web) from
// NODUM_WEB_DIST, so every asset is referenced under the same origin and the
// server's `script-src 'self'` CSP holds. Two settings keep that true:
//  - assetsInlineLimit: 0  → no inline `data:` asset URIs
//  - modulePreload.polyfill: false → no injected inline module-preload script
// In dev (`npm run dev`), proxy the API routes to the running FastAPI server so
// the browser sees one origin and the session cookie flows.
const API_TARGET = "http://127.0.0.1:8600";
// Keep in sync with the routes nodum.api registers — a missing prefix here makes the
// dev server 404 that route while production (FastAPI serves the SPA same-origin) works.
const API_ROUTES = [
  "/nodes",
  "/edges",
  "/node-kinds",
  "/edge-kinds",
  "/search",
  "/expand",
  "/schema",
  "/auth",
  "/healthz",
];

// Dev/preview ports follow the conception dev-port scheme: nodum's Vite slot is
// 5700 (dev) / 5701 (preview). Avoids 5173 and the other apps' slots.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
  },
  server: {
    port: 5700,
    strictPort: true,
    proxy: Object.fromEntries(API_ROUTES.map((route) => [route, API_TARGET])),
  },
  preview: {
    port: 5701,
    strictPort: true,
  },
});
