/**
 * Resolving the space of a proposal the queue does not state one for.
 *
 * **This is a workaround, and it should not outlive the gap it works around.**
 * `GET /api/review/queue` states a space for a proposed *node* (`node.space_id`)
 * and for nothing else: `service._edge_context` and `service._update_context`
 * build their reviewer context out of endpoint and target *id and title*, so an
 * edge or an update arrives with no space on it at all. Design decision D4
 * makes space the outer grouping level of this queue, and a queue where every
 * edge proposal — which includes every `mentions` edge a `[[wikilink]]`
 * materialised, the commonest thing an agent files — landed under *space not
 * reported* would be a section header with nothing underneath it.
 *
 * So the missing spaces are read off the referenced nodes directly. The cost is
 * kept to something a polled view can carry:
 *
 * - the queue answers most of it for free (`spacesFromProposals`), because an
 *   agent run usually proposes the node its edges start from;
 * - every id is asked for **once ever** — the cache is a ref, so the 20-second
 *   poll re-resolves nothing;
 * - a node that answers 404 is cached as unresolvable rather than re-asked;
 * - the pass is capped ({@link MAX_SPACE_LOOKUPS}) and issued in small
 *   concurrent chunks, so a full 500-proposal queue cannot turn one render into
 *   a thousand simultaneous requests. What the cap leaves over stays in the
 *   *space not reported* section, which says so.
 *
 * The fix that deletes this file is one field on each of those two context
 * builders.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { ProposalOut } from "../../api/types";
import { referencedNodeIds, spacesFromProposals } from "./grouping";

/** Most nodes one pass will look up. Beyond this the queue says "not reported". */
export const MAX_SPACE_LOOKUPS = 200;

/** How many lookups are in flight at once. */
const CHUNK = 8;

/**
 * Space id per node id, for the endpoints and targets the queue leaves blank.
 *
 * @param proposals The current queue.
 * @returns A map to hand to `groupProposalsBySpace`'s `nodeSpaces`. Grows as
 *   lookups land; never shrinks while the view is mounted.
 */
export function useProposalSpaces(proposals: readonly ProposalOut[]): ReadonlyMap<string, string> {
  // `null` records "asked, and there is no answer" so a missing node is not
  // re-requested on every poll.
  const cache = useRef(new Map<string, string | null>());
  // How much of the cache the caller has already been handed. Entries are never
  // removed and never change, so the size is a sufficient identity — and it
  // keeps a re-render from being issued for a map nobody's answer changed.
  const publishedSize = useRef(0);
  const [resolved, setResolved] = useState<ReadonlyMap<string, string>>(new Map());

  // Keyed on the id list rather than on `proposals`, whose identity changes on
  // every poll even when the queue is unchanged. Ids the queue already states a
  // space for are dropped here, so the effect never sees them.
  const stated = spacesFromProposals(proposals);
  const wantedKey = referencedNodeIds(proposals)
    .filter((id) => !stated.has(id))
    .join(",");

  useEffect(() => {
    const controller = new AbortController();

    const publish = () => {
      const next = new Map<string, string>();
      for (const [nodeId, spaceId] of cache.current) {
        if (spaceId !== null) next.set(nodeId, spaceId);
      }
      if (next.size === publishedSize.current) return;
      publishedSize.current = next.size;
      setResolved(next);
    };

    void (async () => {
      // A previous run aborted mid-chunk may have cached answers it never got
      // to hand over. Publishing first is what stops those being stranded.
      publish();
      const wanted = (wantedKey ? wantedKey.split(",") : [])
        .filter((id) => !cache.current.has(id))
        .slice(0, MAX_SPACE_LOOKUPS);
      if (wanted.length === 0) return;

      for (let start = 0; start < wanted.length; start += CHUNK) {
        if (controller.signal.aborted) return;
        await Promise.all(
          wanted.slice(start, start + CHUNK).map(async (nodeId) => {
            try {
              const node = await api.getNode(nodeId, controller.signal);
              cache.current.set(nodeId, node.space_id);
            } catch {
              // Unknown, unreachable, or refused: record the miss so the poll
              // does not ask again, and let the grouping report it honestly.
              if (!controller.signal.aborted) cache.current.set(nodeId, null);
            }
          }),
        );
        if (!controller.signal.aborted) publish();
      }
    })();

    return () => controller.abort();
  }, [wantedKey]);

  return resolved;
}
