import { afterEach, describe, expect, it, vi } from "vitest";
import type { EdgeTypeOut, NodeOut, SearchHit, SearchResult } from "../api/types";
import {
  createDebouncer,
  edgeBody,
  fetchTargetCandidates,
  inverseEdgeType,
  parseConfidence,
  pickEdgeType,
  preferredEdgeType,
  targetCrossing,
} from "./linkDialog";
import type { TargetCandidate } from "./linkDialog";

function node(id: string, overrides: Partial<NodeOut> = {}): NodeOut {
  return {
    id,
    space_id: "main",
    type: "note",
    parent_id: null,
    position: null,
    title: `title-${id}`,
    content: "",
    props: {},
    state: "active",
    created_by: "human:owner",
    created_at: "2026-08-07 10:00:00",
    updated_at: "2026-08-07 10:00:00",
    ...overrides,
  };
}

/** A representative subset of the seed inverse pairs (8 of the 17 rows in
 * `nodum/migrations.py`), in the shape `/api/types` returns them. */
const EDGE_TYPES: EdgeTypeOut[] = [
  { id: "relates_to", name: "relates_to", inverse_name: "relates_to", json_schema: {}, is_builtin: true },
  { id: "supports", name: "supports", inverse_name: "supported_by", json_schema: {}, is_builtin: true },
  { id: "supported_by", name: "supported_by", inverse_name: "supports", json_schema: {}, is_builtin: true },
  { id: "contradicts", name: "contradicts", inverse_name: "contradicted_by", json_schema: {}, is_builtin: true },
  { id: "contradicted_by", name: "contradicted_by", inverse_name: "contradicts", json_schema: {}, is_builtin: true },
  { id: "derived_from", name: "derived_from", inverse_name: "derived", json_schema: {}, is_builtin: true },
  { id: "derived", name: "derived", inverse_name: "derived_from", json_schema: {}, is_builtin: true },
  { id: "duplicate_of", name: "duplicate_of", inverse_name: "duplicate_of", json_schema: {}, is_builtin: true },
];

function hit(id: string, overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    node_id: id,
    space_id: "main",
    type: "note",
    title: `title-${id}`,
    snippet: "…snippet…",
    score: 1,
    signals: { bm25: 1 },
    ...overrides,
  };
}

describe("inverseEdgeType", () => {
  it("swaps a pair for its inverse", () => {
    expect(inverseEdgeType(EDGE_TYPES, "supports")).toBe("supported_by");
    expect(inverseEdgeType(EDGE_TYPES, "supported_by")).toBe("supports");
  });

  it("is its own inverse for a symmetric relation", () => {
    expect(inverseEdgeType(EDGE_TYPES, "relates_to")).toBe("relates_to");
    expect(inverseEdgeType(EDGE_TYPES, "duplicate_of")).toBe("duplicate_of");
  });

  it("returns the type itself when the catalog has no row for it", () => {
    expect(inverseEdgeType(EDGE_TYPES, "made_up")).toBe("made_up");
  });

  it("refuses to flip a directed type that declares no inverse", () => {
    // A user-created directed type: the catalog row exists, `inverse_name`
    // is null, and the type is not genuinely self-inverse (which the catalog
    // spells as inverse_name === its own id, as `relates_to` does above).
    const directed: EdgeTypeOut = {
      id: "influences",
      name: "influences",
      inverse_name: null,
      json_schema: {},
      is_builtin: false,
    };
    expect(inverseEdgeType([...EDGE_TYPES, directed], "influences")).toBeNull();
  });
});

describe("preferredEdgeType", () => {
  it("prefers relates_to when the catalog offers it", () => {
    expect(preferredEdgeType(EDGE_TYPES)).toBe("relates_to");
  });

  it("falls back to the first catalog entry", () => {
    const without = EDGE_TYPES.filter((entry) => entry.id !== "relates_to");
    expect(preferredEdgeType(without)).toBe("contradicted_by");
  });

  it("returns null for an empty catalog", () => {
    expect(preferredEdgeType([])).toBeNull();
  });
});

