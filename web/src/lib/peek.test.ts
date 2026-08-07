/**
 * The peek card's pure model: excerpt capping, edge-count derivation, the
 * intent state machine, and the per-session cache. Nothing here touches a DOM
 * or a network — the cache's loader is injected, which is what makes this
 * suite honest in the node environment.
 */

import { describe, expect, it } from "vitest";
import type { EdgeOut, NodeOut, SubgraphOut } from "../api/types";
import {
  createPeekCache,
  edgeCounts,
  peekConfirm,
  peekDismiss,
  peekEnter,
  peekExcerpt,
  peekLeave,
  peekReducer,
  PEEK_IDLE,
  PEEK_LIMIT,
  type PeekData,
  type PeekLoader,
} from "./peek";

function node(overrides: Partial<NodeOut> = {}): NodeOut {
  return {
    id: "n1",
    space_id: "s1",
    type: "note",
    parent_id: null,
    position: null,
    title: "A note",
    content: "",
    props: {},
    state: "active",
    created_by: "human",
    created_at: "2026-08-01 10:00:00",
    updated_at: "2026-08-02 11:00:00",
    ...overrides,
  };
}

function edge(overrides: Partial<EdgeOut> = {}): EdgeOut {
  return {
    id: "e1",
    src_id: "n1",
    dst_id: "n2",
    type: "mentions",
    props: {},
    confidence: null,
    created_by: "human",
    state: "active",
    valid_from: null,
    valid_to: null,
    created_at: "2026-08-01 10:00:00",
    ...overrides,
  };
}

function subgraph(nodes: NodeOut[], edges: EdgeOut[]): SubgraphOut {
  return { root: nodes[0]?.id ?? "", depth: 1, nodes, edges, truncated: false };
}

/* ------------------------------------------------------------------ */
/* peekExcerpt                                                         */
/* ------------------------------------------------------------------ */

describe("peekExcerpt", () => {
  it("collapses every whitespace run to a single space and trims", () => {
    const content = "  First line.\n\n\tSecond line.   \nThird  line.  ";
    expect(peekExcerpt(content)).toBe("First line. Second line. Third line.");
  });

  it("returns null for blank content", () => {
    expect(peekExcerpt("")).toBeNull();
    expect(peekExcerpt("   \n\t  ")).toBeNull();
  });

  it("leaves content at or under the limit alone", () => {
    const under = "a".repeat(PEEK_LIMIT - 1);
    expect(peekExcerpt(under)).toBe(under);
    const exact = "a".repeat(PEEK_LIMIT);
    expect(peekExcerpt(exact)).toBe(exact);
  });

  it("caps over-limit content to the limit, with a trailing ellipsis", () => {
    const long = "a".repeat(PEEK_LIMIT + 40);
    const excerpt = peekExcerpt(long);
    expect(excerpt).not.toBeNull();
    expect(excerpt).toHaveLength(PEEK_LIMIT);
    expect(excerpt?.endsWith("…")).toBe(true);
    // The capped text is the opening prose, not a random slice.
    expect(excerpt?.slice(0, 8)).toBe("aaaaaaaa");
  });

  it("honours an explicit limit", () => {
    const long = "a".repeat(50);
    const excerpt = peekExcerpt(long, 10);
    expect(excerpt).toHaveLength(10);
    expect(excerpt?.endsWith("…")).toBe(true);
  });
});

/* ------------------------------------------------------------------ */
/* edgeCounts                                                          */
/* ------------------------------------------------------------------ */

describe("edgeCounts", () => {
  it("counts outgoing and incoming edges of the root separately", () => {
    const graph = subgraph(
      [node(), node({ id: "n2" }), node({ id: "n3" })],
      [
        edge({ id: "e1", src_id: "n1", dst_id: "n2" }),
        edge({ id: "e2", src_id: "n1", dst_id: "n3" }),
        edge({ id: "e3", src_id: "n2", dst_id: "n1" }),
      ],
    );
    expect(edgeCounts(graph)).toEqual({ in: 1, out: 2 });
  });

  it("ignores edges not incident to the root", () => {
    const graph = subgraph(
      [node(), node({ id: "n2" }), node({ id: "n3" })],
      [edge({ id: "e1", src_id: "n2", dst_id: "n3" })],
    );
    expect(edgeCounts(graph)).toEqual({ in: 0, out: 0 });
  });

  it("returns zeros for an empty walk", () => {
    expect(edgeCounts(subgraph([], []))).toEqual({ in: 0, out: 0 });
  });

  it("handles a root with only incoming edges", () => {
    const graph = subgraph(
      [node(), node({ id: "n2" })],
      [edge({ id: "e1", src_id: "n2", dst_id: "n1" })],
    );
    expect(edgeCounts(graph)).toEqual({ in: 1, out: 0 });
  });
});

/* ------------------------------------------------------------------ */
/* Intent state machine                                                */
/* ------------------------------------------------------------------ */

