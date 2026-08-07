/**
 * The pure model behind the create-link dialog (`components/LinkDialog.tsx`).
 *
 * The dialog is the first caller of `api.createEdge`, and everything about the
 * edge it builds that is worth testing lives here rather than in the
 * component: the direction↔edge-type pairing, the target-search fallback, the
 * confidence parse, and the debounce that keeps the search off the typing
 * path.
 *
 * ## The direction model
 *
 * The From node anchors the dialog, and direction is a first-class control:
 * "outgoing" means an edge *from* the From node to the target, "incoming"
 * means one *into* it. The edge-type catalog seeds every inverse pair as two
 * rows (`supports` ↔ `supported_by`, …), so flipping the direction swaps the
 * selected type for its catalog inverse — the label the plan promised
 * ("outgoing shows `supports`, incoming shows `supported_by`") — and the
 * polarity flips with it, so the two states describe the same fact.
 * Symmetric relations (`relates_to`, `duplicate_of`, `mentions`) are their
 * own inverse and stay put.
 *
 * ## The target search
 *
 * The target input reuses `suggestLinks`, the same title-prefix read the
 * editor's `[[` autocomplete uses, and falls back to a full hybrid `search`
 * when the prefix matches nothing — so a title that only matches mid-word or
 * in content is still reachable.
 */

import type {
  CreateEdgeBody,
  EdgeTypeOut,
  NodeOut,
  NodeState,
  SearchHit,
  SearchResult,
} from "../api/types";

/** Which way the edge points relative to the dialog's From node. */
export type LinkDirection = "out" | "in";

/** Everything `edgeBody` needs to build the request. */
export interface LinkEdgeInput {
  /** The dialog's From node. */
  sourceId: string;
  /** The chosen target node. */
  targetId: string;
  direction: LinkDirection;
  /**
   * The selected edge type as displayed for the current direction — already
   * the inverse form when the direction was flipped.
   */
  edgeType: string;
  /** The parsed confidence, or null when the field was left unset. */
  confidence: number | null;
}

/**
 * Swap an edge type for its catalog inverse.
 *
 * @param edgeTypes The live edge-type catalog.
 * @param typeId The selected type.
 * @returns The inverse id, or the type itself when the catalog has no row for
 *   it (which also covers the symmetric types, whose inverse is themselves).
 */
export function inverseEdgeType(edgeTypes: readonly EdgeTypeOut[], typeId: string): string {
  const entry = edgeTypes.find((candidate) => candidate.id === typeId);
  return entry?.inverse_name ?? typeId;
}

/**
 * The type a fresh dialog selects: `relates_to` when the catalog offers it,
 * else the first catalog entry — a neutral default either way.
 *
 * @param edgeTypes The live edge-type catalog.
 * @returns The default type id, or null when the catalog is empty.
 */
export function preferredEdgeType(edgeTypes: readonly EdgeTypeOut[]): string | null {
  const sorted = [...edgeTypes].sort((a, b) => a.id.localeCompare(b.id));
  return sorted.find((entry) => entry.id === "relates_to")?.id ?? sorted[0]?.id ?? null;
}

/**
 * Build the `POST /api/edges` body from the dialog's form.
 *
 * Incoming swaps the endpoints so the From node stays the edge's anchor: the
 * created edge always *points at* the From node when the direction is `in`.
 *
 * @param input The form as displayed.
 * @returns The request body, `confidence` null when the field was unset.
 */
export function edgeBody(input: LinkEdgeInput): CreateEdgeBody {
  const { sourceId, targetId, direction, edgeType, confidence } = input;
  return direction === "out"
    ? { src_id: sourceId, dst_id: targetId, type: edgeType, confidence }
    : { src_id: targetId, dst_id: sourceId, type: edgeType, confidence };
}

/** How the optional confidence field parsed. */
export type ConfidenceParse =
  /** `value` is null when the field was left empty — the default state. */
  | { ok: true; value: number | null }
  /** `reason` is a sentence the dialog can show under the field. */
  | { ok: false; reason: string };

