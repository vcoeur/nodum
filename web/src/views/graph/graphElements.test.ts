/**
 * Turning a subgraph into elements, and the one judgement the conversion makes.
 *
 * Most of `graphElements.ts` is transcription. The part worth pinning is
 * **crossing detection** (design decision D5): an edge whose endpoints live in
 * different spaces is drawn distinctly, and that marking is a claim about the
 * data. Two ways to get it wrong, both covered below:
 *
 * - marking a crossing where there is none, which draws a boundary the file
 *   does not have — the trap is a node with no `space_id`, where "unknown" is
 *   not "different";
 * - making the marking conditional on the space filter, which would leave the
 *   boundaries in a graph invisible until someone already suspected them. The
 *   element set knows nothing about the filter, and that is the point: the
 *   filter is a class toggle applied over the top.
 */

import { describe, expect, it } from "vitest";
import type { EdgeOut, NodeOut, NodeState, SubgraphOut } from "../../api/types";
import { incidentEdges, nodeLabel, toElements } from "./graphElements";
import type { GraphEdgeData, GraphNodeData } from "./graphElements";

/** A node carrying only what the element builder reads. */
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

/** An edge carrying only what the element builder reads. */
function edge(id: string, src: string, dst: string, state: NodeState = "active"): EdgeOut {
  return {
    id,
    src_id: src,
    dst_id: dst,
    type: "mentions",
    props: {},
    confidence: null,
    created_by: "human",
    state,
    valid_from: null,
    valid_to: null,
    created_at: "2026-07-26 09:00:00",
  };
}

/** One subgraph, rooted at the first node. */
function subgraph(nodes: NodeOut[], edges: EdgeOut[]): SubgraphOut {
  return { root: nodes[0]?.id ?? "", depth: 2, nodes, edges, truncated: false };
}

/** The edge elements of a built set, keyed by edge id. */
function edgeData(result: ReturnType<typeof toElements>): Map<string, GraphEdgeData> {
  const byId = new Map<string, GraphEdgeData>();
  for (const element of result.elements) {
    if (element.group === "edges") byId.set(String(element.data.id), element.data as GraphEdgeData);
  }
  return byId;
}

/** The classes attached to one element id. */
function classesOf(result: ReturnType<typeof toElements>, id: string): string {
  const element = result.elements.find((candidate) => candidate.data.id === id);
  return String(element?.classes ?? "");
}

describe("nodeLabel", () => {
  it("falls back to a short id when a node has no title", () => {
    expect(nodeLabel({ ...node("0123456789abcdef", "sp-a"), title: null })).toBe("⟨01234567⟩");
  });

  it("truncates a long title rather than letting it cover the canvas", () => {
    const label = nodeLabel({ ...node("n", "sp-a"), title: "x".repeat(200) });
    expect(label.length).toBeLessThan(50);
    expect(label.endsWith("…")).toBe(true);
  });
});

