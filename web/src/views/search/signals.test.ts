/**
 * Reading a search hit's `signals` map.
 *
 * The whole module exists because the three numbers the server emits are **not
 * on one scale**: `bm25` and `vector` are reciprocal-rank-fusion contributions
 * (`1 / (K + rank)`, K = 60) that sum to the hit's fused score, while `graph`
 * is an edge weight (edge-type weight × confidence) attached to a neighbour
 * pulled in by expansion — an order of magnitude larger and meaning something
 * else entirely.
 *
 * So the properties under test are: `graph` never joins the share bar and never
 * gets a recovered rank, the two fusion signals do both, and a rank that does
 * not round-trip degrades to showing the raw float rather than lying about a
 * position.
 */

import { describe, expect, it } from "vitest";
import type { SearchHit } from "../../api/types";
import {
  describeSignals,
  formatSignalValue,
  readVectorEvidence,
  RRF_K,
  SIGNAL_KEYS,
  SIGNAL_LABEL,
} from "./signals";

/** The contribution `nodum.search` emits for a hit at `rank` in one list. */
const rrf = (rank: number) => 1 / (RRF_K + rank);

/** A hit carrying exactly the signals given. */
function hit(signals: Record<string, number>, node_id = "n1"): SearchHit {
  const score = (signals.bm25 ?? 0) + (signals.vector ?? 0);
  return {
    node_id,
    space_id: "main",
    type: "note",
    title: "A note",
    snippet: "…",
    score,
    signals,
  };
}

describe("describeSignals", () => {
  it("reads the two fusion signals in display order with their human labels", () => {
    const described = describeSignals(hit({ vector: rrf(2), bm25: rrf(1) }));
    // Order comes from SIGNAL_KEYS, not from the object's key order — the
    // server is free to serialise the map however it likes.
    expect(described.parts.map((part) => part.key)).toEqual(["bm25", "vector"]);
    expect(described.parts.map((part) => part.label)).toEqual([
      SIGNAL_LABEL.bm25,
      SIGNAL_LABEL.vector,
    ]);
  });

  it("recovers the rank behind each contribution", () => {
    // A rank is far more scannable than 0.0164, and it is exactly recoverable
    // because the contribution is 1 / (K + rank) for an integer rank.
    const described = describeSignals(hit({ bm25: rrf(1), vector: rrf(7) }));
    expect(described.parts.map((part) => part.rank)).toEqual([1, 7]);
  });

  it("drops the rank instead of lying when the number is not of the RRF form", () => {
    // The server owns K. If it changes shape, the honest degradation is to
    // show the float the server sent, not a position invented from it.
    const described = describeSignals(hit({ bm25: 0.0155 }));
    expect(described.parts[0]?.rank).toBeNull();
    expect(described.parts[0]?.value).toBe(0.0155);
  });

  it("refuses a rank outside the plausible range", () => {
    expect(describeSignals(hit({ bm25: rrf(10_001) })).parts[0]?.rank).toBeNull();
    expect(describeSignals(hit({ bm25: rrf(10_000) })).parts[0]?.rank).toBe(10_000);
    // A contribution at or below zero is not a rank at all.
    expect(describeSignals(hit({ bm25: 0 })).parts[0]?.rank).toBeNull();
    expect(describeSignals(hit({ bm25: -1 })).parts[0]?.rank).toBeNull();
  });

  it("sums only the fusion signals into the fused total", () => {
    const described = describeSignals(hit({ bm25: rrf(1), vector: rrf(3), graph: 0.8 }));
    expect(described.fusedTotal).toBeCloseTo(rrf(1) + rrf(3), 12);
  });

  it("gives shares that add up, so the bar is an honest picture of the fusion", () => {
    const described = describeSignals(hit({ bm25: rrf(1), vector: rrf(3) }));
    const shares = described.parts.map((part) => part.share!);
    expect(shares.reduce((total, share) => total + share, 0)).toBeCloseTo(1, 12);
    // Rank 1 beats rank 3, so keyword takes the larger share.
    expect(shares[0]!).toBeGreaterThan(shares[1]!);
  });

  it("never gives the graph signal a share, because it is not part of the fusion", () => {
    // An edge weight sharing a bar with two RRF contributions would read as
    // "this hit was 98% neighbour", which is meaningless.
    const described = describeSignals(hit({ bm25: rrf(1), graph: 0.9 }));
    const graph = described.parts.find((part) => part.key === "graph")!;
    expect(graph.share).toBeNull();
    expect(graph.rank).toBeNull();
    expect(graph.value).toBe(0.9);
  });

  it("marks a graph-only hit as a neighbour rather than a match", () => {
    const described = describeSignals(hit({ graph: 0.75 }));
    expect(described.isNeighbour).toBe(true);
    expect(described.isCorroborated).toBe(false);
    expect(described.dominant).toBe("graph");
    expect(described.fusedTotal).toBe(0);
  });

  it("is not a neighbour once any retrieval signal fired", () => {
    expect(describeSignals(hit({ bm25: rrf(1), graph: 0.75 })).isNeighbour).toBe(false);
    expect(describeSignals(hit({ vector: rrf(1), graph: 0.75 })).isNeighbour).toBe(false);
  });

  it("calls a hit corroborated only when keyword and semantic both fired", () => {
    expect(describeSignals(hit({ bm25: rrf(1), vector: rrf(9) })).isCorroborated).toBe(true);
    expect(describeSignals(hit({ bm25: rrf(1) })).isCorroborated).toBe(false);
    expect(describeSignals(hit({ vector: rrf(1) })).isCorroborated).toBe(false);
  });

  it("picks the dominant signal by raw value", () => {
    expect(describeSignals(hit({ bm25: rrf(1), vector: rrf(9) })).dominant).toBe("bm25");
    expect(describeSignals(hit({ bm25: rrf(9), vector: rrf(1) })).dominant).toBe("vector");
  });

  it("surfaces a signal this build does not know about instead of dropping it", () => {
    // A new server-side signal must be visible, not silently swallowed by a
    // client that predates it.
    const described = describeSignals(hit({ bm25: rrf(1), rerank: 0.42 }));
    expect(described.unknown).toEqual([{ key: "rerank", value: 0.42 }]);
    expect(described.parts.map((part) => part.key)).toEqual(["bm25"]);
  });

  it("ignores a non-numeric or non-finite value rather than rendering NaN", () => {
    const broken = hit({});
    broken.signals = { bm25: Number.NaN, vector: Number.POSITIVE_INFINITY } as Record<
      string,
      number
    >;
    expect(describeSignals(broken).parts).toEqual([]);
    expect(describeSignals(broken).fusedTotal).toBe(0);
  });

  it("survives a hit with no signals at all", () => {
    const bare = hit({});
    expect(describeSignals(bare).parts).toEqual([]);
    expect(describeSignals(bare).isNeighbour).toBe(false);
    expect(describeSignals(bare).unknown).toEqual([]);
    // Something has to be named as dominant; the first retrieval signal is the
    // least surprising fallback.
    expect(describeSignals(bare).dominant).toBe(SIGNAL_KEYS[0]);
  });
});

