/**
 * The 401 broadcast — one narrow channel between the API client and the shell.
 *
 * A 401 from any route but login means the session is gone: expired, logged
 * out elsewhere, or the human disabled. There is exactly one correct reaction
 * — send the user to the login view — and only the app shell can navigate, so
 * the client (`src/api/client.ts`, the only `fetch` in the app) reports the
 * 401 here and the shell subscribes. The in-flight request still rejects with
 * an `ApiError` the view catches as usual; the route change is simply already
 * underway.
 *
 * A subscription rather than a `location.assign("/login")` in the client, so
 * the redirect goes through the router (no full reload) and the shell can pass
 * along the page the user was on for the post-login return.
 */

/** What runs when the session dies. Takes no argument — the reaction is fixed. */
export type UnauthorizedListener = () => void;

const listeners = new Set<UnauthorizedListener>();

/**
 * Subscribe to session death.
 *
 * @param listener Called synchronously for every 401 the client reports.
 * @returns The unsubscribe function.
 */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Report that the API answered 401 — the session is gone.
 *
 * Called only by the API client. A listener that throws must not keep the
 * others from running: the redirect is not optional.
 */
export function reportUnauthorized(): void {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // A broken listener is not a reason to skip the rest.
    }
  }
}
