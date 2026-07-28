/**
 * The review queue's data source: fetch, poll, and refresh honestly.
 *
 * There is no realtime push in Phase 3 — that is deliberate (the plan files SSE
 * under Phase 5, where the nightly consolidation cycle gives it something to
 * push). Proposals are filed by out-of-process agents, so the only way this
 * view learns about one is to ask: on an interval, and on window focus, which
 * is when a human has just come back to look.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { describeError, describeFailure } from "../../lib";
import type { ProposalOut } from "../../api/types";

/** How often the queue re-polls while the tab is visible and idle. */
export const POLL_INTERVAL_MS = 20_000;

/**
 * How many proposals one read asks for.
 *
 * `GET /api/review/queue` answers `rows[:limit]` and reports **no** total and no
 * truncation flag — the list envelope's `count` is the length of what came back
 * — so the only thing a client can know about a longer queue is that its own
 * window filled. Named here, and reported as {@link ReviewQueue.truncated},
 * because a number the toolbar prints as a total has to be one it can defend.
 */
export const REVIEW_QUEUE_LIMIT = 500;

/** What the queue is currently doing. */
export type QueueStatus = "loading" | "ready" | "error";

/** Everything {@link useReviewQueue} hands back. */
export interface ReviewQueue {
  proposals: ProposalOut[];
  status: QueueStatus;
  /**
   * Whether the read filled its window, so there may be more waiting.
   *
   * Conservative in the same way `CycleDetailOut.events_truncated` is: exactly
   * `REVIEW_QUEUE_LIMIT` rows may well be the last ones there are. It says the
   * list *may* be short and never that it provably is.
   */
  truncated: boolean;
  /** The failure that put `status` in `"error"`, if any. */
  error: unknown;
  /** True while a background poll is in flight over already-rendered data. */
  refreshing: boolean;
  /** When the list on screen was last confirmed against the server. */
  loadedAt: number | null;
  /** Re-fetch now. Awaitable, so an action can refresh before it reports. */
  refresh: () => Promise<void>;
}

/**
 * Load the pending-proposal queue and keep it current.
 *
 * @param paused Suspend polling — set while a dialog is open or an accept is in
 *   flight, so the list never re-orders under the pointer that is about to
 *   click it. A manual {@link ReviewQueue.refresh} still works while paused.
 */
export function useReviewQueue(paused: boolean): ReviewQueue {
  const [proposals, setProposals] = useState<ProposalOut[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [status, setStatus] = useState<QueueStatus>("loading");
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [loadedAt, setLoadedAt] = useState<number | null>(null);

  // Kept in a ref so the polling effect does not re-subscribe on every change.
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const inFlight = useRef<AbortController | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      inFlight.current?.abort();
    };
  }, []);

  const load = useCallback(async (background: boolean) => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    if (background) setRefreshing(true);
    try {
      const next = await api.getReviewQueue({ limit: REVIEW_QUEUE_LIMIT }, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setProposals(next);
      // The only truncation signal on this route: the server sends no total, so
      // a full window is all a client can go on.
      setTruncated(next.length >= REVIEW_QUEUE_LIMIT);
      setError(null);
      setStatus("ready");
      setLoadedAt(Date.now());
    } catch (caught) {
      if (controller.signal.aborted || !mounted.current) return;
      // A background poll that fails leaves the last good list on screen and
      // surfaces the failure as a banner; only a cold load blanks the view.
      setError(caught);
      setStatus((current) => (current === "ready" && background ? "ready" : "error"));
    } finally {
      if (mounted.current && controller.signal.aborted === false) setRefreshing(false);
      if (inFlight.current === controller) inFlight.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    await load(true);
  }, [load]);

  // Cold load.
  useEffect(() => {
    void load(false);
  }, [load]);

  // Interval poll + window focus. Both skip while paused or while the tab is
  // hidden — polling a background tab is spending the single SQLite writer's
  // neighbours for nothing.
  useEffect(() => {
    const pollIfIdle = () => {
      if (pausedRef.current || document.hidden) return;
      void load(true);
    };
    const timer = window.setInterval(pollIfIdle, POLL_INTERVAL_MS);
    const onFocus = () => pollIfIdle();
    const onVisibility = () => {
      if (!document.hidden) pollIfIdle();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load]);

  return { proposals, truncated, status, error, refreshing, loadedAt, refresh };
}

/**
 * Which banner the review view shows for a caught failure.
 *
 * Named for the view, not `FailureKind`: the shared classifier in
 * `src/lib/failure.ts` owns that name, and it means something different — the
 * *kind of failure*, which every view reads the same way. This is a choice of
 * panel, and each view makes its own.
 *
 * A 403 should be impossible — the HTTP surface forces `actor="human"` on every
 * review call, so the human tier can never refuse it — which is exactly why it
 * gets its own loud case rather than becoming a generic red toast.
 *
 * A 503 is the opposite: entirely expected. SQLite has one writer, an accept of
 * a whole run is a long write, and the review view is the surface most likely to
 * be sitting behind one. It is retryable, and saying so is the whole difference
 * between "wait a second" and "something is broken" — so it keeps its own case
 * here rather than collapsing into `api`, the way the graph view already reads
 * it (`views/graph/errors.ts`).
 */
export type ReviewFailureKind = "forbidden" | "unreachable" | "busy" | "api";

/** Which banner a failure deserves. */
export function classifyFailure(error: unknown): ReviewFailureKind {
  const kind = describeFailure(error).kind;
  if (kind === "forbidden") return "forbidden";
  if (kind === "unreachable") return "unreachable";
  if (kind === "busy") return "busy";
  return "api";
}

/** The human-readable line for a failure, server message included when there is one. */
export function failureMessage(error: unknown): string {
  return describeError(error);
}
