/**
 * The graph filter codec: what a URL means, and what it does not say.
 *
 * Two properties carry the design and are what these tests are really about:
 *
 * - **Only non-default values are written**, so a URL stays clean and
 *   `isDefaultFilters` and `applyFilters` cannot drift apart — a round trip
 *   through a query string must be the identity on the filter set.
 * - **`minConfidence: null` is not `0`.** Null means the parameter is not sent
 *   at all; `0` is a live filter that drops every edge whose confidence is
 *   unstated, which is most of the human-authored graph. Collapsing the two is
 *   a silent data-hiding bug, so it is pinned in the parser, the encoder, the
 *   request builder, and the chip row.
 *
 * The space filter (design decision D5) adds a third, and it is the odd one
 * out: it is in the URL like the others but it is **not part of the request**,
 * and it **removes nothing**. Both halves are pinned below, because both are
 * ways the feature could be quietly turned into something else — sending it
 * would make the server drop the far endpoint of a crossing, and treating it
 * like the other filters would hide the far endpoint client-side. Either would
 * leave the graph claiming a connection ends at a boundary it does not.
 */

import { describe, expect, it } from "vitest";
import type { EdgeOut, NodeOut } from "../../api/types";
import {
  applyFilters,
  DEFAULT_FILTERS,
  filterChips,
  filterKey,
  isDefaultFilters,
  MAX_DEPTH,
  MAX_LIMIT,
  MIN_LIMIT,
  parseFilters,
  spaceDimming,
  toSubgraphParams,
} from "./filters";
import type { GraphFilters } from "./filters";

/** Parse a bare query string, the way the view reads `location.search`. */
const parse = (search: string) => parseFilters(new URLSearchParams(search));

/** Encode onto an empty query string and read it back as text. */
const encode = (filters: GraphFilters) => applyFilters(new URLSearchParams(), filters).toString();

/** A node carrying only the fields the space filter reads. */
function node(id: string, spaceId: string | null): NodeOut {
  return {
    id,
    space_id: spaceId,
    type: "note",
    parent_id: null,
    position: null,
    title: id,
    content: "",
    props: {},
    state: "active",
    created_by: "human",
    created_at: "2026-07-26 09:00:00",
    updated_at: "2026-07-26 09:00:00",
  };
}

/** An edge carrying only the fields the space filter reads. */
function edge(id: string, src: string, dst: string): EdgeOut {
  return {
    id,
    src_id: src,
    dst_id: dst,
    type: "mentions",
    props: {},
    confidence: null,
    created_by: "human",
    state: "active",
    valid_from: null,
    valid_to: null,
    created_at: "2026-07-26 09:00:00",
  };
}

describe("parseFilters", () => {
  it("gives a bare URL the documented defaults", () => {
    expect(parse("")).toEqual(DEFAULT_FILTERS);
    expect(isDefaultFilters(parse(""))).toBe(true);
  });

  it("reads every parameter under the name the API itself accepts", () => {
    // The query string is deliberately the API request minus its root, so a
    // renamed key here would break the paste-into-curl property.
    const filters = parse(
      "depth=3&limit=50&edge_state=active&edge_state=proposed" +
        "&edge_type=mentions&edge_type=cites&node_type=note&created_by=agent%3Aresearcher" +
        "&min_confidence=0.7",
    );
    expect(filters).toEqual({
      depth: 3,
      limit: 50,
      edgeStates: ["active", "proposed"],
      edgeTypes: ["mentions", "cites"],
      nodeTypes: ["note"],
      createdBy: "agent:researcher",
      minConfidence: 0.7,
      // Not in the query string above, and deliberately: `space` is the one
      // key this codec owns that the API does not accept. It has its own tests.
      space: "",
    });
  });

  it("clamps numbers into range instead of erroring on a hand-edited URL", () => {
    expect(parse("depth=999").depth).toBe(MAX_DEPTH);
    expect(parse("depth=-4").depth).toBe(0);
    expect(parse("limit=99999").limit).toBe(MAX_LIMIT);
    expect(parse("limit=0").limit).toBe(MIN_LIMIT);
    expect(parse("min_confidence=5").minConfidence).toBe(1);
    expect(parse("min_confidence=-1").minConfidence).toBe(0);
  });

  it("falls back rather than rendering nothing when a value is junk", () => {
    expect(parse("depth=abc").depth).toBe(DEFAULT_FILTERS.depth);
    expect(parse("limit=").limit).toBe(DEFAULT_FILTERS.limit);
    expect(parse("min_confidence=abc").minConfidence).toBeNull();
  });

  it("drops an unknown edge state rather than asking the server to walk it", () => {
    expect(parse("edge_state=active&edge_state=nonsense").edgeStates).toEqual(["active"]);
    // Nothing valid left means the floor, not an empty walk.
    expect(parse("edge_state=nonsense").edgeStates).toEqual(["active"]);
  });

  it("distinguishes an absent confidence floor from a floor of zero", () => {
    // A floor of 0 still excludes every edge whose confidence is NULL, which
    // is most human-created structure. It is a filter, not a no-op.
    expect(parse("").minConfidence).toBeNull();
    expect(parse("min_confidence=0").minConfidence).toBe(0);
  });

  it("reads a space filter, by id or by name", () => {
    // A space reference is an id *or* a name on every nodum surface, so the
    // codec keeps whatever the URL said and leaves resolving it to the view.
    expect(parse("space=sp-research").space).toBe("sp-research");
    expect(parse("space=research").space).toBe("research");
    expect(parse("").space).toBe("");
  });
});

