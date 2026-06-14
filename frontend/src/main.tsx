import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Self-hosted variable fonts — bundled same-origin by Vite so the strict
// `default-src 'self'` CSP holds (no CDN, no inline). Display serif + grotesque
// body + technical mono.
import "@fontsource-variable/fraunces/full.css";
import "@fontsource-variable/hanken-grotesk";
import "@fontsource-variable/jetbrains-mono";

import { App } from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root element");
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
