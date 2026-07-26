/**
 * The admin view's grant logic (`grants.ts`).
 *
 * The semantics that matter: a re-grant is an upsert (so the picker withholds
 * spaces the agent already holds), level order is load-bearing (the picker
 * presents weakest to strongest, matching the server's `GRANT_LEVEL_NAMES`),
 * and a grant row must render even when its space can no longer be listed.
 */

import { describe, expect, it } from "vitest";
import type { GrantOut, NodeOut } from "../../api/types";
import { GRANT_LEVELS, grantableSpaces, grantsForAgent, spaceLabel } from "./grants";

/** A grant row with only the fields these functions read. */
function grant(agentId: string, spaceId: string, level = "read"): GrantOut {
  return { agent_id: agentId, space_id: spaceId, level, created_at: "2026-07-26 10:00:00" };
}

/** A space node with only the fields these functions read. */
function space(id: string, title: string | null): NodeOut {
  return {
    id,
    space_id: "meta",
    type: "space",
    parent_id: null,
    position: null,
    title,
    content: "",
    props: {},
    state: "active",
    created_by: "human:owner",
    created_at: "2026-07-26 10:00:00",
    updated_at: "2026-07-26 10:00:00",
  };
}

describe("GRANT_LEVELS", () => {
  it("runs weakest to strongest, matching the server's vocabulary", () => {
    expect(GRANT_LEVELS).toEqual(["read", "suggest", "edit"]);
  });
});

describe("grantsForAgent", () => {
  it("keeps only the agent's own rows", () => {
    const grants = [grant("a", "main"), grant("b", "meta"), grant("a", "meta")];
    expect(grantsForAgent(grants, "a").map((row) => row.space_id)).toEqual(["main", "meta"].sort());
    expect(grantsForAgent(grants, "nobody")).toEqual([]);
  });

  it("orders by space id regardless of the server's row order", () => {
    const grants = [grant("a", "zeta"), grant("a", "alpha"), grant("a", "main")];
    expect(grantsForAgent(grants, "a").map((row) => row.space_id)).toEqual([
      "alpha",
      "main",
      "zeta",
    ]);
  });
});

describe("grantableSpaces", () => {
  const spaces = [space("main", "Main"), space("meta", "Meta"), space("research", "Research")];

  it("withholds spaces the agent already holds — a re-grant would re-level, not add", () => {
    const grants = [grant("a", "meta")];
    expect(grantableSpaces(spaces, grants, "a").map((row) => row.id)).toEqual([
      "main",
      "research",
    ]);
  });

  it("is empty once the agent holds every space", () => {
    const grants = [grant("a", "main"), grant("a", "meta"), grant("a", "research")];
    expect(grantableSpaces(spaces, grants, "a")).toEqual([]);
  });

  it("is not fooled by another agent's grants", () => {
    const grants = [grant("b", "main"), grant("b", "meta"), grant("b", "research")];
    expect(grantableSpaces(spaces, grants, "a")).toHaveLength(3);
  });
});

describe("spaceLabel", () => {
  it("prefers the space's title", () => {
    expect(spaceLabel([space("main", "Main")], "main")).toBe("Main");
  });

  it("falls back to the id for an untitled space", () => {
    expect(spaceLabel([space("main", null)], "main")).toBe("main");
  });

  it("falls back to the id for a space the list no longer carries", () => {
    // An archived space drops out of /api/spaces; its grant rows must still render.
    expect(spaceLabel([], "main")).toBe("main");
  });
});