describe("pickEdgeType", () => {
  // A user-created directed type: the catalog row exists with `inverse_name`
  // null, so it is direction-locked (the same row `inverseEdgeType` refuses
  // to flip).
  const directed: EdgeTypeOut = {
    id: "influences",
    name: "influences",
    inverse_name: null,
    json_schema: {},
    is_builtin: false,
  };
  const WITH_DIRECTED = [...EDGE_TYPES, directed];

  it("keeps the current direction when the picked type has an inverse", () => {
    expect(pickEdgeType(EDGE_TYPES, "in", "supports")).toEqual({
      edgeType: "supports",
      direction: "in",
    });
    expect(pickEdgeType(EDGE_TYPES, "out", "supports")).toEqual({
      edgeType: "supports",
      direction: "out",
    });
  });

  it("resets an incoming direction to outgoing when the picked type is direction-locked", () => {
    // Picking a directed type while flipped to incoming would otherwise
    // strand the dialog: both toggle buttons disabled, and a submit that
    // swaps the endpoints under the directed label.
    expect(pickEdgeType(WITH_DIRECTED, "in", "influences")).toEqual({
      edgeType: "influences",
      direction: "out",
    });
  });

  it("leaves an outgoing direction alone for a direction-locked type", () => {
    expect(pickEdgeType(WITH_DIRECTED, "out", "influences")).toEqual({
      edgeType: "influences",
      direction: "out",
    });
  });

  it("keeps the direction when the catalog has no row for the picked type", () => {
    expect(pickEdgeType(EDGE_TYPES, "in", "made_up")).toEqual({
      edgeType: "made_up",
      direction: "in",
    });
  });
});

describe("edgeBody", () => {
  const base = { sourceId: "from", targetId: "to", edgeType: "supports", confidence: null };

  it("points out of the From node when outgoing", () => {
    expect(edgeBody({ ...base, direction: "out" })).toEqual({
      src_id: "from",
      dst_id: "to",
      type: "supports",
      confidence: null,
    });
  });

  it("swaps the endpoints and keeps the type when incoming", () => {
    expect(edgeBody({ ...base, direction: "in", edgeType: "supported_by" })).toEqual({
      src_id: "to",
      dst_id: "from",
      type: "supported_by",
      confidence: null,
    });
  });

  it("carries the parsed confidence when one was set", () => {
    const body = edgeBody({ ...base, direction: "out", confidence: 0.8 });
    expect(body).toEqual({ src_id: "from", dst_id: "to", type: "supports", confidence: 0.8 });
  });
});

describe("parseConfidence", () => {
  it("is unset by default", () => {
    expect(parseConfidence("")).toEqual({ ok: true, value: null });
    expect(parseConfidence("   ")).toEqual({ ok: true, value: null });
  });

  it("parses a number in range", () => {
    expect(parseConfidence("0.8")).toEqual({ ok: true, value: 0.8 });
    expect(parseConfidence(" 0.5 ")).toEqual({ ok: true, value: 0.5 });
    expect(parseConfidence("0")).toEqual({ ok: true, value: 0 });
    expect(parseConfidence("1")).toEqual({ ok: true, value: 1 });
  });

  it("refuses a non-number", () => {
    expect(parseConfidence("high").ok).toBe(false);
    expect(parseConfidence("0.8.1").ok).toBe(false);
  });

  it("refuses a value outside the server's own range", () => {
    expect(parseConfidence("1.5")).toEqual({
      ok: false,
      reason: "Confidence must be between 0 and 1.",
    });
    expect(parseConfidence("-0.1").ok).toBe(false);
  });
});

