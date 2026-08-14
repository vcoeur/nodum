/**
 * The reading view's edge rail and backlinks, from `getNode(id, { depth: 1 })`.
 *
 * The neighborhood read returns the root plus every active edge it walks,
 * and the rail is the part of that walk *incident to the root*: one row per
 * edge, oriented, with the far node attached. The walk is "both" directions,
 * so every incident edge of the root is present and nothing else is; the
 * logic here narrows the envelope to exactly that and derives the two facts
 * a row renders — direction and crossing — so the component stays a renderer.
 *
 * **Backlinks are the same read, narrowed differently.** An inbound `mentions`
 * edge is a `[[wikilink]]` somebody wrote at this node (`service`
 * materialises exactly those), and the rail listing it by type and title
 * answers *that* somebody linked here without answering *what they said*. The
 * far node's content is in the same envelope, so the sentence around the link
 * costs no request — {@link mentionContext} finds it.
 *
 * The match is **exact**, on the node's id or its title, because that is what
 * `service._resolve_wikilink` matched to create the edge: exact id first, then
 * exact title. A looser match here would show a snippet around a link that is
 * not the one the edge came from.
 */

import type { EdgeOut, NodeOut, SubgraphOut } from "../../api/types";

/** One incident edge of the root, ready to render. */
export interface IncidentRow {
  edge: EdgeOut;
  /** The root node, retained so edge actions can name both endpoints. */
  near: NodeOut;
  /** `out` when the root is the edge's source, `in` when it is its target. */
  direction: "out" | "in";
  /** The far node, from the same walk; null only if the envelope lies. */
  far: NodeOut | null;
  /** True when the two endpoints live in different spaces (design D5). */
  crossing: boolean;
}

/**
 * The rail header's count text.
 *
 * A truncated neighbourhood read (`SubgraphOut.truncated`) means the count is
 * the walk's cap, not the neighbourhood's size, so it is stated as a floor
 * ("200+ edges") rather than presented as fact.
 *
 * @param count The number of incident rows.
 * @param truncated Whether the neighbourhood read hit a cap.
 * @returns The header text, pluralised unless truncated makes it approximate.
 */
export function edgeCountLabel(count: number, truncated: boolean): string {
  if (truncated) return `${count}+ edges`;
  return `${count} edge${count === 1 ? "" : "s"}`;
}

/**
 * The root's incident edges, outgoing first, in edge-creation order.
 *
 * @param subgraph The `depth: 1` neighborhood read.
 * @returns The rows, or an empty list when nothing is incident.
 */
export function incidentRows(subgraph: SubgraphOut): IncidentRow[] {
  const root = subgraph.nodes[0];
  if (root === undefined) return [];
  const byId = new Map(subgraph.nodes.map((node) => [node.id, node]));

  const rows: IncidentRow[] = [];
  for (const edge of subgraph.edges) {
    let direction: "out" | "in";
    let farId: string;
    if (edge.src_id === root.id) {
      direction = "out";
      farId = edge.dst_id;
    } else if (edge.dst_id === root.id) {
      direction = "in";
      farId = edge.src_id;
    } else {
      continue; // not incident — an envelope this read cannot produce
    }
    const far = byId.get(farId) ?? null;
    rows.push({
      edge,
      near: root,
      direction,
      far,
      crossing: far !== null && far.space_id !== null && far.space_id !== root.space_id,
    });
  }

  rows.sort((a, b) => {
    if (a.direction !== b.direction) return a.direction === "out" ? -1 : 1;
    return a.edge.created_at < b.edge.created_at ? -1 : 1;
  });
  return rows;
}

/* ------------------------------------------------------------------ */
/* Backlinks                                                           */
/* ------------------------------------------------------------------ */

/** The edge type a materialised `[[wikilink]]` creates. */
const MENTIONS = "mentions";

/** How much prose around a mention a backlink shows. */
export const BACKLINK_CONTEXT_LIMIT = 220;

/** One inbound mention: who links here, and what they said around the link. */
export interface Backlink {
  /** The `mentions` edge itself. */
  edge: EdgeOut;
  /** The node whose content carries the wikilink. */
  from: NodeOut;
  /**
   * The prose around the link, whitespace-collapsed and elided.
   *
   * Null when the wikilink cannot be located in the content — an edge whose
   * link has since been edited away but which no human has archived, or a
   * mention created some other way. A backlink with no context is still shown:
   * the edge is the fact, the snippet is the courtesy.
   */
  context: string | null;
  /** True when the mentioning node lives in another space (design D5). */
  crossing: boolean;
}

/**
 * The nodes that link here, with the sentence each one links from.
 *
 * @param subgraph The `depth: 1` neighborhood read.
 * @returns One entry per inbound `mentions` edge, oldest first; empty when
 *   nothing mentions the root.
 */
export function backlinks(subgraph: SubgraphOut): Backlink[] {
  const root = subgraph.nodes[0];
  if (root === undefined) return [];
  // Exactly what `_resolve_wikilink` matches on, in the same order.
  const targets = root.title === null ? [root.id] : [root.id, root.title];

  return incidentRows(subgraph)
    .filter((row) => row.direction === "in" && row.edge.type === MENTIONS && row.far !== null)
    .map((row) => {
      const from = row.far as NodeOut;
      return {
        edge: row.edge,
        from,
        context: mentionContext(from.content, targets),
        crossing: row.crossing,
      };
    });
}

/**
 * The prose around the first `[[target]]` in some content.
 *
 * The window is centred on the link and clipped to whole content, then
 * whitespace-collapsed so a snippet spanning a line break reads as one
 * sentence. The `[[…]]` itself is left in: in a collapsed excerpt it is the
 * only thing that says *which* phrase is the link.
 *
 * @param content The mentioning node's raw Markdown.
 * @param targets What the wikilink may name — the node's id and its title.
 * @param limit How many characters of context to show.
 * @returns The snippet, elided at either end when it is not the whole content,
 *   or null when no wikilink names any target.
 */
export function mentionContext(
  content: string,
  targets: readonly string[],
  limit: number = BACKLINK_CONTEXT_LIMIT,
): string | null {
  let at = -1;
  let match = "";
  for (const target of targets) {
    if (target === "") continue;
    const needle = `[[${target}]]`;
    const found = content.indexOf(needle);
    if (found !== -1 && (at === -1 || found < at)) {
      at = found;
      match = needle;
    }
  }
  if (at === -1) return null;

  const padding = Math.max(0, limit - match.length);
  let start = safeBoundary(content, at - Math.floor(padding / 2));
  let end = safeBoundary(content, start + limit);
  // Running off the end buys room at the front rather than a short snippet.
  if (end >= content.length) {
    end = content.length;
    start = safeBoundary(content, Math.max(0, end - limit));
  }

  const slice = content.slice(start, end).replace(/\s+/g, " ").trim();
  if (slice === "") return null;
  return `${start > 0 ? "…" : ""}${slice}${end < content.length ? "…" : ""}`;
}

/**
 * An index that is not inside a surrogate pair, clamped to the string.
 *
 * Slicing at an arbitrary offset would otherwise cut an astral-plane character
 * — an emoji, a rarer CJK ideograph — in half and leave a replacement glyph in
 * the snippet.
 *
 * @param text The string being sliced.
 * @param index The desired offset.
 */
function safeBoundary(text: string, index: number): number {
  if (index <= 0) return 0;
  if (index >= text.length) return text.length;
  const code = text.charCodeAt(index);
  return code >= 0xdc00 && code <= 0xdfff ? index - 1 : index;
}
