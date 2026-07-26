/**
 * The `/spaces` screen's derivations (`spaces.ts`).
 *
 * The semantics that matter here are all safety ones: a structural space is
 * recognised by id so a rename cannot smuggle one past the guard, the archive
 * copy says what archiving does *not* do, a second space cannot take a name
 * that already resolves, and no failure copy ever claims a space is missing —
 * the server's refusal is deliberately ambiguous between "gone" and "not
 * yours", and resolving that ambiguity in the interface would put the leak
 * back.
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import type { GrantOut, SpaceOut } from "../../api/types";
import { describeFailure } from "../../lib";
import {
  archiveConsequences,
  describeSpaceFailure,
  renameConsequence,
  spaceRows,
  STRUCTURAL_SPACE_IDS,
  structuralReason,
  validateSpaceName,
} from "./spaces";

/** A grant row with only the fields the screen reads. */
function grant(agentId: string, spaceId: string, level: string): GrantOut {
  return { agent_id: agentId, space_id: spaceId, level, created_at: "2026-07-26 10:00:00" };
}

/** A space as `GET /api/spaces` sends it. */
function space(
  id: string,
  title: string | null,
  nodeCount = 0,
  grants: GrantOut[] = [],
): SpaceOut {
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
    node_count: nodeCount,
    grants,
  };
}

/** The row for one space, built the way the view builds it. */
function rowFor(target: SpaceOut, spaces: readonly SpaceOut[] = [target], writeTarget = "main") {
  const row = spaceRows(spaces, writeTarget).find((candidate) => candidate.id === target.id);
  if (!row) throw new Error(`no row for ${target.id}`);
  return row;
}

describe("spaceRows", () => {
  it("keeps the server's order and carries each space's live count", () => {
    const rows = spaceRows(
      [space("main", "main", 12), space("meta", "meta", 40), space("s1", "research", 3)],
      "main",
    );
    expect(rows.map((row) => row.id)).toEqual(["main", "meta", "s1"]);
    expect(rows.map((row) => row.nodeCount)).toEqual([12, 40, 3]);
  });

  it("labels a space by its title, falling back to the id", () => {
    const rows = spaceRows([space("s1", "research"), space("s2", null)], "main");
    expect(rows.map((row) => row.label)).toEqual(["research", "s2"]);
  });

  it("lists grant holders strongest level first, then by name", () => {
    const held = space("s1", "research", 0, [
      grant("zeta", "s1", "read"),
      grant("alpha", "s1", "edit"),
      grant("beta", "s1", "suggest"),
      grant("alfred", "s1", "read"),
    ]);
    expect(rowFor(held).holders).toEqual([
      { agent: "alpha", level: "edit" },
      { agent: "beta", level: "suggest" },
      { agent: "alfred", level: "read" },
      { agent: "zeta", level: "read" },
    ]);
  });

  it("calls a space self-governing only when an agent holds edit on it", () => {
    // The point of the flag: an edit-granted space never reaches the review
    // queue, so "no proposals" there means self-rule, not silence (design D4).
    const governed = space("s1", "research", 0, [grant("scout", "s1", "edit")]);
    const suggesting = space("s2", "reading", 0, [grant("scout", "s2", "suggest")]);
    const bare = space("s3", "drafts");
    expect(rowFor(governed).selfGoverning).toBe(true);
    expect(rowFor(suggesting).selfGoverning).toBe(false);
    expect(rowFor(bare).selfGoverning).toBe(false);
  });

  it("marks the structural spaces by id, so a renamed one is still structural", () => {
    const spaces = [space("main", "journal"), space("meta", "meta"), space("s1", "main")];
    const rows = spaceRows(spaces, "main");
    // `main` renamed to "journal" is still main; a *different* space merely
    // titled "main" is not — an id is what the schema depends on.
    expect(rows.map((row) => row.structural)).toEqual([true, true, false]);
  });

  it("finds the write target when it is stored as a name rather than an id", () => {
    // The store keeps whatever the human picked, verbatim; both spellings
    // resolve server-side, so both have to resolve here.
    const spaces = [space("main", "main"), space("s1", "research")];
    expect(spaceRows(spaces, "research").map((row) => row.writeTarget)).toEqual([false, true]);
    expect(spaceRows(spaces, "s1").map((row) => row.writeTarget)).toEqual([false, true]);
  });

  it("flags no row when the write target names a space the list no longer has", () => {
    const spaces = [space("main", "main"), space("s1", "research")];
    expect(spaceRows(spaces, "retired").some((row) => row.writeTarget)).toBe(false);
  });
});