describe("fetchTargetCandidates", () => {
  it("returns nothing for an empty query", async () => {
    const suggest = vi.fn();
    const search = vi.fn();
    await expect(fetchTargetCandidates("  ", suggest, search)).resolves.toEqual([]);
    expect(suggest).not.toHaveBeenCalled();
    expect(search).not.toHaveBeenCalled();
  });

  it("uses the prefix read when it matches, with no fallback", async () => {
    const suggest = vi.fn(async () => [node("a"), node("b")]);
    const search = vi.fn(async () => ({ query: "t", k: 8, hits: [] }));
    const candidates = await fetchTargetCandidates("web auth", suggest, search);

    expect(suggest).toHaveBeenCalledWith("web auth");
    expect(search).not.toHaveBeenCalled();
    expect(candidates.map((candidate) => candidate.nodeId)).toEqual(["a", "b"]);
    // A suggest row is a full node: state and timestamp survive, snippet is null.
    expect(candidates[0]).toMatchObject({
      nodeId: "a",
      title: "title-a",
      state: "active",
      updatedAt: "2026-08-07 10:00:00",
      snippet: null,
    });
  });

  it("falls back to full search when the prefix matches nothing", async () => {
    const suggest = vi.fn(async () => []);
    const search = vi.fn(async () => ({ query: "auth token", k: 8, hits: [hit("h1")] }));
    const candidates = await fetchTargetCandidates("auth token", suggest, search);

    expect(search).toHaveBeenCalledWith("auth token");
    expect(candidates).toHaveLength(1);
    // A search hit is the narrower shape: snippet survives, state does not.
    expect(candidates[0]).toMatchObject({
      nodeId: "h1",
      title: "title-h1",
      snippet: "…snippet…",
      state: null,
      updatedAt: null,
    });
  });

  it("returns an empty list when both reads find nothing", async () => {
    const suggest = vi.fn(async () => []);
    const search = vi.fn(async () => ({ query: "x", k: 8, hits: [] }));
    await expect(fetchTargetCandidates("x", suggest, search)).resolves.toEqual([]);
  });

  it("propagates a failure from either read", async () => {
    const suggest = vi.fn(async () => {
      throw new Error("boom");
    });
    await expect(fetchTargetCandidates("x", suggest, vi.fn())).rejects.toThrow("boom");
  });

  it("trims the query before either read", async () => {
    const suggest = vi.fn(async () => [node("a")]);
    const search = vi.fn(async (): Promise<SearchResult> => ({ query: "x", k: 8, hits: [] }));
    await fetchTargetCandidates("  web auth  ", suggest, search);
    expect(suggest).toHaveBeenCalledWith("web auth");
  });
});

describe("targetCrossing", () => {
  const source = node("from", { space_id: "main" });

  it("marks a candidate in another space", () => {
    expect(targetCrossing(source, candidate(node("a", { space_id: "research" })))).toBe(true);
  });

  it("does not mark one in the same space, or one without a space", () => {
    expect(targetCrossing(source, candidate(node("b", { space_id: "main" })))).toBe(false);
    expect(targetCrossing(source, candidate(node("c", { space_id: null })))).toBe(false);
  });
});

describe("createDebouncer", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("coalesces a burst of schedules into one run", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const debouncer = createDebouncer(250);

    debouncer.schedule(run);
    vi.advanceTimersByTime(100);
    debouncer.schedule(run);
    vi.advanceTimersByTime(100);
    debouncer.schedule(run);
    expect(run).not.toHaveBeenCalled();

    vi.advanceTimersByTime(250);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("runs once quiet time has passed since the last schedule", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const debouncer = createDebouncer(250);

    debouncer.schedule(run);
    vi.advanceTimersByTime(250);
    expect(run).toHaveBeenCalledTimes(1);

    // A later schedule starts a new window.
    debouncer.schedule(run);
    vi.advanceTimersByTime(249);
    expect(run).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("cancel drops the pending run and is safe when nothing is pending", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const debouncer = createDebouncer(250);

    debouncer.schedule(run);
    debouncer.cancel();
    vi.advanceTimersByTime(1000);
    expect(run).not.toHaveBeenCalled();

    expect(() => debouncer.cancel()).not.toThrow();
  });
});

/** A fully typed candidate row for the crossing tests. */
function candidate(row: NodeOut): TargetCandidate {
  return {
    nodeId: row.id,
    title: row.title ?? row.id,
    type: row.type,
    spaceId: row.space_id,
    state: row.state,
    snippet: null,
    updatedAt: row.updated_at,
  };
}
