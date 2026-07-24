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

/** What the queue is currently doing. */
export type QueueStatus = "loading" | "ready" | "error";

/** Everything {@link useReviewQueue} hands back. */
export interface ReviewQueue {
  proposals: ProposalOut[];
  status: QueueStatus;
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
      const next = await api.getReviewQueue({ limit: 500 }, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setProposals(next);
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

  return { proposals, status, error, refreshing, loadedAt, refresh };
}

/**
 * Classify a caught value for the banners the review view shows.
 *
 * A 403 should be impossible — the HTTP surface forces `actor="human"` on every
 * review call, so the human tier can never refuse it — which is exactly why it
 * gets its own loud case rather than becoming a generic red toast. The
 * unreachable/refused split is the shared one (`src/lib/failure.ts`), so the dev
 * proxy's 502 reads as "nothing was listening" here too.
 */
export type FailureKind = "forbidden" | "unreachable" | "api";

/** Which banner a failure deserves. */
export function classifyFailure(error: unknown): FailureKind {
  const kind = describeFailure(error).kind;
  if (kind === "forbidden") return "forbidden";
  if (kind === "unreachable") return "unreachable";
  return "api";
}

/** The human-readable line for a failure, server message included when there is one. */
export function failureMessage(error: unknown): string {
  return describeError(error);
}
