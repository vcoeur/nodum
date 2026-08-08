/**
 * The peek card's pure model: excerpt, edge counts, intent state, and cache.
 *
 * The hover/focus preview (`components/NodePeek.tsx`) is a transient surface —
 * it shows for a second and is gone — so the expensive parts of rendering a
 * node must not run for it. That is what this module enforces: the excerpt is
 * the node's opening prose, whitespace-collapsed plain text, **never** rendered
 * HTML, so a peek never pays for sanitisation or mermaid; the edge counts come
 * from the same `depth: 1` neighborhood read the reading view already makes;
 * and the per-session cache keeps repeated hovers of one node at one request
 * pair.
 *
 * The intent delay is a small state machine rather than a boolean because the
 * card has to re-aim: hovering from one trigger straight onto another must
 * cancel the first peek's intent and start the second's, and a leave for a
 * trigger the peek is not about must be a no-op.
 *
 * Two limits are deliberate. The cache is **session-scoped**: what a card
 * shows is the node as it was the first time it was peeked, so a node edited
 * elsewhere in this session stays stale until the page reloads. That trade-off
 * is the point — a peek is a transient glance, and refreshing per hover would
 * cost a round trip to buy a freshness nobody sees; the alternative
 * (invalidating on mutation) would couple this module to the write path. And
 * loads are **not cancellable**: the card has no AbortSignal, because the
 * fetch is shared per session and aborting one hover would poison the cache
 * for the next. A stale arrival is discarded by the caller's render guard,
 * while the cache entry itself stays good for the next show.
 */

import type { NodeOut, SubgraphOut } from "../api/types";

/** How much of a node's content the peek shows. */
export const PEEK_LIMIT = 280;

/** How long a hover must hold before the card shows. */
export const PEEK_DELAY_MS = 300;

/**
 * How long a leave is given to be cancelled by entering the card.
 *
 * The card is a portal, so moving the pointer from a trigger onto it crosses a
 * DOM boundary the trigger cannot see. Without this window the card would
 * vanish the moment the pointer left the trigger — making its actions
 * unreachable by mouse.
 */
export const PEEK_LEAVE_GRACE_MS = 150;

/**
 * The node's opening prose, whitespace-collapsed, capped for a preview.
 *
 * @param content The node's raw Markdown content.
 * @param limit Maximum excerpt length; the default is {@link PEEK_LIMIT}.
 * @returns The collapsed excerpt, or null when the content is blank.
 */
export function peekExcerpt(content: string, limit: number = PEEK_LIMIT): string | null {
  const flattened = content.replace(/\s+/g, " ").trim();
  if (!flattened) return null;
  // Cap on code points, not UTF-16 units: `slice` on the string would cut an
  // astral-plane character (an emoji, say) in half at the boundary.
  const chars = [...flattened];
  return chars.length > limit ? `${chars.slice(0, limit - 1).join("")}…` : flattened;
}

/** How many active edges point at a node and away from it. */
export interface EdgeCounts {
  /** Edges whose target is the node. */
  in: number;
  /** Edges whose source is the node. */
  out: number;
}

/**
 * Derive a root node's in/out edge counts from its depth-1 neighborhood.
 *
 * @param subgraph The `getNode(id, { depth: 1 })` read.
 * @returns The counts; both zero when the walk returned nothing.
 */
export function edgeCounts(subgraph: SubgraphOut): EdgeCounts {
  const root = subgraph.root;
  if (root === "") return { in: 0, out: 0 };
  let incoming = 0;
  let outgoing = 0;
  for (const edge of subgraph.edges) {
    // A self-loop is the root's own edge, so it counts as outgoing — the
    // same convention the reading view's rail uses.
    if (edge.src_id === root) outgoing += 1;
    else if (edge.dst_id === root) incoming += 1;
  }
  return { in: incoming, out: outgoing };
}

/* ------------------------------------------------------------------ */
/* Intent state machine                                                */
/* ------------------------------------------------------------------ */

/** What the peek is doing about its trigger. */
export type PeekPhase = "hidden" | "pending" | "shown";

/** The intent state: which phase, and which trigger it is about. */
export interface PeekState {
  phase: PeekPhase;
  /** The trigger (a node id) the peek is about, null while hidden. */
  trigger: string | null;
}