describe("readVectorEvidence", () => {
  it("reports the vector signal as contributing when any fused hit carries one", () => {
    expect(readVectorEvidence([hit({ bm25: rrf(1) }), hit({ bm25: rrf(2), vector: rrf(1) })])).toBe(
      "contributed",
    );
  });

  it("reports it absent when a non-empty fused set carries none", () => {
    // This is the only available evidence that the embedding provider is
    // unavailable or the `vec` projector has never run — there is no flag.
    expect(readVectorEvidence([hit({ bm25: rrf(1) }), hit({ bm25: rrf(2) })])).toBe("absent");
  });

  it("concludes nothing from an empty result set", () => {
    expect(readVectorEvidence([])).toBe("unknown");
  });

  it("concludes nothing from neighbours alone", () => {
    // An expansion hit never carries a retrieval signal by construction, so
    // its silence about the vector signal is not evidence of anything.
    expect(readVectorEvidence([hit({ graph: 0.8 }), hit({ graph: 0.6 })])).toBe("unknown");
  });

  it("ignores neighbours when judging a mixed set", () => {
    expect(readVectorEvidence([hit({ graph: 0.8 }), hit({ bm25: rrf(1) })])).toBe("absent");
    expect(readVectorEvidence([hit({ graph: 0.8 }), hit({ vector: rrf(1) })])).toBe("contributed");
  });
});

describe("formatSignalValue", () => {
  it("gives an RRF contribution enough places to be distinguishable", () => {
    // At two places every fusion contribution renders as "0.02".
    expect(formatSignalValue(rrf(1))).toBe("0.0164");
    expect(formatSignalValue(rrf(2))).toBe("0.0161");
    expect(formatSignalValue(rrf(1))).not.toBe(formatSignalValue(rrf(2)));
  });

  it("keeps an edge weight readable at two places", () => {
    expect(formatSignalValue(0.85)).toBe("0.85");
    expect(formatSignalValue(0.1)).toBe("0.10");
  });
});
