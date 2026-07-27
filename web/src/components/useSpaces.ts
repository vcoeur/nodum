/**
 * The live space list — the shared read behind every space surface.
 *
 * `GET /api/spaces` is the only listing that carries spaces (they are nodes in
 * meta, which `/api/nodes` excludes by default), so every screen that names a
 * space needs it: the filter's vocabulary, the write-target picker, the grant
 * picker, the review queue's self-governing sections, and the `/spaces` screen
 * itself. Six views each wrote this fetch; they are all this now.
 *
 * It lives beside {@link SpaceFilter} rather than in `src/lib/` because that
 * component is controlled and presentational by design — it renders the value
 * and the list it is handed — and this is the other half of that contract. The
 * pair belongs together; `src/lib/` is the plain-function tier.
 *
 * Two behaviours are load-bearing rather than incidental:
 *
 * - **a failure drops the previous list rather than keeping it.** A stale list
 *   would go on asserting that a space is self-governing after a grant it can
 *   no longer see, and would let a filter render "Any space" over a narrowed
 *   read. `failed` is what a control says instead;
 * - **`reload` is awaitable and re-runs the request.** A grant changed on
 *   `/admin` can turn a space self-governing, and a lifecycle action on
 *   `/spaces` changes the list outright — a view that learned the list once
 *   would keep claiming otherwise until a reload of the page.
 *
 * `error` is exposed alongside `failed` for the one view that *escalates* a
 * missing space list instead of degrading: `/admin` cannot offer a grant
 * without the vocabulary to grant over, so it renders the shared failure panel
 * rather than an empty picker that reads as "nothing left to grant".
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { SpaceOut } from "../api/types";

/** What is known of the space list. */
export interface SpaceList {
  /** Active spaces, or null while loading and after a failure. */
  spaces: SpaceOut[] | null;
  /** True once the request failed; a control says so rather than offering "any". */
  failed: boolean;
  /** What it failed with, for a view that escalates rather than degrades. */
  error: unknown;
  /** Re-run the request. Resolves once the state has settled. */
  reload: () => Promise<void>;
}

/** The three state fields, moved together so no render sees two of three. */
type SpaceListState = Pick<SpaceList, "spaces" | "failed" | "error">;

const IDLE: SpaceListState = { spaces: null, failed: false, error: null };

/**
 * Fetch the active spaces, with their live node counts and grant holders.
 *
 * @returns The list, whether it failed, what it failed with, and a reload.
 */
export function useSpaces(): SpaceList {
  const [state, setState] = useState<SpaceListState>(IDLE);
  // `reload` carries no signal — it is called from an event handler, not an
  // effect — so this is what stops a request in flight from writing into a
  // component that has since unmounted.
  const mounted = useRef(true);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const listed = await api.listSpaces(signal);
      if (!mounted.current || signal?.aborted) return;
      setState({ spaces: listed, failed: false, error: null });
    } catch (error) {
      if (!mounted.current || signal?.aborted) return;
      setState({ spaces: null, failed: true, error });
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void load(controller.signal);
    return () => {
      mounted.current = false;
      controller.abort();
    };
  }, [load]);

  const reload = useCallback(() => load(), [load]);

  return { ...state, reload };
}
