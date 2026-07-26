/**
 * Naming a space the review queue reports (`spaceNaming.ts`).
 *
 * The property that matters is that the three answers stay apart: a space the
 * pickers list, a space they deliberately do not (archived — its proposals are
 * still waiting), and an id nothing resolves. Collapsing the middle one into
 * either neighbour is exactly the defect this module exists to fix — a section
 * headed by a bare 32-hex id, or one claiming a retired space is live.
 */

import { describe, expect, it } from "vitest";
import type { NodeOut } from "../../api/types";
import { nameSpace, unresolvedSpaceIds } from "./spaceNaming";

/** A space node, trimmed to what the resolver reads. */
function space(id: string, title: string | null, state = "active"): NodeOut {
  return {
    id,
    space_id: "meta",
    type: "space",
    parent_id: null,
    position: null,
    title,
    content: "",
    props: {},
    state,
    created_by: "human",
    created_at: "2026-07-26 09:00:00",
    updated_at: "2026-07-26 09:00:00",
  };
}

const ACTIVE: NodeOut[] = [space("sp-main", "main"), space("sp-research", "research")];
const ARCHIVED: NodeOut[] = [space("sp-old", "reading", "archived")];

describe("nameSpace", () => {
  it("names an active space by its title", () => {
    expect(nameSpace("sp-research", ACTIVE, ARCHIVED)).toEqual({
      label: "research",
      kind: "active",
    });
  });

  it("names an archived space and says that is what it is", () => {
    expect(nameSpace("sp-old", ACTIVE, ARCHIVED)).toEqual({
      label: "reading",
      kind: "archived",
    });
  });

  it("reports an id nothing resolves as itself rather than guessing", () => {
    expect(nameSpace("sp-gone", ACTIVE, ARCHIVED)).toEqual({
      label: "sp-gone",
      kind: "unknown",
    });
  });

  it("falls back to the id for a space with no title", () => {
    expect(nameSpace("sp-odd", [space("sp-odd", null)], [])).toEqual({
      label: "sp-odd",
      kind: "active",
    });
  });

  it("degrades to the id, not to a wrong name, before the archived read lands", () => {
    expect(nameSpace("sp-old", ACTIVE, [])).toEqual({ label: "sp-old", kind: "unknown" });
  });

  it("resolves a space named rather than identified, as every reference may be", () => {
    expect(nameSpace("research", ACTIVE, ARCHIVED).kind).toBe("active");
  });
});

describe("unresolvedSpaceIds", () => {
  it("names only the ids the active list cannot", () => {
    expect(unresolvedSpaceIds(["sp-main", "sp-old", "sp-gone"], ACTIVE)).toEqual([
      "sp-old",
      "sp-gone",
    ]);
  });

  it("skips the blank id: the unreported bucket is not a space", () => {
    expect(unresolvedSpaceIds(["", "sp-main"], ACTIVE)).toEqual([]);
  });

  it("reports each id once, however many sections carry it", () => {
    expect(unresolvedSpaceIds(["sp-old", "sp-old"], ACTIVE)).toEqual(["sp-old"]);
  });

  it("is empty on a healthy queue, which is what keeps the extra read off", () => {
    expect(unresolvedSpaceIds(["sp-main", "sp-research"], ACTIVE)).toEqual([]);
  });
});
