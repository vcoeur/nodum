/**
 * Title-prefix lookup for the root picker.
 *
 * Thin on purpose: `suggest_links` reads the `nodes` table directly rather than
 * the FTS projector, so the picker answers on a database whose projectors have
 * never run — which is exactly the state a fresh graph is in, and the state in
 * which a search-backed picker would otherwise come back empty and look broken.
 *
 * Archived titles never appear (a retired node is not a link target), so the
 * candidate list is `active` plus `proposed`.
 */

import { api } from "../../api/client";

/** One candidate root. */
export interface RootCandidate {
  id: string;
  title: string | null;
  type: string;
}

/**
 * Look up nodes whose title starts with a prefix.
 *
 * An empty prefix matches every titled node, capped by `limit` — the "the
 * picker just opened" case.
 *
 * @param prefix What the user has typed.
 * @param limit Maximum candidates.
 * @param signal Abort signal from the caller's effect cleanup.
 * @returns Candidate roots, best match first.
 */
export async function searchRoots(
  prefix: string,
  limit: number,
  signal?: AbortSignal,
): Promise<RootCandidate[]> {
  const nodes = await api.suggestLinks(prefix, limit, signal);
  return nodes.map((node) => ({ id: node.id, title: node.title, type: node.type }));
}

/**
 * A handful of nodes to offer when the picker has no query yet.
 *
 * `listNodes` orders by creation time, so this is "what is in the graph", not a
 * ranking — the label in the UI says as much.
 *
 * @param limit Maximum rows.
 * @param signal Abort signal.
 * @returns Candidate roots.
 */
export async function listRootCandidates(
  limit: number,
  signal?: AbortSignal,
): Promise<RootCandidate[]> {
  const nodes = await api.listNodes({ limit }, signal);
  return nodes.map((node) => ({ id: node.id, title: node.title, type: node.type }));
}
