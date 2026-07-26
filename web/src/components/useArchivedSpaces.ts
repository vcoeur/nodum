/**
 * The archived spaces a screen still holds references to.
 *
 * A space id outlives the listing that names it. Archive `research` and every
 * node, proposal, grant and search hit already filed there goes on reporting
 * that id — but `GET /api/spaces` is active-only, so each of them degrades to a
 * bare 32-hex string with nothing saying it is the space just retired.
 *
 * **Widening the shared endpoint was the wrong fix and stays rejected**: it is
 * the read behind {@link useSpaces} and therefore behind six pickers, and an
 * archived space has no business in any of them (a space is retired precisely
 * so it stops being offered). This is the other shape — a **lazy, view-local**
 * read of exactly the rows that cannot otherwise be named, through the ordinary
 * node listing a human can already run (`nodum node list --space meta
 * --include-meta --type space --state archived`). The shared vocabulary is
 * untouched.
 *
 * It is also not the per-row backfill this phase deleted (`useProposalSpaces.ts`,
 * a chunked `getNode` walk over every referenced node): one listing, fired only
 * when {@link unresolvedSpaceIds} finds something — which on a healthy file is
 * never — and a failure costs nothing but the id the screen was already
 * showing. It lives here rather than in one view because five surfaces name a
 * space (the review queue, search, the editor's meta bar, the graph inspector
 * and `/admin`'s grant table) and five copies of one fetch is what
 * {@link useSpaces} already had to be collapsed out of once.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { NodeOut } from "../api/types";

/** How many archived spaces one read pulls; a file with more has other problems. */
const ARCHIVED_SPACE_LIMIT = 200;

/** What {@link useArchivedSpaces} hands back. */
export interface ArchivedSpaces {
  /** The archived space nodes, or an empty list until one is needed. */
  spaces: NodeOut[];
  /** Re-run the read; a no-op until something needs it. */
  reload: () => Promise<void>;
}

/**
 * Read the archived space nodes, lazily.
 *
 * @param needed True once the screen holds a reference to a space the active
 *   list cannot name. False keeps the request unsent. Callers derive it from
 *   {@link unresolvedSpaceIds}, which answers empty while the active list is
 *   still loading — so this stays unsent through that window rather than firing
 *   on every mount.
 */
export function useArchivedSpaces(needed: boolean): ArchivedSpaces {
  const [spaces, setSpaces] = useState<NodeOut[]>([]);
  const mounted = useRef(true);
  // Whether the read has ever been wanted. Only `reload` consults it — the
  // effect below is keyed on `needed`, which is what keeps the request to one
  // per transition rather than one per poll.
  const everNeeded = useRef(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const listed = await api.listNodes(
        {
          space: "meta",
          include_meta: true,
          type: "space",
          state: "archived",
          limit: ARCHIVED_SPACE_LIMIT,
        },
        signal,
      );
      if (!mounted.current || signal?.aborted) return;
      setSpaces(listed);
    } catch {
      // Nothing to report: the surface falls back to the space id it was
      // already showing, which is honest. A banner for a lookup the human never
      // asked for would be noise on a screen about something else.
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // `needed` is a boolean and `load` is stable, so this fires when the screen
  // first reports a space the active list cannot name, and not again — a ref
  // guard would be the one that breaks, since StrictMode's second pass would
  // find it already set and skip the read that its own cleanup just aborted.
  useEffect(() => {
    if (!needed) return;
    everNeeded.current = true;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [needed, load]);

  const reload = useCallback(async () => {
    if (!everNeeded.current) return;
    await load();
  }, [load]);

  return { spaces, reload };
}