describe("toElements", () => {
  it("carries each node's space onto the element, so nothing has to re-derive it", () => {
    const result = toElements(subgraph([node("a", "sp-a")], []));
    const data = result.elements[0]!.data as GraphNodeData;
    expect(data.space).toBe("sp-a");
  });

  it("renders a missing space as an empty string rather than as null", () => {
    const result = toElements(subgraph([node("a", null)], []));
    expect((result.elements[0]!.data as GraphNodeData).space).toBe("");
  });

  it("marks an edge between two different spaces as a crossing", () => {
    const result = toElements(
      subgraph([node("a", "sp-a"), node("b", "sp-b")], [edge("e", "a", "b")]),
    );
    expect(edgeData(result).get("e")!.crossing).toBe(true);
    expect(classesOf(result, "e")).toContain("crossing");
    expect(result.crossingEdges).toBe(1);
  });

  it("does not mark an edge inside one space", () => {
    const result = toElements(
      subgraph([node("a", "sp-a"), node("b", "sp-a")], [edge("e", "a", "b")]),
    );
    expect(edgeData(result).get("e")!.crossing).toBe(false);
    expect(classesOf(result, "e")).not.toContain("crossing");
    expect(result.crossingEdges).toBe(0);
  });

  it("treats an unknown space as unknown, not as a different one", () => {
    // Both directions: a crossing claimed on a null endpoint would draw a
    // boundary the data never asserted.
    const fromNull = toElements(
      subgraph([node("a", null), node("b", "sp-b")], [edge("e", "a", "b")]),
    );
    const toNull = toElements(
      subgraph([node("a", "sp-a"), node("b", null)], [edge("e", "a", "b")]),
    );
    const bothNull = toElements(
      subgraph([node("a", null), node("b", null)], [edge("e", "a", "b")]),
    );
    expect(fromNull.crossingEdges).toBe(0);
    expect(toNull.crossingEdges).toBe(0);
    expect(bothNull.crossingEdges).toBe(0);
  });

  it("keeps the state class beside the crossing class, never instead of it", () => {
    // Colour is the state axis and the crossing is an outline, so a proposed
    // crossing has to keep reading as proposed.
    const result = toElements(
      subgraph([node("a", "sp-a"), node("b", "sp-b")], [edge("e", "a", "b", "proposed")]),
    );
    expect(classesOf(result, "e").split(" ").sort()).toEqual(["crossing", "state-proposed"]);
  });

  it("counts every crossing, not just the first", () => {
    const result = toElements(
      subgraph(
        [node("a", "sp-a"), node("b", "sp-b"), node("c", "sp-c")],
        [edge("e1", "a", "b"), edge("e2", "b", "c"), edge("e3", "a", "c")],
      ),
    );
    expect(result.crossingEdges).toBe(3);
  });

  it("puts a node's space in the signature, since it decides the crossing classes", () => {
    const before = toElements(subgraph([node("a", "sp-a"), node("b", "sp-a")], []));
    const after = toElements(subgraph([node("a", "sp-a"), node("b", "sp-b")], []));
    expect(after.signature).not.toBe(before.signature);
  });

  it("drops an edge naming an endpoint the response did not carry, and says so", () => {
    const result = toElements(subgraph([node("a", "sp-a")], [edge("e", "a", "missing")]));
    expect(result.danglingEdges).toBe(1);
    expect(edgeData(result).size).toBe(0);
    // A dropped edge is not a crossing, whatever the surviving endpoint's space.
    expect(result.crossingEdges).toBe(0);
  });
});

describe("incidentEdges", () => {
  const data = subgraph(
    [node("a", "sp-a"), node("b", "sp-a"), node("far", "sp-b")],
    [edge("e-local", "a", "b"), edge("e-out", "a", "far"), edge("e-in", "far", "a")],
  );

  it("marks the edges that leave the selected node's space", () => {
    const incident = incidentEdges(data, "a");
    const crossing = incident.filter((item) => item.crossing).map((item) => item.edge.id);
    expect(crossing.sort()).toEqual(["e-in", "e-out"]);
  });

  it("leaves an edge inside the space unmarked", () => {
    const local = incidentEdges(data, "a").find((item) => item.edge.id === "e-local");
    expect(local!.crossing).toBe(false);
  });

  it("resolves the far node in both directions", () => {
    const incident = incidentEdges(data, "a");
    expect(incident.find((item) => item.edge.id === "e-out")!.other?.id).toBe("far");
    expect(incident.find((item) => item.edge.id === "e-in")!.other?.id).toBe("far");
  });

  it("does not claim a crossing when either side's space is unknown", () => {
    const unknown = subgraph(
      [node("a", null), node("b", "sp-b")],
      [edge("e", "a", "b")],
    );
    expect(incidentEdges(unknown, "a")[0]!.crossing).toBe(false);
    expect(incidentEdges(unknown, "b")[0]!.crossing).toBe(false);
  });
});