describe("applyFilters", () => {
  it("writes nothing at all for the default set", () => {
    expect(encode(DEFAULT_FILTERS)).toBe("");
  });

  it("omits each value that matches its default", () => {
    expect(encode({ ...DEFAULT_FILTERS, depth: 4 })).toBe("depth=4");
    expect(encode({ ...DEFAULT_FILTERS, limit: 25 })).toBe("limit=25");
    // `["active"]` is the default edge-state list, so it stays out of the URL.
    expect(encode({ ...DEFAULT_FILTERS, edgeStates: ["active"] })).toBe("");
  });

  it("writes a confidence floor of zero, because it is not the default", () => {
    expect(encode({ ...DEFAULT_FILTERS, minConfidence: 0 })).toBe("min_confidence=0");
  });

  it("repeats a key per value rather than joining them", () => {
    // The server reads repeated query keys; a comma-joined list would arrive
    // as one nonsense edge type.
    expect(encode({ ...DEFAULT_FILTERS, edgeTypes: ["mentions", "cites"] })).toBe(
      "edge_type=mentions&edge_type=cites",
    );
  });

  it("preserves parameters it does not own", () => {
    // The path-finding selection lives in the same query string and must
    // survive a filter change.
    const next = applyFilters(new URLSearchParams("target=abc123&depth=7"), {
      ...DEFAULT_FILTERS,
      depth: 3,
    });
    expect(next.get("target")).toBe("abc123");
    expect(next.get("depth")).toBe("3");
  });

  it("clears a parameter it owns when the value returns to its default", () => {
    const next = applyFilters(new URLSearchParams("depth=7&min_confidence=0.5"), DEFAULT_FILTERS);
    expect(next.toString()).toBe("");
  });

  it("writes the space filter, so a narrowed view is linkable like any other", () => {
    expect(encode({ ...DEFAULT_FILTERS, space: "sp-research" })).toBe("space=sp-research");
    expect(applyFilters(new URLSearchParams("space=sp-a"), DEFAULT_FILTERS).toString()).toBe("");
  });
});

describe("the codec round-trips", () => {
  const cases: [string, GraphFilters][] = [
    ["defaults", DEFAULT_FILTERS],
    ["depth and limit", { ...DEFAULT_FILTERS, depth: 5, limit: 33 }],
    ["two edge states", { ...DEFAULT_FILTERS, edgeStates: ["active", "proposed"] }],
    ["only proposed", { ...DEFAULT_FILTERS, edgeStates: ["proposed"] }],
    ["type lists", { ...DEFAULT_FILTERS, edgeTypes: ["mentions"], nodeTypes: ["note", "claim"] }],
    ["an author", { ...DEFAULT_FILTERS, createdBy: "agent:researcher" }],
    ["a zero floor", { ...DEFAULT_FILTERS, minConfidence: 0 }],
    ["a real floor", { ...DEFAULT_FILTERS, minConfidence: 0.85 }],
    ["a space, by id", { ...DEFAULT_FILTERS, space: "sp-research" }],
    ["a space, by name", { ...DEFAULT_FILTERS, space: "research" }],
    ["a space beside a real filter", { ...DEFAULT_FILTERS, space: "sp-a", depth: 4 }],
  ];

  for (const [name, filters] of cases) {
    it(`survives encode → parse: ${name}`, () => {
      expect(parse(encode(filters))).toEqual(filters);
    });
  }
});

