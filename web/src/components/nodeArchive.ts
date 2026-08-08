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
 * not make, said before the click rather than as a 400 after it, **plus one
 * refusal that is this module's own**: a space.
 *
 * A space is an ordinary node of type `space` living in `meta`, and
 * `POST /api/nodes/{id}/archive` reaches the same row `POST /api/spaces/…`
 * does — `_transition_row` says so in as many words. The server would perform
 * it. What it costs is nothing like what these lines say: every grant on the
 * space goes inert, the space stops resolving so nothing can be written or
 * granted there again, and its name stays reserved for good. That copy exists,
 * counted from the space's own row, and it lives in `views/spaces/spaces.ts`
 * where the screen that can show it is. So the node-scale control refuses and
 * points there rather than describing a write it would be describing wrongly.
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

/** The node type every space carries (`service.create_space`). */
const SPACE_TYPE = "space";

/**
 * Why this node cannot be archived *here*, or null when it can.
 *
 * Three of the four refusals are the service's own. The state one is
 * `_transition_row`'s `from_state` check: `archive` runs `active → archived`
 * and nothing else, so a proposed node is retired by rejecting it in the review
 * queue and an archived one is already there.
 *
 * The fourth — a space — is this surface's, not the server's: the write would
 * succeed and would mean something these consequence lines do not describe.
 * See the module docblock.
 *
 * @param node The node the action would archive.
 * @returns One sentence naming the reason, for a disabled control.
 */
export function archiveRefusal(node: NodeOut): string | null {
  // Before the space branch: `GET /api/spaces` is active-only, so sending
  // somebody to the Spaces screen for a space that is *already* archived
  // sends them to a screen that cannot show the row, to redo something that
  // is done. "Already archived." is the true answer for either kind of node.
  if (node.state === "archived") {
    return "Already archived.";
  }
  if (STRUCTURAL_SPACE_IDS.includes(node.id)) {
    return `${node.id} is a structural space — the server refuses to archive it.`;
  }
  if (node.type === SPACE_TYPE) {
    return (
      "This is a space, and archiving one cuts off every grant on it — a different action with " +
      "different consequences. Archive it from the Spaces screen, which states them."
    );
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
  // Reversible is the fact; the *button* is not promised, and neither is any
  // condition under which it appears. The toast withholds it whenever it
  // cannot prove the log head is this write — an agent's write landing in
  // between, a cycle-stamped head, or an event-log read that simply failed —
  // and the last of those makes "nothing else landed" insufficient as well as
  // unnecessary. A confirm naming a condition the next screen falsifies is the
  // same defect as one naming a control it does not show.
  lines.push(
    "This is reversible: the archive is one event, and undoing that event puts the node back — " +
      "offered in the confirmation that follows when it can be, and by seq on the CLI otherwise.",
  );

  return lines;
}
