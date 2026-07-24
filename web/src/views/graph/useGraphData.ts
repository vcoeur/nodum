/**
 * Data hooks for the graph view: the bounded subgraph read and the path query.
 *
 * Both abort in flight on cleanup, so a fast sequence of filter changes or a
 * navigation away never lands a stale response on a live component.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { PathOut, SubgraphOut } from "../../api/types";
import type { GraphFilters } from "./filters";
import { filterKey, toSubgraphParams } from "./filters";

/**
 * How long a filter change waits before it becomes a request.
 *
 * Long enough that dragging the depth slider issues one call rather than eight,
 * short enough that a deliberate change still feels immediate.
 */
export const FETCH_DEBOUNCE_MS = 250;

/** The lifecycle of one async read. */
export type LoadStatus = "idle" | "loading" | "ready" | "error";

/** State of the subgraph read. */
export interface SubgraphState {
  status: LoadStatus;
  /** The last successful result, kept while a new one loads. */
  data: SubgraphOut | null;
  error: unknown;
  /** True while a request is in flight over data from an earlier parameter set. */
  stale: boolean;
}

const IDLE: SubgraphState = { status: "idle", data: null, error: null, stale: false };

/**
 * Fetch the bounded subgraph for a root and filter set.
 *
 * The request is debounced, so moving a slider does not issue a call per frame,
 * and the previous result is kept on screen while the next one loads — a graph
 * that blanks on every keystroke is unusable even when it is fast.
 *
 * @param rootId The root, or undefined while no root is chosen.
 * @param filters The active filters.
 * @param reloadToken Bump to force a refetch with unchanged parameters.
 * @returns The current read state.
 */
export function useSubgraph(
  rootId: string | undefined,
  filters: GraphFilters,
  reloadToken: number,
): SubgraphState {
  const [state, setState] = useState<SubgraphState>(IDLE);

  // The effect keys on `filterKey`, which fully determines the request, so the
  // parameters themselves are read through a ref rather than listed as deps.
  const latest = useRef({ rootId, filters });
  latest.current = { rootId, filters };

  const key = filterKey(rootId, filters);

  useEffect(() => {
    const { rootId: root, filters: active } = latest.current;
    if (!root) {
      setState(IDLE);
      return;
    }

    setState((previous) => {
      // A result for a different root is not a useful placeholder — drop it.
      const carried = previous.data && previous.data.root === root ? previous.data : null;
      return { status: "loading", data: carried, error: null, stale: carried !== null };
    });

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api
        .getSubgraph(toSubgraphParams(root, active), controller.signal)
        .then((data) => setState({ status: "ready", data, error: null, stale: false }))
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          setState((previous) => ({
            status: "error",
            data: previous.data,
            error: cause,
            stale: false,
          }));
        });
    }, FETCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [key, reloadToken]);

  return state;
}

/** State of the path query. */
export interface PathState {
  status: LoadStatus;
  data: PathOut | null;
  error: unknown;
}

const PATH_IDLE: PathState = { status: "idle", data: null, error: null };

/**
 * Find the shortest path between two nodes.
 *
 * Deliberately not debounced: both endpoints are picked by an explicit action,
 * so there is no stream of intermediate values to swallow.
 *
 * Note that `find_path` walks **every active edge** in the graph and ignores
 * the filters above — the result can therefore leave the rendered subgraph
 * entirely. The view says so rather than drawing a path it cannot support.
 *
 * @param a Start node id, or null.
 * @param b End node id, or null.
 * @returns The current query state; idle until both ends are set.
 */
export function usePath(a: string | null, b: string | null): PathState {
  const [state, setState] = useState<PathState>(PATH_IDLE);

  useEffect(() => {
    if (!a || !b) {
      setState(PATH_IDLE);
      return;
    }
    setState({ status: "loading", data: null, error: null });
    const controller = new AbortController();
    api
      .findPath(a, b, controller.signal)
      .then((data) => setState({ status: "ready", data, error: null }))
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", data: null, error: cause });
      });
    return () => controller.abort();
  }, [a, b]);

  return state;
}