/** The resting state: no peek pending or shown. */
export const PEEK_IDLE: PeekState = { phase: "hidden", trigger: null };

/**
 * A pointer or focus entered a trigger.
 *
 * Any trigger the peek is not already shown for starts (or restarts) arming —
 * that is the re-aim: moving from one trigger straight to another points the
 * card at the new one.
 *
 * @param state The current state.
 * @param trigger The trigger entered.
 */
export function peekEnter(state: PeekState, trigger: string): PeekState {
  if (state.phase === "shown" && state.trigger === trigger) return state;
  if (state.phase === "pending" && state.trigger === trigger) return state;
  return { phase: "pending", trigger };
}

/**
 * A pointer or focus left a trigger.
 *
 * A leave for a trigger the peek is not about is a no-op — the armed timer for
 * a *different* trigger must survive a visit to a neighbour that is not one.
 *
 * @param state The current state.
 * @param trigger The trigger left.
 */
export function peekLeave(state: PeekState, trigger: string): PeekState {
  return state.trigger === trigger ? PEEK_IDLE : state;
}

/**
 * The intent timer fired for a pending peek.
 *
 * @param state The current state.
 */
export function peekConfirm(state: PeekState): PeekState {
  return state.phase === "pending" ? { phase: "shown", trigger: state.trigger } : state;
}

/**
 * A dismissal — Escape, leaving the card, unmounting.
 *
 * @param _state The current state (unused: a dismissal always hides).
 */
export function peekDismiss(_state: PeekState): PeekState {
  return PEEK_IDLE;
}

/** The events {@link peekReducer} accepts. */
export type PeekEvent =
  | { type: "enter"; trigger: string }
  | { type: "leave"; trigger: string }
  | { type: "confirm" }
  | { type: "dismiss" };

/**
 * One reducer over the intent state machine, for `useReducer`.
 *
 * @param state The current state.
 * @param event The transition to apply.
 */
export function peekReducer(state: PeekState, event: PeekEvent): PeekState {
  switch (event.type) {
    case "enter":
      return peekEnter(state, event.trigger);
    case "leave":
      return peekLeave(state, event.trigger);
    case "confirm":
      return peekConfirm(state);
    case "dismiss":
      return peekDismiss(state);
  }
}

/* ------------------------------------------------------------------ */
/* Per-session cache                                                   */
/* ------------------------------------------------------------------ */

/** Everything the peek card renders for one node. */
export interface PeekData {
  node: NodeOut;
  inCount: number;
  outCount: number;
}

/** How a cache entry is produced, on a cache miss. */
export type PeekLoader = (nodeId: string) => Promise<PeekData>;

/** A per-session cache of peek data, keyed by node id. */
export interface PeekCache {
  /**
   * The cached entry for a node, or null on a miss.
   *
   * @param nodeId The node id.
   */
  get(nodeId: string): PeekData | null;
  /**
   * Resolve a node's peek data, loading on a miss.
   *
   * Concurrent calls for one node share a single load, and a failed load is
   * not cached, so a transient refusal can be retried by the next hover.
   *
   * @param nodeId The node id.
   */
  getOrLoad(nodeId: string): Promise<PeekData>;
  /** Drop every entry. */
  clear(): void;
}

/**
 * Build a peek cache over an injected loader.
 *
 * The loader is injected rather than imported so the module stays pure — the
 * component wires it to the API client once.
 *
 * @param load The miss handler, e.g. a `getNode` pair plus {@link edgeCounts}.
 */
export function createPeekCache(load: PeekLoader): PeekCache {
  const entries = new Map<string, PeekData>();
  const inFlight = new Map<string, Promise<PeekData>>();

  return {
    get(nodeId) {
      return entries.get(nodeId) ?? null;
    },
    getOrLoad(nodeId) {
      const cached = entries.get(nodeId);
      if (cached !== undefined) return Promise.resolve(cached);
      const pending = inFlight.get(nodeId);
      if (pending !== undefined) return pending;
      const started = load(nodeId).then(
        (data) => {
          inFlight.delete(nodeId);
          entries.set(nodeId, data);
          return data;
        },
        (error: unknown) => {
          inFlight.delete(nodeId);
          throw error;
        },
      );
      inFlight.set(nodeId, started);
      return started;
    },
    clear() {
      entries.clear();
      inFlight.clear();
    },
  };
}
