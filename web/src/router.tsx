import { lazy, Suspense } from "react";
import type { ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router-dom";
import App from "./App";
import { EmptyState, Spinner } from "./components";

/**
 * Every route in the app, registered up front.
 *
 * Fourteen routes over nine views: login, editor (blank and per-node),
 * search, review, graph (root picker and per-root), assets, per-node history,
 * admin, and spaces. `/` redirects to `/search`, and anything unmatched renders
 * {@link NotFoundView} rather than a blank screen.
 *
 * `/login` stands **outside** the app-shell route: the shell is the session
 * gate that redirects to login, and a gate that contained the login page
 * would gate it too. Everything under `/` passes the gate.
 *
 * Two properties this file is responsible for:
 *
 * - **Views are lazy-loaded.** CodeMirror, Mermaid, and Cytoscape are the three
 *   heaviest dependencies in the tree and each belongs to exactly one view, so
 *   route-level splitting keeps them out of the initial bundle. A view module
 *   must therefore keep a **default export**.
 * - **Paths are a contract between views.** Views link to each other by URL and
 *   never by import — the editor links to `/history/:nodeId`, search links to
 *   `/editor/:nodeId` and `/graph/:nodeId`, the graph links to `/editor/:nodeId`.
 *   Renaming a path or a parameter here breaks those links silently, so grep
 *   for the path string before changing one.
 *
 * `nodum serve` mirrors this table with an SPA catch-all: any non-`/api`,
 * non-`/healthz` GET returns `index.html`, so every route below survives a
 * reload and a bookmark.
 */

const LoginView = lazy(() => import("./views/login/LoginView"));
const AdminView = lazy(() => import("./views/admin/AdminView"));
const EditorView = lazy(() => import("./views/editor/EditorView"));
const SearchView = lazy(() => import("./views/search/SearchView"));
const ReviewView = lazy(() => import("./views/review/ReviewView"));
const GraphView = lazy(() => import("./views/graph/GraphView"));
const AssetsView = lazy(() => import("./views/assets/AssetsView"));
const HistoryView = lazy(() => import("./views/history/HistoryView"));
const SpacesView = lazy(() => import("./views/spaces/SpacesView"));

/** Shown while a view's chunk loads. */
function ViewLoading() {
  return (
    <div className="nd-view">
      <div className="nd-empty">
        <Spinner large label="Loading view" />
      </div>
    </div>
  );
}

/** Wrap a lazily-loaded view in the shared loading fallback. */
function lazyView(element: ReactNode) {
  return <Suspense fallback={<ViewLoading />}>{element}</Suspense>;
}

/** Shown for a URL no route matches. */
function NotFoundView() {
  return (
    <div className="nd-view">
      <EmptyState
        title="No such page"
        body="That URL does not match a view. Pick one from the header."
      />
    </div>
  );
}

export const router = createBrowserRouter([
  // The gate's destination. Outside the shell, which is what does the gating.
  { path: "/login", element: lazyView(<LoginView />) },
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/search" replace /> },

      // Editor — `/editor` opens a blank node, `/editor/:nodeId` an existing one.
      { path: "editor", element: lazyView(<EditorView />) },
      { path: "editor/:nodeId", element: lazyView(<EditorView />) },

      { path: "search", element: lazyView(<SearchView />) },
      { path: "review", element: lazyView(<ReviewView />) },

      // Graph — `/graph` picks a root, `/graph/:rootId` renders it.
      { path: "graph", element: lazyView(<GraphView />) },
      { path: "graph/:rootId", element: lazyView(<GraphView />) },

      { path: "assets", element: lazyView(<AssetsView />) },

      // Spaces — the lifecycle screen: what territory exists, its node counts,
      // and which agents hold grants on it (design D2). Deliberately its own
      // top-level view rather than a panel in /admin, which is the *grants*
      // screen and would frame a space as pure governance.
      { path: "spaces", element: lazyView(<SpacesView />) },

      // Admin — accounts and grants; replaces the removed policy editor.
      { path: "admin", element: lazyView(<AdminView />) },

      // History — always per node. The editor links here by URL, so the path
      // is part of the contract between the two slices: `/history/:nodeId`.
      { path: "history/:nodeId", element: lazyView(<HistoryView />) },

      { path: "*", element: <NotFoundView /> },
    ],
  },
]);