describe("structuralReason", () => {
  it("gives every structural space a reason", () => {
    for (const id of STRUCTURAL_SPACE_IDS) {
      expect(structuralReason(id)).toBeTruthy();
    }
  });

  it("says what archiving main would actually do", () => {
    expect(structuralReason("main")).toContain("every write lands when nothing names a space");
  });

  it("says meta holds the spaces themselves", () => {
    expect(structuralReason("meta")).toContain("space that spaces live in");
  });

  it("is null for an ordinary space", () => {
    expect(structuralReason("research")).toBeNull();
  });
});

describe("archiveConsequences", () => {
  const busy = space("s1", "research", 42, [
    grant("scout", "s1", "edit"),
    grant("archivist", "s1", "read"),
  ]);

  it("states that nothing is deleted, whatever the space holds", () => {
    for (const subject of [busy, space("s2", "empty", 0), space("s3", "one", 1)]) {
      const lines = archiveConsequences(rowFor(subject)).join(" ");
      expect(lines).toMatch(/deletes no/i);
      expect(lines).toContain("space_id");
    }
  });

  it("counts the live nodes and says the count includes proposals", () => {
    const lines = archiveConsequences(rowFor(busy));
    expect(lines[0]).toContain("42 live nodes");
    expect(lines[0]).toContain("active and proposed");
  });

  it("counts one node without an s, and says plainly when there are none", () => {
    expect(archiveConsequences(rowFor(space("s3", "one", 1)))[0]).toContain("1 live node —");
    expect(archiveConsequences(rowFor(space("s2", "empty", 0)))[0]).toContain("no live nodes");
  });

  it("names the agents whose grants go inert", () => {
    const lines = archiveConsequences(rowFor(busy)).join(" ");
    expect(lines).toContain("go inert");
    expect(lines).toContain("scout");
    expect(lines).toContain("archivist");
  });

  it("says a single grant in the singular, and says so when there is none", () => {
    const one = space("s4", "solo", 0, [grant("scout", "s4", "read")]);
    expect(archiveConsequences(rowFor(one)).join(" ")).toContain("The grant held by scout");
    expect(archiveConsequences(rowFor(space("s5", "none"))).join(" ")).toContain(
      "No agent holds a grant on it",
    );
  });

  it("says the archive is final", () => {
    expect(archiveConsequences(rowFor(busy)).join(" ")).toContain("no way back");
  });

  it("warns about the write target only when this space is it", () => {
    const spaces = [space("main", "main"), busy];
    const targeted = spaceRows(spaces, "research").find((row) => row.id === "s1");
    const untargeted = spaceRows(spaces, "main").find((row) => row.id === "s1");
    expect(archiveConsequences(targeted!).join(" ")).toContain("current write target");
    expect(archiveConsequences(untargeted!).join(" ")).not.toContain("write target");
  });
});

describe("renameConsequence", () => {
  it("says the old name stops resolving while the id keeps working", () => {
    const said = renameConsequence(rowFor(space("s1", "research")), "reading");
    expect(said).toContain('id ("s1")');
    expect(said).toContain('"research" no longer does');
  });

  it("does not claim the old name stopped resolving when it was the id", () => {
    // Every seeded space is in this case — `main`'s id *is* `main` — so a flat
    // "the old name no longer resolves" sentence would be false exactly there.
    const said = renameConsequence(rowFor(space("main", "main")), "journal");
    expect(said).toContain("keeps resolving");
    expect(said).not.toContain("no longer");
  });
});

