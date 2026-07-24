import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { ErrorBoundary, ToastProvider } from "./components";
import { getHealth } from "./api/client";

/**
 * The app shell: header, view navigation, the routed outlet, and the two global
 * surfaces every view inherits — the toast stack and the crash boundary.
 *
 * Owned by the scaffold. View slices must not edit this file; if a view needs
 * something from the shell, add it here in a separate change so the other
 * slices see it.
 */

/** Every view in the app, in the order they appear in the header. */
const VIEWS = [
  { to: "/search", label: "Search" },
  { to: "/editor", label: "Editor" },
  { to: "/review", label: "Review" },
  { to: "/graph", label: "Graph" },
  { to: "/assets", label: "Assets" },
] as const;

/** The app shell. Rendered as the router's root route element. */
export default function App() {
  return (
    <ToastProvider>
      <div className="nd-app">
        <header className="nd-header">
          <NavLink to="/search" className="nd-header__brand">
            <KnotMark />
            nodum
            <span className="nd-header__tagline">knowledge graph</span>
          </NavLink>

          <nav className="nd-nav" aria-label="Views">
            {VIEWS.map((view) => (
              <NavLink
                key={view.to}
                to={view.to}
                className={({ isActive }) =>
                  isActive ? "nd-nav__link nd-nav__link--active" : "nd-nav__link"
                }
              >
                {view.label}
              </NavLink>
            ))}
          </nav>

          <div className="nd-header__status">
            <ServerStatus />
          </div>
        </header>

        <main className="nd-main">
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </ToastProvider>
  );
}

/**
 * The brand mark: three nodes tied by two edges — nodum is Latin for "knot".
 */
function KnotMark() {
  return (
    <svg
      className="nd-header__mark"
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.25"
      aria-hidden="true"
    >
      <path d="M3 3.5 L11 10.5 M11 3.5 L3 10.5" />
      <circle cx="3" cy="3.5" r="1.6" fill="currentColor" stroke="none" />
      <circle cx="11" cy="10.5" r="1.6" fill="currentColor" stroke="none" />
      <circle cx="7" cy="7" r="1.2" fill="currentColor" stroke="none" />
    </svg>
  );
}

/** Whether the API answered the last health check. */
type Reachability = "checking" | "online" | "offline";

/** How often the header re-checks that `nodum serve` is answering. */
const HEALTH_POLL_MS = 30_000;

/**
 * A quiet indicator that the Python process is answering.
 *
 * Worth the poll: the UI is served by the same process as the API, so "offline"
 * almost always means the dev server is proxying to a backend that is not
 * running — a state that is otherwise only visible as failing requests.
 */
function ServerStatus() {
  const [state, setState] = useState<Reachability>("checking");

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const check = async () => {
      try {
        await getHealth(controller.signal);
        if (!cancelled) setState("online");
      } catch {
        if (!cancelled) setState("offline");
      }
    };

    void check();
    const timer = window.setInterval(() => void check(), HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  if (state === "checking") return null;

  const online = state === "online";
  return (
    <span
      className={online ? "nd-badge nd-badge--active" : "nd-badge nd-badge--archived"}
      title={online ? "The nodum server is answering" : "No answer from the nodum server"}
    >
      <span className="nd-badge__dot" aria-hidden="true" />
      {online ? "connected" : "no server"}
    </span>
  );
}

export { App };