describe("isDefaultFilters", () => {
  it("is false for every single non-default value", () => {
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, depth: 3 })).toBe(false);
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, limit: 3 })).toBe(false);
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, edgeStates: ["proposed"] })).toBe(false);
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, edgeTypes: ["mentions"] })).toBe(false);
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, nodeTypes: ["note"] })).toBe(false);
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, createdBy: "human" })).toBe(false);
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, minConfidence: 0 })).toBe(false);
    expect(isDefaultFilters({ ...DEFAULT_FILTERS, space: "sp-a" })).toBe(false);
  });

  it("agrees with applyFilters about what is worth writing down", () => {
    // If these two ever disagree the URL says one thing and the "filters
    // active" indicator says another.
    const sets: GraphFilters[] = [
      DEFAULT_FILTERS,
      { ...DEFAULT_FILTERS, depth: 1 },
      { ...DEFAULT_FILTERS, minConfidence: 0 },
      { ...DEFAULT_FILTERS, edgeStates: ["active", "archived"] },
      { ...DEFAULT_FILTERS, space: "sp-a" },
    ];
    for (const filters of sets) {
      expect(encode(filters) === "").toBe(isDefaultFilters(filters));
    }
  });
});

describe("toSubgraphParams", () => {
  it("always sends a limit, so no caller can ask for an unbounded graph", () => {
    // Leaning on a server-side default would make the bound a property of the
    // server rather than of this view.
    expect(toSubgraphParams("root", DEFAULT_FILTERS).limit).toBe(DEFAULT_FILTERS.limit);
  });

  it("sends the edge states as one list, because two walks are a different graph", () => {
    const query = toSubgraphParams("root", {
      ...DEFAULT_FILTERS,
      edgeStates: ["active", "proposed"],
    });
    expect(query.edge_state).toEqual(["active", "proposed"]);
  });

  it("omits every empty optional filter rather than sending an empty list", () => {
    const query = toSubgraphParams("root", DEFAULT_FILTERS);
    expect(query).toEqual({ root_id: "root", depth: 2, limit: 200, edge_state: ["active"] });
    expect("edge_types" in query).toBe(false);
    expect("node_types" in query).toBe(false);
    expect("created_by" in query).toBe(false);
    expect("min_confidence" in query).toBe(false);
  });

  it("sends a floor of zero but never sends a null one", () => {
    expect(toSubgraphParams("root", { ...DEFAULT_FILTERS, minConfidence: 0 }).min_confidence).toBe(0);
    expect("min_confidence" in toSubgraphParams("root", DEFAULT_FILTERS)).toBe(false);
  });

  it("never sends the space filter, whatever it is set to", () => {
    // The endpoint has no `space` parameter, and it must not be given one: a
    // server-side space filter would delete the far endpoint of a cross-space
    // edge, which is precisely what D5 says the graph may not do. This is the
    // assertion that fails if someone "completes" the request builder.
    const query = toSubgraphParams("root", { ...DEFAULT_FILTERS, space: "sp-research" });
    expect(query).toEqual({ root_id: "root", depth: 2, limit: 200, edge_state: ["active"] });
    expect(JSON.stringify(query)).not.toContain("sp-research");
  });
});