describe("peek state machine", () => {
  it("arms on enter from hidden", () => {
    expect(peekEnter(PEEK_IDLE, "n1")).toEqual({ phase: "pending", trigger: "n1" });
  });

  it("re-aims from pending to a new trigger", () => {
    const pending = peekEnter(PEEK_IDLE, "n1");
    expect(peekEnter(pending, "n2")).toEqual({ phase: "pending", trigger: "n2" });
  });

  it("stays shown when the shown trigger is entered again", () => {
    const shown = peekConfirm(peekEnter(PEEK_IDLE, "n1"));
    expect(peekEnter(shown, "n1")).toBe(shown);
  });

  it("re-arms when a different trigger is entered while shown", () => {
    const shown = peekConfirm(peekEnter(PEEK_IDLE, "n1"));
    expect(peekEnter(shown, "n2")).toEqual({ phase: "pending", trigger: "n2" });
  });

  it("is idempotent for a repeat enter while pending", () => {
    const pending = peekEnter(PEEK_IDLE, "n1");
    expect(peekEnter(pending, "n1")).toBe(pending);
  });

  it("hides on leave of the current trigger, from pending or shown", () => {
    expect(peekLeave(peekEnter(PEEK_IDLE, "n1"), "n1")).toBe(PEEK_IDLE);
    const shown = peekConfirm(peekEnter(PEEK_IDLE, "n1"));
    expect(peekLeave(shown, "n1")).toBe(PEEK_IDLE);
  });

  it("ignores a leave for a trigger the peek is not about", () => {
    const pending = peekEnter(PEEK_IDLE, "n1");
    expect(peekLeave(pending, "n2")).toBe(pending);
  });

  it("confirms only from pending", () => {
    expect(peekConfirm(peekEnter(PEEK_IDLE, "n1"))).toEqual({ phase: "shown", trigger: "n1" });
    expect(peekConfirm(PEEK_IDLE)).toBe(PEEK_IDLE);
    const shown = peekConfirm(peekEnter(PEEK_IDLE, "n1"));
    expect(peekConfirm(shown)).toBe(shown);
  });

  it("dismisses from any phase", () => {
    expect(peekDismiss(PEEK_IDLE)).toBe(PEEK_IDLE);
    expect(peekDismiss(peekEnter(PEEK_IDLE, "n1"))).toBe(PEEK_IDLE);
    expect(peekDismiss(peekConfirm(peekEnter(PEEK_IDLE, "n1")))).toBe(PEEK_IDLE);
  });

  it("routes every event through the reducer", () => {
    expect(peekReducer(PEEK_IDLE, { type: "enter", trigger: "n1" })).toEqual({
      phase: "pending",
      trigger: "n1",
    });
    expect(peekReducer(peekEnter(PEEK_IDLE, "n1"), { type: "confirm" })).toEqual({
      phase: "shown",
      trigger: "n1",
    });
    expect(peekReducer(peekEnter(PEEK_IDLE, "n1"), { type: "leave", trigger: "n1" })).toBe(
      PEEK_IDLE,
    );
    expect(peekReducer(peekEnter(PEEK_IDLE, "n1"), { type: "dismiss" })).toBe(PEEK_IDLE);
  });
});

/* ------------------------------------------------------------------ */
/* Per-session cache                                                   */
/* ------------------------------------------------------------------ */

describe("createPeekCache", () => {
  function harness(loader?: PeekLoader) {
    const calls: string[] = [];
    const loaded = loader ?? (async (nodeId: string) => {
      calls.push(nodeId);
      return { node: node({ id: nodeId }), inCount: 1, outCount: 2 } satisfies PeekData;
    });
    return { cache: createPeekCache(loaded), calls };
  }

  it("returns null before a node has been loaded", () => {
    const { cache } = harness();
    expect(cache.get("n1")).toBeNull();
  });

  it("loads once and serves the cached entry afterwards", async () => {
    const { cache, calls } = harness();
    const first = await cache.getOrLoad("n1");
    const second = await cache.getOrLoad("n1");
    expect(second).toBe(first);
    expect(calls).toEqual(["n1"]);
    expect(cache.get("n1")).toBe(first);
  });

  it("shares one load between concurrent callers", async () => {
    let resolveFirst: (data: PeekData) => void = () => {};
    let calls = 0;
    const cache = createPeekCache((_nodeId) => {
      calls += 1;
      return new Promise<PeekData>((resolve) => {
        resolveFirst = resolve;
      });
    });
    const pending = [cache.getOrLoad("n1"), cache.getOrLoad("n1"), cache.getOrLoad("n1")];
    const data = { node: node(), inCount: 0, outCount: 0 };
    resolveFirst(data);
    await expect(Promise.all(pending)).resolves.toEqual([data, data, data]);
    expect(calls).toBe(1);
  });

  it("caches distinct ids separately", async () => {
    const { cache, calls } = harness();
    await cache.getOrLoad("n1");
    await cache.getOrLoad("n2");
    expect(calls).toEqual(["n1", "n2"]);
  });

  it("does not cache a failed load, so the next call retries", async () => {
    let calls = 0;
    const cache = createPeekCache(async (nodeId) => {
      calls += 1;
      if (calls === 1) throw new Error("boom");
      return { node: node({ id: nodeId }), inCount: 0, outCount: 0 };
    });
    await expect(cache.getOrLoad("n1")).rejects.toThrow("boom");
    expect(cache.get("n1")).toBeNull();
    await expect(cache.getOrLoad("n1")).resolves.toMatchObject({ inCount: 0 });
    expect(calls).toBe(2);
  });

  it("clears every entry and in-flight promise", async () => {
    const { cache } = harness();
    await cache.getOrLoad("n1");
    cache.clear();
    expect(cache.get("n1")).toBeNull();
    // A new load after clear produces a fresh entry.
    await expect(cache.getOrLoad("n1")).resolves.toMatchObject({ inCount: 1 });
  });
});
