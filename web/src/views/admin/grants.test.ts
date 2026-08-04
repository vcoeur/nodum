/**
 * The admin view's grant logic (`grants.ts`).
 *
 * The semantics that matter: a re-grant is an upsert (so the picker withholds
 * spaces the agent already holds) and level order is load-bearing (the picker
 * presents weakest to strongest, matching the server's `GRANT_LEVEL_NAMES`).
 *
 * Naming a space — including the archived-space fallback a grant row depends on
 * — is covered once, in `components/spaceOptions.test.ts`, since that is where
 * the one `spaceLabel` lives.
 */

import { describe, expect, it } from "vitest";
import type { GrantLevel, GrantOut, NodeOut } from "../../api/types";
import { GRANT_LEVELS, grantableSpaces, grantsForAgent } from "./grants";

/** A grant row with only the fields these functions read. */
function grant(agentId: string, spaceId: string, level: GrantLevel = "read"): GrantOut {
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
