/**
 * What archiving *one node* costs, and when the action may be offered at all.
 *
 * `views/spaces/spaces.ts` owns the same copy one scale up, and the rule it
 * states holds here too: **every line has to be a fact the service delivers.**
 * These are the facts, each read off the service rather than assumed:
 *
 * - Nothing is deleted. `_transition_row` writes `state = 'archived'` and
 *   nothing else; content, props, and the whole version history stay, and the
 *   node keeps answering at its own id — which is why the reading view can
 *   still open it afterwards.
 * - **Its edges do not go with it.** Archiving settles no edges: only `accept`
 *   and `reject` run `_settle_synthesis_edges`, and the graph walk filters on
 *   *edge* state, never node state. So every incident edge stays active and the
 *   node keeps appearing in its neighbours' rails and in the graph. This is the
 *   line a human would otherwise assume the opposite of.
 * - Search stops finding it: `search.search` filters `state = 'active'` by
 *   default, so it drops out of every query that does not widen the filter.
 * - It is reversible, and the confirmation says so because it is the one thing
 *   that makes archiving a node different from the cliff archiving a space is.
 *
 * {@link archiveRefusal} is the other half — the transitions the service will
 * not make, said before the click rather than as a 400 after it.
 */

import type { NodeOut } from "../api/types";

/**
 * The spaces the schema creates; `_transition_row` refuses to archive either.
 *
 * Matched by id, exactly as the service matches them. Duplicated from
 * `views/spaces/spaces.ts` rather than imported: a view never imports another
 * view's module, and this is the shared tier the rule now also lives in.
 */
const STRUCTURAL_SPACE_IDS: readonly string[] = ["main", "meta"];

/**
 * Why this node cannot be archived, or null when it can.
 *
 * Three refusals, all the service's own. The state one is
 * `_transition_row`'s `from_state` check: `archive` runs `active → archived`
 * and nothing else, so a proposed node is retired by rejecting it in the review
 * queue and an archived one is already there.
 *
 * @param node The node the action would archive.
 * @returns One sentence naming the reason, for a disabled control.
 */
export function archiveRefusal(node: NodeOut): string | null {
  if (STRUCTURAL_SPACE_IDS.includes(node.id)) {
    return `${node.id} is a structural space — the server refuses to archive it.`;
  }
  if (node.state === "archived") {
    return "Already archived.";
  }
  if (node.state === "proposed") {
    return "Only an active node can be archived — reject it in the review queue instead.";
  }
  return null;
}

/**
 * What archiving this node will and will not do, one sentence per line.
 *
 * @param node The node being archived.
 * @param edgeCount How many edges are incident to it, or null when the calling
 *   surface has not read its neighbourhood. The edge line is stated either way
 *   — that edges survive is the point — and merely counted when known.
 */
export function archiveConsequences(node: NodeOut, edgeCount: number | null): string[] {
  const label = node.title?.trim() ? node.title : "This node";
  const lines: string[] = [];

  lines.push(
    `${label} is marked archived. Nothing is deleted: its content, its props and its whole ` +
      "version history stay, and it keeps answering at its own id — this page included.",
  );
  lines.push(
    "Search stops finding it, because search reads active nodes unless the state filter says otherwise.",
  );
  lines.push(
    edgeCount === null
      ? "Its edges are not archived with it: each one stays active, so the node still appears in its neighbours' edge rails and in the graph."
      : edgeCount === 0
        ? "Nothing connects to it, so no edge is affected."
        : `Its ${edgeCount} edge${edgeCount === 1 ? "" : "s"} ${edgeCount === 1 ? "is" : "are"} not archived with it: ` +
          `${edgeCount === 1 ? "it stays" : "they stay"} active, so the node still appears in its ` +
          "neighbours' edge rails and in the graph.",
  );
  lines.push(
    "This is reversible — the confirmation that follows offers an undo for exactly this event.",
  );

  return lines;
}
