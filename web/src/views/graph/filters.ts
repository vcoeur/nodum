/**
 * The graph view's filter model, its URL codec, and the request it builds.
 *
 * Filter state lives in the URL rather than in component state, so a rendered
 * subgraph is linkable and survives a reload. The query-parameter names are
 * deliberately the ones `GET /api/graph/subgraph` itself accepts, which makes
 * the browser's query string the API request minus its root — one spelling to
 * learn, and a URL that can be pasted into `curl`.
 *
 * **`space` is the one exception, and it is a deliberate one.** The subgraph
 * endpoint takes no space parameter and must not be given one: design decision
 * D5 keeps the far endpoint of a cross-space edge rendered, dimmed and
 * clickable, and a server-side filter would delete it instead — leaving the
 * graph asserting that the connection ends there. So `space` narrows what the
 * render *emphasises*, never what the walk returns; it is carried in the URL
 * with the others so a narrowed view is still linkable, and
 * {@link toSubgraphParams} pointedly never sends it. {@link filterKey}, which
 * identifies a *request*, pointedly never includes it either.
 *
 * Only non-default values are written, so the common case stays a clean URL.
 */

import type { EdgeOut, NodeOut, NodeState, SubgraphParams } from "../../api/types";

/** Edge lifecycle states the walk can be asked to follow. */
export const EDGE_STATES: readonly NodeState[] = ["active", "proposed", "archived"];

/** Everything the subgraph request is parameterised by. */
export interface GraphFilters {
  /** Maximum hops from the root. */
  depth: number;
  /**
   * Hard node cap, enforced server-side *during* the walk.
   *
   * It bounds edges too: the server caps those at `limit * SUBGRAPH_EDGE_FACTOR`
   * and reports either cap through the one `truncated` flag.
   */
  limit: number;
  /** Edge states the walk may follow. Never empty — `active` is the floor. */
  edgeStates: NodeState[];
  /** Edge type ids the walk may follow; empty means "any". */
  edgeTypes: string[];
  /** Node type ids that may be admitted; empty means "any". The root is exempt. */
  nodeTypes: string[];
  /** Only follow edges written by this actor; empty means "any". */
  createdBy: string;
  /**
   * Floor on an edge's stored confidence, or null when the floor is off.
   *
   * Null is not the same as 0: a floor of 0 still drops every edge whose
   * confidence is unstated, because the server reads NULL as "does not meet
   * the bar". Off means the parameter is not sent at all.
   */
  minConfidence: number | null;
  /**
   * Space to narrow the reading to; `""` is off (design decision D1).
   *
   * **This one is not part of the request.** `GET /api/graph/subgraph` has no
   * `space` parameter, and it must not grow one here: design decision D5 says a
   * cross-space edge keeps its far endpoint *rendered, dimmed and clickable*,
   * and a server-side filter would drop that node — making the graph assert
   * that the connection ends there, which is false. So this is the one control
   * in the bar that shapes the render rather than the walk, and everything it
   * touches is still on screen. See {@link spaceDimming}.
   *
   * A space reference is an id or a name everywhere in nodum; the picker emits
   * ids, but a hand-written URL may carry a name, so the view resolves it
   * against the space list before comparing it to `NodeOut.space_id`.
   */
  space: string;
}

/** The filter set a bare `/graph/:rootId` renders. */
export const DEFAULT_FILTERS: GraphFilters = {
  depth: 2,
  limit: 200,
  edgeStates: ["active"],
  edgeTypes: [],
  nodeTypes: [],
  createdBy: "",
  minConfidence: null,
  space: "",
};

/** Depth bounds offered by the control. The server accepts more; this is taste. */
export const MIN_DEPTH = 0;
export const MAX_DEPTH = 8;

/**
 * Node-cap bounds. The ceiling exists so "raise the limit" cannot run away.
 *
 * `MAX_LIMIT` **mirrors `service.MAX_SUBGRAPH_LIMIT`**, which is a real
 * server-side clamp, not a slider preference: `subgraph` silently reduces any
 * larger `limit` to it. Raising this number alone therefore raises nothing —
 * the request goes out with the bigger cap and comes back clamped, so the view
 * would promise a ceiling it cannot reach. Change the server constant first,
 * then this one.
 *
 * It also sets the edge ceiling: the server caps edges at
 * `limit * SUBGRAPH_EDGE_FACTOR`, so the node cap the user picks here is what
 * bounds both.
 */
