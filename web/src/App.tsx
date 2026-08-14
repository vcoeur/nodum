import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { CommandPalette, ErrorBoundary, Spinner, ToastProvider } from "./components";
import { api, getHealth, ApiError } from "./api/client";
import type { HumanOut } from "./api/types";
import {
  clearWriteTarget,
  invalidateRecentNodesScopes,
  isModalOpen,
  onUnauthorized,
  onRecentScopesInvalidated,
  setRecentNodesScope,
  setCommandPaletteOpen,
} from "./lib";
import { versionLabel } from "./versionLabel";

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
  { to: "/nodes", label: "Nodes" },
  { to: "/editor", label: "Editor" },
  { to: "/review", label: "Review" },
  { to: "/journal", label: "Journal" },
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
  const [paletteOpen, setPaletteOpen] = useState(false);

  // The page a 401 should return to after a fresh login. A ref, because the
  // broadcast subscription is registered once and must not go stale as the
  // user navigates.
  const locationRef = useRef(location);
  locationRef.current = location;
  // Invalidates a late identity response after a 401 has already removed the
  // verified scope. A late success must not re-open a departed human's titles.
  const identityGeneration = useRef(0);
  const gateController = useRef<AbortController | null>(null);

  useEffect(
    () =>
      onUnauthorized(() => {
        identityGeneration.current += 1;
        setPaletteOpen(false);
        setCommandPaletteOpen(false);
        invalidateRecentNodesScopes();
        setMe(null);
        setGated(false);
        clearWriteTarget();
        const { pathname, search } = locationRef.current;
        navigate("/login", { replace: true, state: { from: pathname + search } });
      }),
    [navigate],
  );

  /**
   * Establish the session's verified identity and the recents scope that may
   * only follow from it. Runs on mount and again after another tab transitioned
   * sessions: a pending response issued under the previous cookie is stale the
   * moment that cookie changes owners, so every run aborts the last request
   * and lifts the scope until this request proves who owns the tab now.
   */
  const verifyIdentity = useCallback(() => {
    gateController.current?.abort();
    const controller = new AbortController();
    gateController.current = controller;
    const generation = ++identityGeneration.current;
    // No browser-local reading title is visible until this request proves which
    // human owns this tab. A non-401 failure may still render failure-capable
    // views below, but it never acquires a recents scope.
    setRecentNodesScope(null);
    void (async () => {
      try {
        const human = await api.getMe(controller.signal);
        if (controller.signal.aborted || identityGeneration.current !== generation) return;
        setRecentNodesScope(human.id);
        setMe(human);
      } catch (error) {
        // A 401 already broadcast the redirect to /login. Anything else — an
        // unreachable server, above all — must not lock the app behind the
        // gate: the views render their own failure panels for that. A
        // superseded request is the same stale truth as in the success branch:
        // a re-verify superseded this one, so the newer request owns the gate.
        if (controller.signal.aborted || identityGeneration.current !== generation) return;
        if (error instanceof ApiError && error.status === 401) return;
      }
      setGated(false);
    })();
  }, []);

  useEffect(() => {
    verifyIdentity();
    const unsubscribeInvalidation = onRecentScopesInvalidated(verifyIdentity);
    return () => {
      unsubscribeInvalidation();
      gateController.current?.abort();
    };
  }, [verifyIdentity]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.key === "k" || event.key === "K") && (event.ctrlKey || event.metaKey)) {
        // Ctrl/Cmd-K belongs to the app even when a modal already owns focus:
        // without cancelling it first, the browser can consume the chord while
        // the modal stays open.
        event.preventDefault();
        if (gated || me === null || isModalOpen()) return;
        // Set ownership synchronously: SearchView receives this same native
        // event and must see that Ctrl/Cmd-K belongs to the palette.
        setCommandPaletteOpen(true);
        setPaletteOpen(true);
      }
    };
    // Capture before a focus-owning Modal stops bubbling its keys at document.
    // The palette remains blocked by modal ownership, but the browser must not
    // receive the reserved command chord.
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [gated, me]);

  useEffect(() => {
    setCommandPaletteOpen(paletteOpen);
    return () => setCommandPaletteOpen(false);
  }, [paletteOpen]);

  const onLogout = () => {
    void (async () => {
      try {
        await api.logout();
      } catch {
        // A dead session lands on /login either way.
      }
      // The write target is persisted per *browser*, not per session, so
      // without this the next human to sign in on this machine inherits the
      // previous one's target and files their first note into a space they
      // never chose — D1a's failure across an account change rather than
      // across a tab. Clearing it is not a silent rewrite: nobody is looking
      // at a create surface at this point, and the reset is announced by the
      // editor showing `main` the moment one is opened.
      clearWriteTarget();
      invalidateRecentNodesScopes();
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
            // Keyed on the pathname so a route change remounts the boundary:
            // without a key, React never resets a crashed boundary on
            // navigation and the crash panel survives every route change.
            <ErrorBoundary key={location.pathname}>
              <Outlet />
            </ErrorBoundary>
          )}
        </main>
        {!gated && me !== null && paletteOpen ? <CommandPalette onClose={() => setPaletteOpen(false)} /> : null}
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
 * running — a state that is otherwise only visible as failing requests. While
 * it answers, the label beside the pill names the version the process reports,
 * so what is served is visible without a terminal.
 */
function ServerStatus() {
  const [state, setState] = useState<Reachability>("checking");
  const [version, setVersion] = useState<string | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const check = async () => {
      try {
        const health = await getHealth(controller.signal);
        if (!cancelled) {
          setState("online");
          setVersion(health.version);
        }
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
  // The version renders only while the server is answering: next to "no
  // server" a last-known value would read as current, and the pill alone is
  // the whole truth there.
  const label = online ? versionLabel(version) : null;
  return (
    <>
      <span
        className={online ? "nd-badge nd-badge--active" : "nd-badge nd-badge--archived"}
        title={online ? "The nodum server is answering" : "No answer from the nodum server"}
      >
        <span className="nd-badge__dot" aria-hidden="true" />
        {online ? "connected" : "no server"}
      </span>
      {label ? (
        <span className="nd-header__version" title="nodum version">
          {label}
        </span>
      ) : null}
    </>
  );
}

export { App };
