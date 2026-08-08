import { describe, expect, it } from "vitest";
import { archiveConsequences, archiveRefusal } from "./nodeArchive";
import type { NodeOut } from "../api/types";

function node(over: Partial<NodeOut> = {}): NodeOut {
  return {
    id: "n1",
    type: "note",
    title: "Kafka partitions",
    content: "",
    props: {},
    parent_id: null,
    position: null,
    space_id: "main",
    state: "active",
    created_by: "human:alice",
    created_at: "2026-08-01 10:00:00",
    updated_at: "2026-08-01 10:00:00",
    ...over,
  } as NodeOut;
}

describe("archiveRefusal", () => {
  it("allows an active node", () => {
    expect(archiveRefusal(node())).toBeNull();
  });

  it("refuses a structural space by id", () => {
    // `_transition_row` refuses `main` and `meta` whatever route reaches them.
    expect(archiveRefusal(node({ id: "main" }))).toContain("structural space");
    expect(archiveRefusal(node({ id: "meta" }))).toContain("structural space");
  });

  it("refuses a node already archived", () => {
    expect(archiveRefusal(node({ state: "archived" }))).toBe("Already archived.");
  });

  it("points a proposed node at the review queue", () => {
    // `archive` is `active → archived` only; rejecting is what retires a
    // proposal, and the refusal has to say where that lives.
    expect(archiveRefusal(node({ state: "proposed" }))).toContain("review queue");
  });
});

describe("archiveConsequences", () => {
  it("names the node and says nothing is deleted", () => {
    const lines = archiveConsequences(node(), 3);
    expect(lines[0]).toContain("Kafka partitions");
    expect(lines[0]).toContain("Nothing is deleted");
  });

  it("falls back to a neutral subject for an untitled node", () => {
    const lines = archiveConsequences(node({ title: null }), 0);
    expect(lines[0]?.startsWith("This node")).toBe(true);
  });

  it("treats a whitespace-only title as no title", () => {
    const lines = archiveConsequences(node({ title: "   " }), 0);
    expect(lines[0]?.startsWith("This node")).toBe(true);
  });

  it("states that edges survive, and counts them when known", () => {
    // The load-bearing line: archiving settles no edges, so a reader who
    // assumes the neighbourhood goes quiet is wrong in a way the graph shows.
    expect(archiveConsequences(node(), 3).some((line) => line.includes("3 edges are not archived"))).toBe(
      true,
    );
    expect(archiveConsequences(node(), 1).some((line) => line.includes("1 edge is not archived"))).toBe(
      true,
    );
  });

  it("says nothing connects to it rather than counting zero edges", () => {
    expect(archiveConsequences(node(), 0).some((line) => line.includes("Nothing connects to it"))).toBe(
      true,
    );
  });

  it("still states that edges survive when the count is unknown", () => {
    const lines = archiveConsequences(node(), null);
    expect(lines.some((line) => line.includes("not archived with it"))).toBe(true);
  });

  it("promises the undo the dialog actually offers", () => {
    expect(archiveConsequences(node(), 2).some((line) => line.includes("reversible"))).toBe(true);
  });

  it("never claims search still reaches it", () => {
    const lines = archiveConsequences(node(), 2);
    expect(lines.some((line) => line.includes("Search stops finding it"))).toBe(true);
  });
});
