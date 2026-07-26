/**
 * The live space list, for the review queue's space sections.
 *
 * The queue needs the list for a reason the other views do not: `SpaceOut.grants`
 * is how an **edit-granted** space is recognised, and an edit-granted space is
 * precisely the one that never appears in this queue (its agents write `active`
 * directly). Without the list the view can render the proposals it has, but it
 * cannot say that a silent space is silent *by design* — which is the whole of
 * design decision D4 — so a failure here is reported rather than swallowed.
 *
 * It re-fetches with the queue's manual refresh: a grant changed on `/admin`
 * turns a space self-governing, and a queue that only learned the space list
 * once would keep claiming otherwise until a reload.
 *
 * Local to this view; the graph slice holds a read-only twin (`views/graph/
 * useSpaces.ts`). A third caller is the moment to hoist a shared hook into
 * `src/lib/` rather than keeping copies in step by hand.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { SpaceOut } from "../../api/types";

/** The space list, or what is known of it. */
export interface SpaceList {
  /** Active spaces, or null while loading and after a failure. */
  spaces: SpaceOut[] | null;
  /** True once the request failed. The view says so instead of implying "none". */
  failed: boolean;
  /** Re-fetch now. */
  reload: () => void;
}

/**
 * Fetch the space list, with their node counts and grants.
 *
 * @returns The spaces, whether the fetch failed, and a reload.
 */
export function useSpaces(): SpaceList {
  const [state, setState] = useState<{ spaces: SpaceOut[] | null; failed: boolean }>({
    spaces: null,
    failed: false,
  });
  const [token, setToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    api
      .listSpaces(controller.signal)
      .then((spaces) => setState({ spaces, failed: false }))
      .catch(() => {
        if (controller.signal.aborted) return;
        // The previous list is dropped: a stale one would keep asserting that a
        // space is self-governing after a grant it can no longer see.
        setState({ spaces: null, failed: true });
      });
    return () => controller.abort();
  }, [token]);

  const reload = useCallback(() => setToken((current) => current + 1), []);
  return { spaces: state.spaces, failed: state.failed, reload };
}
