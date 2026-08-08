import { describe, expect, it } from "vitest";
import type { EdgeOut, NodeOut, SubgraphOut } from "../../api/types";
import { edgeCountLabel, incidentRows } from "./nodeEdges";

function node(id: string, overrides: Partial<NodeOut> = {}): NodeOut {
  return {
    id,
    space_id: "main",
    type: "note",
    parent_id: null,
    position: null,
    title: null,
    content: "",
    props: {},
    state: "active",
    created_by: "human:owner",
    created_at: "2026-08-07 10:00:00",
    updated_at: "2026-08-07 10:00:00",
    ...overrides,
  };
}

function edge(id: string, src: string, dst: string, overrides: Partial<EdgeOut> = {}): EdgeOut {
  return {
    id,
    src_id: src,
    dst_id: dst,
    type: "supports",
    props: {},
    confidence: null,
    created_by: "human:owner",
    state: "active",
    valid_from: null,
    valid_to: null,
    created_at: "2026-08-07 10:00:00",
    ...overrides,
  };
}

function subgraph(root: NodeOut, nodes: NodeOut[], edges: EdgeOut[]): SubgraphOut {
  return { root: root.id, depth: 1, nodes: [root, ...nodes], edges, truncated: false };
}

describe("edgeCountLabel", () => {
  it("pluralises a full count, and states a truncated one as a floor", () => {
    expect(edgeCountLabel(1, false)).toBe("1 edge");
    expect(edgeCountLabel(2, false)).toBe("2 edges");
    // A truncated read's count is the walk's cap, not the neighbourhood's
    // size — the header must not present it as fact.
    expect(edgeCountLabel(200, true)).toBe("200+ edges");
    expect(edgeCountLabel(1, true)).toBe("1+ edges");
  });
});

describe("incidentRows", () => {
  it("orients every incident edge of the root", () => {
    const root = node("root");
    const out = node("a");
    const incoming = node("b");
    const rows = incidentRows(
      subgraph(
        root,
        [out, incoming],
        [edge("e1", root.id, out.id), edge("e2", incoming.id, root.id)],
      ),
    );

    expect(rows).toHaveLength(2);
    expect(rows[0]?.direction).toBe("out");
    expect(rows[0]?.far?.id).toBe("a");
    expect(rows[1]?.direction).toBe("in");
    expect(rows[1]?.far?.id).toBe("b");
  });

  it("sorts outgoing first, then by creation order", () => {
    const root = node("root");
    const lateOut = edge("e2", root.id, "x", { created_at: "2026-08-07 12:00:00" });
    const inRow = edge("e1", "y", root.id, { created_at: "2026-08-07 11:00:00" });
    const earlyOut = edge("e0", root.id, "z", { created_at: "2026-08-07 09:00:00" });
    const rows = incidentRows(
      subgraph(
        root,
        [node("x"), node("y"), node("z")],
        [lateOut, inRow, earlyOut],
      ),
    );

    expect(rows.map((row) => row.edge.id)).toEqual(["e0", "e2", "e1"]);
  });

  it("marks a crossing when the far node lives in another space", () => {
    const root = node("root", { space_id: "main" });
    const research = node("far", { space_id: "research" });
    const rows = incidentRows(subgraph(root, [research], [edge("e1", root.id, "far")]));

    expect(rows[0]?.crossing).toBe(true);

    const same = node("same", { space_id: "main" });
    const sameRows = incidentRows(subgraph(root, [same], [edge("e2", root.id, same.id)]));
    expect(sameRows[0]?.crossing).toBe(false);
  });

  it("skips an edge the walk returned that is not incident", () => {
    const root = node("root");
    const rows = incidentRows(
      subgraph(root, [node("a"), node("b")], [edge("e1", "a", "b")]),
    );
    expect(rows).toEqual([]);
  });

  it("returns an empty list when the envelope has no root", () => {
    expect(incidentRows({ root: "x", depth: 1, nodes: [], edges: [], truncated: false })).toEqual(
      [],
    );
  });
});