describe("filterKey", () => {
  it("is stable across re-renders that change nothing observable", () => {
    // It is the fetch effect's dependency: an unstable key re-issues the
    // subgraph request on every render.
    expect(filterKey("root", { ...DEFAULT_FILTERS })).toBe(filterKey("root", { ...DEFAULT_FILTERS }));
  });

  it("changes when any filter or the root changes", () => {
    const base = filterKey("root", DEFAULT_FILTERS);
    expect(filterKey("other", DEFAULT_FILTERS)).not.toBe(base);
    expect(filterKey("root", { ...DEFAULT_FILTERS, depth: 3 })).not.toBe(base);
    expect(filterKey("root", { ...DEFAULT_FILTERS, minConfidence: 0 })).not.toBe(base);
    expect(filterKey(undefined, DEFAULT_FILTERS)).not.toBe(base);
  });

  it("does not change when the space filter changes, because the request does not", () => {
    // It identifies a *request*. The space filter never reaches the server, so
    // including it would throw away a loaded graph — and its layout — to fetch
    // an identical one every time the human narrowed or widened.
    const base = filterKey("root", DEFAULT_FILTERS);
    expect(filterKey("root", { ...DEFAULT_FILTERS, space: "sp-research" })).toBe(base);
    expect(filterKey("root", { ...DEFAULT_FILTERS, space: "sp-other" })).toBe(base);
  });
});

describe("filterChips", () => {
  it("names nothing when nothing is filtered", () => {
    expect(filterChips(DEFAULT_FILTERS)).toEqual([]);
  });

  it("names every active constraint, so the render is never silently shaped", () => {
    const chips = filterChips({
      ...DEFAULT_FILTERS,
      edgeStates: ["active", "proposed"],
      edgeTypes: ["mentions"],
      nodeTypes: ["note", "claim"],
      createdBy: "agent:researcher",
      minConfidence: 0.6,
    });
    expect(chips.map((chip) => chip.key)).toEqual([
      "edge_state",
      "edge_type:mentions",
      "node_type:note",
      "node_type:claim",
      "created_by",
      "min_confidence",
    ]);
  });

  it("warns on the confidence floor alone, because it hides human structure", () => {
    const chips = filterChips({
      ...DEFAULT_FILTERS,
      edgeTypes: ["mentions"],
      minConfidence: 0.6,
    });
    const warned = chips.filter((chip) => chip.tone === "warn");
    expect(warned).toHaveLength(1);
    expect(warned[0]?.key).toBe("min_confidence");
    expect(warned[0]?.label).toContain("hides unrated edges");
  });

  it("warns about a floor of zero too", () => {
    // The reading that matters is "a floor is set", not "the floor is high".
    expect(filterChips({ ...DEFAULT_FILTERS, minConfidence: 0 })).toHaveLength(1);
  });

  it("clears exactly one constraint per chip and leaves the rest standing", () => {
    const active: GraphFilters = {
      ...DEFAULT_FILTERS,
      edgeTypes: ["mentions", "cites"],
      minConfidence: 0.6,
    };
    const chips = filterChips(active);
    const clearedMentions = chips.find((chip) => chip.key === "edge_type:mentions")!.cleared;
    expect(clearedMentions.edgeTypes).toEqual(["cites"]);
    expect(clearedMentions.minConfidence).toBe(0.6);

    const clearedFloor = chips.find((chip) => chip.key === "min_confidence")!.cleared;
    expect(clearedFloor.minConfidence).toBeNull();
    expect(clearedFloor.edgeTypes).toEqual(["mentions", "cites"]);
  });

  it("clears back to exactly the default when the last constraint goes", () => {
    const chips = filterChips({ ...DEFAULT_FILTERS, minConfidence: 0.6 });
    expect(isDefaultFilters(chips[0]!.cleared)).toBe(true);
  });

  it("names the space filter first, since it is the widest-reaching", () => {
    const chips = filterChips({
      ...DEFAULT_FILTERS,
      space: "sp-research",
      edgeTypes: ["mentions"],
    });
    expect(chips.map((chip) => chip.key)).toEqual(["space", "edge_type:mentions"]);
  });

  it("says the space filter dims rather than hides, because that is what it does", () => {
    // Every other chip describes something being removed. This one must not
    // read like them: nothing outside the space is gone from the render, and a
    // human who believed otherwise would misread the whole picture.
    const [chip] = filterChips({ ...DEFAULT_FILTERS, space: "sp-research" }, "research");
    expect(chip!.label).toContain("research");
    expect(chip!.label).toContain("dimmed, not hidden");
    // Neutral, not `warn`: `warn` is reserved for a filter that removes
    // structure, and this one removes none.
    expect(chip!.tone).toBe("neutral");
  });

  it("drops the dimming claim when the reference narrows nothing", () => {
    // An archived space still named in the URL resolves to nothing, so the view
    // renders the whole neighbourhood and the banner under the toolbar says so.
    // The chip sits roughly one line above that banner: promising "outside is
    // dimmed" there contradicts it in a single glance, and with the legend's
    // dimming line correctly suppressed in this state the chip would be the last
    // thing on the page still making the claim.
    const [chip] = filterChips({ ...DEFAULT_FILTERS, space: "sp-retired" }, "retired", false);
    expect(chip!.label).toContain("retired");
    expect(chip!.label).not.toContain("dimmed");
    expect(chip!.label).toContain("not in effect");
  });

  it("falls back to the raw reference when the space list has not named it yet", () => {
    const [chip] = filterChips({ ...DEFAULT_FILTERS, space: "sp-research" });
    expect(chip!.label).toContain("sp-research");
  });

  it("clears the space filter alone, leaving the real filters standing", () => {
    const chips = filterChips({
      ...DEFAULT_FILTERS,
      space: "sp-a",
      minConfidence: 0.6,
    });
    const cleared = chips.find((chip) => chip.key === "space")!.cleared;
    expect(cleared.space).toBe("");
    expect(cleared.minConfidence).toBe(0.6);
  });
});

