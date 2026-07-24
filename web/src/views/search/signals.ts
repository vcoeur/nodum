/**
 * Reading a hit's `signals` map — the part of the search view that has to be
 * right about what the server actually computed.
 *
 * `nodum/search.py` produces three kinds of number under three keys, and they
 * are *not* on one scale:
 *
 * - `bm25` and `vector` are **reciprocal-rank-fusion contributions**,
 *   `1 / (K + rank)` with `K = 60`. They are directly comparable to each other
 *   and they sum to exactly the hit's `score`, so a share-of-score bar is an
 *   honest picture of the fusion.
 * - `graph` is an **edge weight** (edge-type weight × confidence, typically
 *   0.5–1.0) attached to a neighbour pulled in by `expand`. It is an order of
 *   magnitude larger than an RRF contribution and means something completely
 *   different, so it never shares a bar with the other two — and a client-side
 *   sort by `score` would wrongly float every neighbour to the top. This is one
 *   of the reasons the view renders the server's order verbatim.
 *
 * Because a contribution is exactly `1 / (K + rank)` for an integer rank, the
 * hit's **position in each signal's own ranked list is recoverable** — which is
 * a far more scannable thing to show a human than a float. The recovery is
 * checked rather than assumed: if it does not reproduce the contribution to
 * within floating-point noise (the server's K changed, say), the rank is
 * dropped and the raw contribution is shown instead.
 */

import type { SearchHit } from "../../api/types";

/** The reciprocal-rank-fusion damping constant, mirroring `search._RRF_K`. */
export const RRF_K = 60;

/** The signal keys `nodum.search` emits. */
export type SignalKey = "bm25" | "vector" | "graph";

/** Display order, matching the order the retrieval path runs them in. */
export const SIGNAL_KEYS: readonly SignalKey[] = ["bm25", "vector", "graph"];

/** Human names — the interface never says "bm25" without saying "keyword". */
export const SIGNAL_LABEL: Record<SignalKey, string> = {
  bm25: "keyword",
  vector: "semantic",
  graph: "neighbour",
};

/** One-line explanations, used as tooltips and in the legend. */
export const SIGNAL_HELP: Record<SignalKey, string> = {
  bm25: "BM25 over the FTS index — matched the words you typed.",
  vector: "Vector ANN over chunk embeddings — matched the meaning, not the words.",
  graph: "Not a match itself: a one-hop neighbour of a match, pulled in by graph expansion.",
};

/** One signal's contribution to a hit. */
export interface SignalPart {
  key: SignalKey;
  /** Human label, e.g. "keyword". */
  label: string;
  /** The raw number from the server. */
  value: number;
  /**
   * Share of the fused score, 0–1. Null for `graph`, which is an edge weight
   * rather than a fusion contribution and has no share of anything.
   */
  share: number | null;
  /** 1-based position in this signal's ranked list, when it round-trips exactly. */
  rank: number | null;
}

/** A hit's signals, read into something renderable. */
export interface HitSignals {
  /** Known signals, in {@link SIGNAL_KEYS} order. */
  parts: SignalPart[];
  /** The largest contributor; `graph` for an expansion hit. */
  dominant: SignalKey;
  /** True when the hit carries only `graph` — a neighbour, not a match. */
  isNeighbour: boolean;
  /** True when both retrieval signals fired: keyword and semantic agree. */
  isCorroborated: boolean;
  /** Sum of the RRF contributions (`bm25` + `vector`) — the fused score. */
  fusedTotal: number;
  /** Signals this UI does not know about, surfaced rather than silently dropped. */
  unknown: { key: string; value: number }[];
}

/**
 * Recover the 1-based rank behind an RRF contribution.
 *
 * @param contribution A `signals.bm25` / `signals.vector` value.
 * @returns The rank, or null when the value is not of the form `1 / (K + rank)`.
 */
function recoverRank(contribution: number): number | null {
  if (!Number.isFinite(contribution) || contribution <= 0) return null;
  const rank = Math.round(1 / contribution - RRF_K);
  if (rank < 1 || rank > 10_000) return null;
  // Only trust the rank if it reproduces the number we were given: the server
  // owns K, and a changed constant must degrade to "show the float", not lie.
  const reproduced = 1 / (RRF_K + rank);
  return Math.abs(reproduced - contribution) <= contribution * 1e-9 ? rank : null;
}

/**
 * Read one hit's `signals` map.
 *
 * @param hit A search hit as returned by the server.
 * @returns The per-signal breakdown the row renders.
 */
export function describeSignals(hit: SearchHit): HitSignals {
  const signals = hit.signals ?? {};
  const parts: SignalPart[] = [];
  let fusedTotal = 0;

  for (const key of SIGNAL_KEYS) {
    const value = signals[key];
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    if (key !== "graph") fusedTotal += value;
    parts.push({
      key,
      label: SIGNAL_LABEL[key],
      value,
      share: null,
      rank: key === "graph" ? null : recoverRank(value),
    });
  }

  for (const part of parts) {
    if (part.key !== "graph" && fusedTotal > 0) part.share = part.value / fusedTotal;
  }

  const unknown = Object.entries(signals)
    .filter(([key, value]) => !isSignalKey(key) && typeof value === "number")
    .map(([key, value]) => ({ key, value: value as number }));

  const present = new Set(parts.map((part) => part.key));
  const isNeighbour = present.has("graph") && !present.has("bm25") && !present.has("vector");
  const dominant =
    parts.reduce<SignalPart | null>(
      (best, part) => (best === null || part.value > best.value ? part : best),
      null,
    )?.key ?? "bm25";

  return {
    parts,
    dominant,
    isNeighbour,
    isCorroborated: present.has("bm25") && present.has("vector"),
    fusedTotal,
    unknown,
  };
}

/** Narrow an arbitrary signals-map key to a known signal. */
function isSignalKey(key: string): key is SignalKey {
  return (SIGNAL_KEYS as readonly string[]).includes(key);
}

/**
 * Whether the vector signal contributed anywhere in a result set.
 *
 * The server skips the vector signal entirely when no embedding provider is
 * available (and produces nothing when the `vec` projector has never indexed a
 * chunk). There is no flag for either case, so absence across a non-empty
 * *fused* set is the only available evidence — neighbours are excluded because
 * an expansion hit never carries a retrieval signal by construction.
 *
 * @param hits The hits of one response.
 * @returns `"contributed"`, `"absent"`, or `"unknown"` when there is nothing to
 *   conclude from (an empty fused set).
 */
export function readVectorEvidence(hits: SearchHit[]): "contributed" | "absent" | "unknown" {
  const fused = hits.filter((hit) => !describeSignals(hit).isNeighbour);
  if (fused.length === 0) return "unknown";
  const contributed = fused.some((hit) => typeof hit.signals?.vector === "number");
  return contributed ? "contributed" : "absent";
}

/** Format a signal value for display: small RRF numbers need more places. */
export function formatSignalValue(value: number): string {
  return value < 0.1 ? value.toFixed(4) : value.toFixed(2);
}