export const MIN_LIMIT = 1;
export const MAX_LIMIT = 2000;

/** Query-parameter names this codec owns; everything else in the URL is left alone. */
const FILTER_KEYS = [
  "depth",
  "limit",
  "edge_state",
  "edge_type",
  "node_type",
  "created_by",
  "min_confidence",
  "space",
] as const;

/** Parse a bounded integer, falling back when the value is absent or junk. */
function readInt(raw: string | null, fallback: number, min: number, max: number): number {
  if (raw === null) return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

/** True when two string lists hold the same values in the same order. */
function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/**
 * Read the filter set out of a URL query string.
 *
 * Unknown, malformed, or out-of-range values fall back to the default rather
 * than erroring: a hand-edited URL should still render something.
 *
 * @param params The current query string.
 * @returns The filters to render with.
 */
export function parseFilters(params: URLSearchParams): GraphFilters {
  const edgeStates = params
    .getAll("edge_state")
    .filter((state): state is NodeState => (EDGE_STATES as readonly string[]).includes(state));

  const rawConfidence = params.get("min_confidence");
  let minConfidence: number | null = null;
  if (rawConfidence !== null) {
    const value = Number.parseFloat(rawConfidence);
    if (Number.isFinite(value)) minConfidence = Math.min(1, Math.max(0, value));
  }

  return {
    depth: readInt(params.get("depth"), DEFAULT_FILTERS.depth, MIN_DEPTH, MAX_DEPTH),
    limit: readInt(params.get("limit"), DEFAULT_FILTERS.limit, MIN_LIMIT, MAX_LIMIT),
    edgeStates: edgeStates.length > 0 ? edgeStates : [...DEFAULT_FILTERS.edgeStates],
    edgeTypes: params.getAll("edge_type"),
    nodeTypes: params.getAll("node_type"),
    createdBy: params.get("created_by") ?? "",
    minConfidence,
    space: params.get("space") ?? "",
  };
}

/**
 * Write a filter set into a query string, preserving every parameter this
 * codec does not own (the path-finding selection, notably).
 *
 * @param current The query string to rewrite.
 * @param filters The filters to encode.
 * @returns A new query string; the caller decides whether it is a push or a replace.
 */
export function applyFilters(current: URLSearchParams, filters: GraphFilters): URLSearchParams {
  const next = new URLSearchParams(current);
  for (const key of FILTER_KEYS) next.delete(key);

  if (filters.depth !== DEFAULT_FILTERS.depth) next.set("depth", String(filters.depth));
  if (filters.limit !== DEFAULT_FILTERS.limit) next.set("limit", String(filters.limit));
  if (!sameList(filters.edgeStates, DEFAULT_FILTERS.edgeStates)) {
    for (const state of filters.edgeStates) next.append("edge_state", state);
  }
  for (const type of filters.edgeTypes) next.append("edge_type", type);
  for (const type of filters.nodeTypes) next.append("node_type", type);
  if (filters.createdBy) next.set("created_by", filters.createdBy);
  if (filters.minConfidence !== null) next.set("min_confidence", String(filters.minConfidence));
  if (filters.space) next.set("space", filters.space);

  return next;
}

/** True when the filters are exactly what a bare `/graph/:rootId` would use. */
export function isDefaultFilters(filters: GraphFilters): boolean {
  return (
    filters.depth === DEFAULT_FILTERS.depth &&
    filters.limit === DEFAULT_FILTERS.limit &&
    sameList(filters.edgeStates, DEFAULT_FILTERS.edgeStates) &&
    filters.edgeTypes.length === 0 &&
    filters.nodeTypes.length === 0 &&
    filters.createdBy === "" &&
    filters.minConfidence === null &&
    filters.space === ""
  );
}

/**
 * A stable string identifying one subgraph *request*.
 *
 * Used as the fetch effect's dependency, so a re-render that changes nothing
 * observable does not re-issue the request.
 *
 * `space` is deliberately absent: it never reaches the server (see the module
 * docstring), so including it would throw away a loaded graph and re-fetch an
 * identical one every time the human narrowed or widened the space — and the
 * re-fetch would take the layout with it.
 */
export function filterKey(rootId: string | undefined, filters: GraphFilters): string {
  return JSON.stringify([
    rootId ?? "",
    filters.depth,
    filters.limit,
    filters.edgeStates,
    filters.edgeTypes,
    filters.nodeTypes,
    filters.createdBy,
    filters.minConfidence,
  ]);
}

/**
 * Build the request for one root and filter set.
 *
 * `limit` is always sent, never omitted: an unbounded graph is not something
 * this view is allowed to ask for, and leaning on a server-side default would
 * make the bound a property of the server rather than of the caller.
 *
 * The edge states go out as a list rather than a single value, because
 * "active plus proposed" has to be one walk over both — the union of two
 * separate walks is a different graph.
 *
 * `filters.space` is **never sent**. The endpoint has no such parameter, and
 * D5 wants the far side of a crossing kept rather than dropped, so the space
 * filter is applied to the render by {@link spaceDimming} instead.
 *
 * @param rootId The node at the centre of the walk.
 * @param filters The active filters.
 * @returns The typed query for `api.getSubgraph`.
 */
export function toSubgraphParams(rootId: string, filters: GraphFilters): SubgraphParams {
  const query: SubgraphParams = {
    root_id: rootId,
    depth: filters.depth,
    limit: filters.limit,
    edge_state: filters.edgeStates,
  };
  if (filters.edgeTypes.length > 0) query.edge_types = filters.edgeTypes;
  if (filters.nodeTypes.length > 0) query.node_types = filters.nodeTypes;
  if (filters.createdBy) query.created_by = filters.createdBy;
  // Sent only when the floor is explicitly on: a `min_confidence` of 0 is a
  // filter, not a no-op — it drops every edge whose confidence is unstated.
  if (filters.minConfidence !== null) query.min_confidence = filters.minConfidence;
  return query;
}

/** One active, individually-clearable filter, for the summary chip row. */
export interface FilterChip {
  /** React key. */
  key: string;
  /** What the chip says. */
  label: string;
  /** Whether this one is hiding structure the user might not expect to lose. */
  tone: "neutral" | "warn";
  /** The filter set with just this constraint removed. */
  cleared: GraphFilters;
}

/**
 * Summarise the non-default filters as removable chips.
 *
 * The point is honesty: every constraint currently shaping the render is named
 * on screen, and the confidence floor is called out as the one that silently
 * removes human-authored structure.
 *
 * The space chip is first because it is the widest-reaching, and its label says
 * *dimmed* rather than *hidden* on purpose: it is the only filter here that
 * removes nothing, and a chip reading like the others would misdescribe D5's
 * whole point.
 *
 * That clause is conditional, though. A space reference the list cannot resolve
 * — an archived space still named in the URL — narrows nothing, and the banner
 * below the toolbar says so in as many words. A chip promising dimming beside a
 * banner denying it is a contradiction the human reads in one glance, and with
 * the legend's dimming line correctly suppressed in that state the chip would be
 * the only sentence on the page still making the claim.
 *
 * @param filters The active filters.
 * @param spaceName How to name the filtered space; defaults to the raw
 *   reference, which is all the caller has before the space list loads.
 * @param spaceInEffect Whether the space reference actually narrows the render.
 *   Defaults to true — the ordinary case, and the one every caller had before
 *   an unresolvable reference was reachable.
 * @returns One chip per active constraint, in a stable order.
 */
export function filterChips(
  filters: GraphFilters,
  spaceName?: string,
  spaceInEffect: boolean = true,
): FilterChip[] {
  const chips: FilterChip[] = [];

  if (filters.space) {
    const name = spaceName || filters.space;
    chips.push({
      key: "space",
      label: spaceInEffect
        ? `space: ${name} — outside is dimmed, not hidden`
        : `space: ${name} — not in effect`,
      tone: "neutral",
      cleared: { ...filters, space: "" },
    });
  }
  if (!sameList(filters.edgeStates, DEFAULT_FILTERS.edgeStates)) {
    chips.push({
      key: "edge_state",
      label: `edge state: ${filters.edgeStates.join(" + ")}`,
      tone: "neutral",
      cleared: { ...filters, edgeStates: [...DEFAULT_FILTERS.edgeStates] },
    });
  }
  for (const type of filters.edgeTypes) {
    chips.push({
      key: `edge_type:${type}`,
      label: `edge type: ${type}`,
      tone: "neutral",
      cleared: { ...filters, edgeTypes: filters.edgeTypes.filter((it) => it !== type) },
    });
  }
  for (const type of filters.nodeTypes) {
    chips.push({
      key: `node_type:${type}`,
      label: `node type: ${type}`,
      tone: "neutral",
      cleared: { ...filters, nodeTypes: filters.nodeTypes.filter((it) => it !== type) },
    });
  }
  if (filters.createdBy) {
    chips.push({
      key: "created_by",
      label: `edges by: ${filters.createdBy}`,
      tone: "neutral",
      cleared: { ...filters, createdBy: "" },
    });
  }
  if (filters.minConfidence !== null) {
    chips.push({
      key: "min_confidence",
      label: `confidence ≥ ${filters.minConfidence.toFixed(2)} — hides unrated edges`,
      tone: "warn",
      cleared: { ...filters, minConfidence: null },
    });
  }
  return chips;
}

/** What a space filter dims, and what it leaves alone (design decision D5). */
export interface SpaceDimming {
  /** True when a space filter is on. Everything below is empty when it is not. */
  active: boolean;
  /** Nodes outside the space: still drawn, still clickable, just recessive. */
  nodes: string[];
  /** Edges with **both** endpoints outside — they recede with them. */
  edges: string[];
  /** Rendered nodes inside the space. */
  inside: number;
  /** Rendered nodes outside it — the ones the filter dims. */
  outside: number;
}

/** Nothing narrowed: the whole render is at full strength. */
const NO_DIMMING: SpaceDimming = { active: false, nodes: [], edges: [], inside: 0, outside: 0 };

/**
 * Work out what a space filter recedes.
 *
 * This is the whole of D5's "the far endpoint dims, never hides", and the
 * reason the graph's space control is a render-time filter rather than a
 * request parameter. A node outside the filtered space is **kept** — drawn,
 * labelled, and selectable — because the connection into it is a fact about
 * the file, and deleting the far end would make the picture claim the edge
 * stopped at the boundary. Narrowing a read is not the same as asserting that
 * the rest of the graph is absent, which is the split model (D1) said one layer
 * down.
 *
 * A node whose `space_id` is null is treated as *not known to be inside* rather
 * than as a different space: it dims (conservative, and it stays clickable) but
 * it never counts as a crossing, since claiming a crossing on unknown data
 * would be the same false assertion in the other direction.
 *
 * An edge dims only when **both** ends are outside. An edge with one end inside
 * is precisely the crossing the human narrowed the view to notice.
 *
 * @param nodes The rendered nodes.
 * @param edges The rendered edges.
 * @param spaceId The filtered space, resolved to an id; `""` for no filter.
 * @returns What to dim, and how much of the render is inside.
 */
export function spaceDimming(
  nodes: readonly NodeOut[],
  edges: readonly EdgeOut[],
  spaceId: string,
): SpaceDimming {
  if (!spaceId) return NO_DIMMING;

  const outsideIds = new Set<string>();
  let inside = 0;
  for (const node of nodes) {
    if (node.space_id === spaceId) inside += 1;
    else outsideIds.add(node.id);
  }

  const dimmedEdges: string[] = [];
  for (const edge of edges) {
    if (outsideIds.has(edge.src_id) && outsideIds.has(edge.dst_id)) dimmedEdges.push(edge.id);
  }

  return {
    active: true,
    nodes: nodes.filter((node) => outsideIds.has(node.id)).map((node) => node.id),
    edges: dimmedEdges,
    inside,
    outside: outsideIds.size,
  };
}
