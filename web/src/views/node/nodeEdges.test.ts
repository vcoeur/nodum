import { describe, expect, it } from "vitest";
import type { EdgeOut, NodeOut, SubgraphOut } from "../../api/types";
import { backlinks, edgeCountLabel, incidentRows, mentionContext } from "./nodeEdges";

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

describe("mentionContext", () => {
  it("returns the prose around the link, with the [[…]] left in", () => {
    const content = "Kafka keeps order per partition. See [[Partitions]] for the detail.";
    const snippet = mentionContext(content, ["Partitions"]);
    expect(snippet).toContain("[[Partitions]]");
    expect(snippet).toContain("Kafka keeps order per partition");
  });

  it("collapses whitespace so a snippet across lines reads as one sentence", () => {
    const content = "First line.\n\n  See\n[[Target]]\n  here.";
    expect(mentionContext(content, ["Target"])).toBe("First line. See [[Target]] here.");
  });

  it("elides both ends when the window is inside longer content", () => {
    const content = `${"a ".repeat(200)}[[Target]]${" b".repeat(200)}`;
    const snippet = mentionContext(content, ["Target"]);
    expect(snippet?.startsWith("…")).toBe(true);
    expect(snippet?.endsWith("…")).toBe(true);
  });

  it("does not elide content that fits whole", () => {
    const snippet = mentionContext("See [[Target]].", ["Target"]);
    expect(snippet).toBe("See [[Target]].");
  });

  it("matches the id form as well as the title", () => {
    // `_resolve_wikilink` checks an exact id before any title, so `[[<id>]]`
    // makes the same `mentions` edge and has to find the same snippet.
    const id = "0123456789abcdef0123456789abcdef";
    expect(mentionContext(`Written as [[${id}]] here.`, [id, "Target"])).toContain(`[[${id}]]`);
  });

  it("prefers the earliest mention when both forms appear", () => {
    const id = "0123456789abcdef0123456789abcdef";
    const content = `Title first [[Target]] then id [[${id}]].`;
    // The window is centred on the first link, so the snippet is about it.
    expect(mentionContext(content, [id, "Target"])?.indexOf("[[Target]]")).toBeLessThan(
      mentionContext(content, [id, "Target"])?.indexOf(`[[${id}]]`) ?? -1,
    );
  });

  it("returns null when no wikilink names a target", () => {
    // An edge whose link was edited away but which nobody archived: the
    // backlink is still real, the snippet is simply not there to show.
    expect(mentionContext("No links at all.", ["Target"])).toBeNull();
  });

  it("ignores an empty target rather than matching [[]]", () => {
    expect(mentionContext("An empty [[]] link.", [""])).toBeNull();
  });

  it("never cuts an astral-plane character in half", () => {
    // A window boundary landing inside a surrogate pair would render as a
    // replacement glyph. Every code point in the snippet must be intact.
    const content = `${"🌍".repeat(100)}[[Target]]${"🌍".repeat(100)}`;
    const snippet = mentionContext(content, ["Target"]) ?? "";
    expect(snippet).not.toContain("�");
    expect([...snippet].every((char) => char.codePointAt(0) !== undefined)).toBe(true);
    for (const char of snippet) {
      const code = char.charCodeAt(0);
      // A lone surrogate is one UTF-16 unit long; a whole pair is two.
      const lone = code >= 0xd800 && code <= 0xdfff && char.length === 1;
      expect(lone).toBe(false);
    }
  });
});

describe("backlinks", () => {
  const root = node("root", { title: "Target" });

  it("lists inbound mentions with the sentence they link from", () => {
    const source = node("a", {
      title: "Essay",
      content: "The ordering guarantee comes from [[Target]] directly.",
    });
    const found = backlinks(
      subgraph(root, [source], [edge("e1", source.id, root.id, { type: "mentions" })]),
    );

    expect(found).toHaveLength(1);
    expect(found[0]?.from.id).toBe("a");
    expect(found[0]?.context).toContain("[[Target]]");
  });

  it("ignores outbound mentions and inbound edges of other types", () => {
    const outward = node("out", { content: "[[Elsewhere]]" });
    const supporter = node("sup", { content: "[[Target]]" });
    const found = backlinks(
      subgraph(
        root,
        [outward, supporter],
        [
          edge("e1", root.id, outward.id, { type: "mentions" }),
          edge("e2", supporter.id, root.id, { type: "supports" }),
        ],
      ),
    );
    expect(found).toEqual([]);
  });

  it("keeps a backlink whose link is no longer in the content", () => {
    // The edge is the fact; the snippet is the courtesy. Dropping the row
    // would hide a live edge because its prose changed.
    const source = node("a", { content: "The link was edited out." });
    const found = backlinks(
      subgraph(root, [source], [edge("e1", source.id, root.id, { type: "mentions" })]),
    );
    expect(found).toHaveLength(1);
    expect(found[0]?.context).toBeNull();
  });

  it("marks a mention from another space as crossing", () => {
    const source = node("a", { space_id: "research", content: "[[Target]]" });
    const found = backlinks(
      subgraph(root, [source], [edge("e1", source.id, root.id, { type: "mentions" })]),
    );
    expect(found[0]?.crossing).toBe(true);
  });

  it("matches an untitled root by id alone", () => {
    const untitled = node("0123456789abcdef0123456789abcdef");
    const source = node("a", { content: `See [[${untitled.id}]].` });
    const found = backlinks(
      subgraph(untitled, [source], [edge("e1", source.id, untitled.id, { type: "mentions" })]),
    );
    expect(found[0]?.context).toContain(untitled.id);
  });

  it("returns an empty list when the envelope has no root", () => {
    expect(backlinks({ root: "x", depth: 1, nodes: [], edges: [], truncated: false })).toEqual([]);
  });
});