describe("spaceDimming", () => {
  const nodes = [
    node("in-1", "sp-a"),
    node("in-2", "sp-a"),
    node("out-1", "sp-b"),
    node("unknown", null),
  ];
  const edges = [
    edge("e-inside", "in-1", "in-2"),
    edge("e-crossing", "in-1", "out-1"),
    edge("e-outside", "out-1", "unknown"),
  ];

  it("does nothing at all when no space is named", () => {
    expect(spaceDimming(nodes, edges, "")).toEqual({
      active: false,
      nodes: [],
      edges: [],
      inside: 0,
      outside: 0,
    });
  });

  it("dims what is outside the space and leaves what is inside alone", () => {
    const dimming = spaceDimming(nodes, edges, "sp-a");
    expect(dimming.active).toBe(true);
    expect(dimming.nodes).toEqual(["out-1", "unknown"]);
    expect(dimming.inside).toBe(2);
    expect(dimming.outside).toBe(2);
  });

  it("removes nothing — every node is still accounted for", () => {
    // This is the whole of D5 as an equation: the render keeps every node the
    // walk returned, and the filter only decides which of them recede.
    const dimming = spaceDimming(nodes, edges, "sp-a");
    expect(dimming.inside + dimming.outside).toBe(nodes.length);
  });

  it("dims a node whose space is unknown rather than assuming it is inside", () => {
    // Null is "not known to be here", and the conservative reading keeps it
    // visible and clickable either way.
    expect(spaceDimming([node("x", null)], [], "sp-a").nodes).toEqual(["x"]);
  });

  it("never dims a crossing edge — it is what the filter exists to reveal", () => {
    const dimming = spaceDimming(nodes, edges, "sp-a");
    expect(dimming.edges).not.toContain("e-crossing");
    expect(dimming.edges).not.toContain("e-inside");
  });

  it("dims an edge only when both of its endpoints are outside", () => {
    const dimming = spaceDimming(nodes, edges, "sp-a");
    expect(dimming.edges).toEqual(["e-outside"]);
  });

  it("reports every node as outside when the space is not in this neighbourhood", () => {
    // A legitimate state, and the view says so in words. What it must never do
    // is come back empty, which would look like the graph itself had gone.
    const dimming = spaceDimming(nodes, edges, "sp-elsewhere");
    expect(dimming.inside).toBe(0);
    expect(dimming.nodes).toHaveLength(nodes.length);
  });

  it("keeps the node ids in render order, so the class toggle key is stable", () => {
    const dimming = spaceDimming(nodes, edges, "sp-a");
    expect(dimming.nodes).toEqual(spaceDimming(nodes, edges, "sp-a").nodes);
  });
});