describe("validateSpaceName", () => {
  const spaces = [space("main", "main"), space("s1", "research")];

  it("refuses a blank or whitespace-only name", () => {
    expect(validateSpaceName("", spaces)).toBeTruthy();
    expect(validateSpaceName("   ", spaces)).toBeTruthy();
  });

  it("accepts a name nothing else answers to", () => {
    expect(validateSpaceName("reading", spaces)).toBeNull();
    expect(validateSpaceName("  reading  ", spaces)).toBeNull();
  });

  it("refuses a name another space already carries", () => {
    // Nothing in the schema stops the collision, and after it `--space
    // research` means whichever row the query reached first.
    expect(validateSpaceName("research", spaces)).toContain("could not be told apart");
    expect(validateSpaceName("  research  ", spaces)).toBeTruthy();
  });

  it("refuses a name that is another space's id", () => {
    // A reference resolves as `id = ? OR title = ?`, so an id is just as taken.
    expect(validateSpaceName("s1", spaces)).toBeTruthy();
  });

  it("is case-sensitive, because the server's comparison is", () => {
    expect(validateSpaceName("Research", spaces)).toBeNull();
  });

  it("does not let a rename clash with the space being renamed", () => {
    expect(validateSpaceName("reading", spaces, { renaming: "s1" })).toBeNull();
  });

  it("refuses a rename to the name it already has", () => {
    expect(validateSpaceName("research", spaces, { renaming: "s1" })).toBe(
      "That is already its name.",
    );
  });

  it("still refuses a rename onto another space's name", () => {
    expect(validateSpaceName("main", spaces, { renaming: "s1" })).toBeTruthy();
  });
});

describe("describeSpaceFailure", () => {
  /** Copy that would resolve the server's deliberate ambiguity. */
  const FORBIDDEN = ["no such space", "does not exist", "no record of", "was deleted"];

  /**
   * The refusal as this screen meets it, from either wire status.
   *
   * `api/client.ts` normalises all three lifecycle routes as well as the two
   * filtered reads, so `UnknownSpaceError` is the only shape that arrives here
   * — the 400/404 split the class absorbs is what these two stand for.
   */
  const refusals = {
    fromSearchShapedStatus: new UnknownSpaceError("research", 400, "unknown space: research"),
    fromListingShapedStatus: new UnknownSpaceError("research", 404, "unknown space: research"),
  };

  it("recognises the refusal whichever status the wire carried", () => {
    for (const error of Object.values(refusals)) {
      expect(describeSpaceFailure(error, "research").title).toBe("That space did not resolve");
    }
  });

  it("never claims the space is gone", () => {
    for (const error of Object.values(refusals)) {
      const described = describeSpaceFailure(error, "research");
      const copy = `${described.title} ${described.body}`.toLowerCase();
      for (const phrase of FORBIDDEN) expect(copy).not.toContain(phrase);
    }
  });

  it("names the reference and points at the one action that helps", () => {
    const described = describeSpaceFailure(refusals.fromListingShapedStatus, "research");
    expect(described.body).toContain('"research"');
    expect(described.body).toContain("reload");
  });

  it("hands every other failure to the shared classifier unchanged", () => {
    const others: unknown[] = [
      new ApiError(503, "DatabaseBusy", "database is locked"),
      new ApiError(403, "HumanOnly", "spaces are human-only"),
      new ApiError(422, "ValidationError", "bad name"),
      new TypeError("Failed to fetch"),
      "a string nobody wrapped",
    ];
    for (const error of others) {
      expect(describeSpaceFailure(error, "research")).toEqual(describeFailure(error, "that space"));
    }
  });

  it("keeps a busy database retryable rather than turning it into a space problem", () => {
    const described = describeSpaceFailure(new ApiError(503, "DatabaseBusy", "locked"), "research");
    expect(described.kind).toBe("busy");
  });
});
