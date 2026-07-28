/**
 * The titles of the nodes an edge event names.
 *
 * **Why this fetch exists at all.** A node event carries its own row, title
 * included; an edge event carries `src_id` and `dst_id` and nothing else. Every
 * event a consolidation cycle writes is an edge, so without a lookup the whole
 * of the journal's diff reads `duplicate_of: cba85bd8… → 9310f1b3…` — the exact
 * rows the review queue, on the same build, renders as *"event sourcing → Event
 * Sourcing"*. It can do that because `list_proposals` attaches `{id, title,
 * space_id}` per referenced node; `list_events` attaches nothing, because it is
 * the append-only log and the log is a record of rows rather than a view.
 *
 * **Why it is one request per node.** `GET /api/nodes` has no id filter — its
 * filters are type, state, parent and space — so there is no batch read to make
 * here, and inventing one would be a server change on a surface this view does
 * not own. So the lookup is `GET /api/nodes/{id}`, and every property below
 * exists to keep that honest:
 *
 * - **it reads a page, never a cycle.** `EventDiff` pages at
 *   {@link EVENT_PAGE_SIZE}, and the ids come from `referencedNodeIds` over the
 *   rendered page alone — so what is fetched is bounded by what is on screen,
 *   and a 500-event cycle costs the same per page as a 12-event one;
 * - **it asks once per id, ever.** Every answer is written into the map,
 *   *including* a failure (as `null`), so a node that no longer resolves is not
 *   re-requested on every render. The map is the record of what has been asked;
 *   there is deliberately no `useRef` guard, because StrictMode's second pass
 *   would find one already set and skip the read its own cleanup just aborted;
 * - **a failure costs nothing.** The endpoint falls back to the shortened id the
 *   diff was already showing, which is honest. A banner for a lookup the human
 *   never asked for would be noise on a page about something else.
 *
 * This is not the per-row backfill the spaces phase deleted (`useProposalSpaces`,
 * a chunked walk over every node in a queue). That one was deleted because the
 * server already sent the answer and the walk was a second, slower copy of it.
 * Here the server sends the row and the row is what the log holds.
 */

import { useEffect, useState } from "react";
import { api } from "../../api/client";

/** How many lookups are in flight at once — enough to be quick, few enough to be polite. */
const LOOKUP_CONCURRENCY = 6;

/**
 * Most ids one page may ask for.
 *
 * A page of {@link EVENT_PAGE_SIZE} edge events names at most twice that many
 * nodes, so this is slack rather than a limit anything reaches. It is here so
 * that a page size raised without a second thought cannot turn into a hundred
 * requests unnoticed.
 */
const MAX_LOOKUPS = 120;

/**
 * Resolve node titles for the ids currently on screen.
 *
 * @param ids The node ids the rendered page names — memoise it in the caller,
 *   since a fresh array every render would re-run the effect (the effect keys on
 *   the ids' text, so a re-run is harmless, but the render is not free).
 * @returns Title by id for everything asked so far: a string when the node has
 *   one, `null` when it has none or did not resolve, and *absent* while the
 *   lookup is still in flight — which is what lets the diff render the
 *   shortened id in the meantime rather than an empty cell.
 */
export function useNodeTitles(ids: readonly string[]): ReadonlyMap<string, string | null> {
  const [titles, setTitles] = useState<ReadonlyMap<string, string | null>>(new Map());
  // The effect keys on the ids' *text* rather than the array's identity, so a
  // caller that rebuilds an equal array does not restart the lookups.
  const wantedKey = ids.join(",");

  useEffect(() => {
    const missing = ids.filter((id) => !titles.has(id)).slice(0, MAX_LOOKUPS);
    if (missing.length === 0) return;

    const controller = new AbortController();
    let cancelled = false;

    void (async () => {
      const found = new Map<string, string | null>();
      for (let start = 0; start < missing.length; start += LOOKUP_CONCURRENCY) {
        if (cancelled) return;
        const batch = missing.slice(start, start + LOOKUP_CONCURRENCY);
        const rows = await Promise.all(
          batch.map((id) =>
            api.getNode(id, controller.signal).then(
              (node) => node.title,
              // Answered with nothing: a node the cycle wrote and an `undo`
              // has since taken back reads exactly like this, and it is a
              // shortened id on screen rather than a retry loop.
              () => null,
            ),
          ),
        );
        batch.forEach((id, index) => found.set(id, rows[index] ?? null));
      }
      if (cancelled) return;
      setTitles((current) => new Map([...current, ...found]));
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
    // `ids` is deliberately not a dependency — it is read through `wantedKey`,
    // and depending on the array itself would restart the lookups on every
    // render. `titles` is one, because it is the record of what has already
    // been asked: settling it re-runs this effect exactly once, to find nothing
    // left to do.
  }, [wantedKey, titles]);

  return titles;
}