/**
 * Parse the confidence field, matching the server's own range.
 *
 * The service refuses a confidence outside `[0, 1]` (`service.create_edge`),
 * and the dialog rejects it client-side so a stray keystroke never costs a
 * round trip.
 *
 * @param text The raw field value.
 * @returns The parsed value, null when unset, or a refusal.
 */
export function parseConfidence(text: string): ConfidenceParse {
  const trimmed = text.trim();
  if (trimmed === "") return { ok: true, value: null };
  const value = Number(trimmed);
  if (!Number.isFinite(value)) return { ok: false, reason: "Confidence must be a number." };
  if (value < 0 || value > 1) return { ok: false, reason: "Confidence must be between 0 and 1." };
  return { ok: true, value };
}

/** One row in the target results, normalised across the two read shapes. */
export interface TargetCandidate {
  nodeId: string;
  title: string;
  type: string;
  spaceId: string | null;
  /** Lifecycle state when the source read carried one; search hits do not. */
  state: NodeState | null;
  /** The opening snippet a search hit carries; `suggestLinks` rows have none. */
  snippet: string | null;
  /** The stored `updated_at` when the source read carried one. */
  updatedAt: string | null;
}

/** A `suggestLinks` row is already a full node. */
function candidateFromNode(node: NodeOut): TargetCandidate {
  return {
    nodeId: node.id,
    title: node.title ?? node.id,
    type: node.type,
    spaceId: node.space_id,
    state: node.state,
    snippet: null,
    updatedAt: node.updated_at,
  };
}

/** A search hit is the narrower shape: no state, no timestamps. */
function candidateFromHit(hit: SearchHit): TargetCandidate {
  return {
    nodeId: hit.node_id,
    title: hit.title ?? hit.node_id,
    type: hit.type,
    spaceId: hit.space_id,
    state: null,
    snippet: hit.snippet,
    updatedAt: null,
  };
}

/**
 * Fetch the target candidates for a query.
 *
 * Title-prefix first (`suggestLinks`), falling back to a full `search` only
 * when the prefix matched nothing — so an exact title still lands on the
 * prefix read, and a title that only matches mid-word is still reachable.
 * An empty query searches nothing.
 *
 * @param query The raw input.
 * @param suggest The title-prefix read (injected so the fallback is testable).
 * @param search The full-search read.
 * @returns The candidates, or `[]` for an empty query.
 */
export async function fetchTargetCandidates(
  query: string,
  suggest: (prefix: string) => Promise<NodeOut[]>,
  search: (q: string) => Promise<SearchResult>,
): Promise<TargetCandidate[]> {
  const trimmed = query.trim();
  if (trimmed === "") return [];
  const suggested = await suggest(trimmed);
  if (suggested.length > 0) return suggested.map(candidateFromNode);
  const result = await search(trimmed);
  return result.hits.map(candidateFromHit);
}

/**
 * Whether a candidate's edge would leave the From node's space.
 *
 * The same territory fact the graph outlines with the crossing hue: an edge
 * whose two endpoints live in different spaces. A target with no space never
 * crosses.
 *
 * @param source The dialog's From node.
 * @param candidate A target candidate.
 */
export function targetCrossing(source: NodeOut, candidate: TargetCandidate): boolean {
  return candidate.spaceId !== null && candidate.spaceId !== source.space_id;
}

/** A schedule-and-coalesce debounce over the window's own timers. */
export interface Debouncer {
  /**
   * Run `fn` once, `delayMs` after the last call to `schedule`.
   *
   * @param fn The work to run.
   */
  schedule(fn: () => void): void;
  /** Drop a pending run. Safe to call when nothing is pending. */
  cancel(): void;
}

/**
 * A minimal debounce queue for the target search.
 *
 * The search is off the typing path: each keystroke re-arms the timer, so a
 * burst of input issues one request. Exported rather than inlined so the
 * coalescing rule has a test.
 *
 * @param delayMs Quiet time before the scheduled run fires.
 */
export function createDebouncer(delayMs: number): Debouncer {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return {
    schedule(fn) {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        fn();
      }, delayMs);
    },
    cancel() {
      if (timer === null) return;
      clearTimeout(timer);
      timer = null;
    },
  };
}
