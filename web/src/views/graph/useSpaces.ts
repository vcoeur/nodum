/**
 * The live space list, fetched once per mount, for the graph's space filter.
 *
 * Local to this view. `src/components/SpaceFilter.tsx` is controlled and
 * presentational by design — each view owns the value and the fetch — so this
 * is the graph's half of that contract. The review slice holds a twin; if a
 * third view needs one, that is the moment to hoist a shared `useSpaces` into
 * `src/lib/` rather than keeping copies in step by hand.
 *
 * A failure is reported rather than swallowed: without the list the filter
 * cannot resolve a space name to an id, so the view has to say the control is
 * unavailable instead of quietly rendering "Any space" over a narrowed URL.
 */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { SpaceOut } from "../../api/types";

/** The space list, or what is known of it. */
export interface SpaceList {
  /** Active spaces, or null while loading and after a failure. */
  spaces: SpaceOut[] | null;
  /** True once the request failed; the picker says so rather than offering "any". */
  failed: boolean;
}

/**
 * Fetch the space list.
 *
 * @returns The spaces and whether the fetch failed.
 */
export function useSpaces(): SpaceList {
  const [state, setState] = useState<SpaceList>({ spaces: null, failed: false });

  useEffect(() => {
    const controller = new AbortController();
    api
      .listSpaces(controller.signal)
      .then((spaces) => setState({ spaces, failed: false }))
      .catch(() => {
        if (controller.signal.aborted) return;
        setState({ spaces: null, failed: true });
      });
    return () => controller.abort();
  }, []);

  return state;
}
