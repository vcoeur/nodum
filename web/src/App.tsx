import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { ErrorBoundary, Spinner, ToastProvider } from "./components";
import { api, getHealth, ApiError } from "./api/client";
import type { HumanOut } from "./api/types";
import { onUnauthorized } from "./lib";

/**
 * The app shell: header, view navigation, the routed outlet, and the two global
 * surfaces every view inherits — the toast stack and the crash boundary.
 *
 * The shell is also the session gate. Every `/api` route but login requires a
 * session, so before any view mounts (and fires its own requests) the shell
 * asks `GET /api/me` who is logged in: a 401 means nobody is, and the user
 * goes to `/login` — the same place any later 401 sends them, via the
 * client's unauthorized broadcast. The answer also names the account for the
 * header. `/login` itself is a sibling route and never passes through here.
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
  { to: "/spaces", label: "Spaces" },
  { to: "/admin", label: "Admin" },
] as const;

/** The app shell. Rendered as the router's root route element. */
export default function App() {
  const navigate = useNavigate();
  const location = useLocation();

  /** The session's human, once known; null while the gate check runs. */
  const [me, setMe] = useState<HumanOut | null>(null);
  /** False until the gate has answered — views mount only past it. */
  const [gated, setGated] = useState(true);

  // The page a 401 should return to after a fresh login. A ref, because the
  // broadcast subscription is registered once and must not go stale as the
  // user navigates.
  const locationRef = useRef(location);
  locationRef.current = location;

  useEffect(
    () =>
      onUnauthorized(() => {
        const { pathname, search } = locationRef.current;
        navigate("/login", { replace: true, state: { from: pathname + search } });
      }),
    [navigate],
  );

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const human = await api.getMe(controller.signal);
        setMe(human);
      } catch (error) {
        // A 401 already broadcast the redirect to /login. Anything else — an
        // unreachable server, above all — must not lock the app behind the
        // gate: the views render their own failure panels for that.
        if (error instanceof ApiError && error.status === 401) return;
      }
      setGated(false);
    })();
    return () => controller.abort();
  }, []);

  const onLogout = () => {
    void (async () => {
      try {
        await api.logout();
      } catch {
        // A dead session lands on /login either way.
      }
      navigate("/login", { replace: true });
    })();
  };

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
            {me ? (
              <>
                <span className="nd-header__who" title={`Signed in as ${me.name}`}>
                  {me.name}
                </span>
                <button
                  type="button"
                  className="nd-button nd-button--ghost nd-button--small"
                  onClick={onLogout}
                >
                  Log out
                </button>
              </>
            ) : null}
          </div>
        </header>

        <main className="nd-main">
          {gated ? (
            <div className="nd-view">
              <div className="nd-empty">
                <Spinner large label="Checking session" />
              </div>
            </div>
          ) : (
            <ErrorBoundary>
              <Outlet />
            </ErrorBoundary>
          )}
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
