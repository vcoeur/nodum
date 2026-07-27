/**
 * Naming a space (`spaceNaming.ts`).
 *
 * Two properties are under test, and both were once defects on a real screen.
 *
 * **The four answers stay apart.** A space the pickers list, a space they
 * deliberately do not (archived — its content is still there), an id nothing
 * resolves, and a list that has not answered yet. Collapsing any of them into a
 * neighbour is the defect this module exists to fix: a bare 32-hex id at a
 * reader, a claim that a retired space is live, or a screen announcing "nothing
 * names this space" while the request that would name it is still in flight.
 *
 * **A loading list means nothing is unresolved.** `unresolvedSpaceIds` drives
 * the lazy archived read on five surfaces; answering with every id on screen
 * while `GET /api/spaces` is in flight fires that read on a healthy file, on
 * every mount, keyed on whichever fetch happened to come back first.
 */

import { describe, expect, it } from "vitest";
import type { NodeOut } from "../api/types";
import {
  findSpace,
  nameSpace,
  spaceNameNote,
  unresolvedSpaceIds,
  writeTargetWouldNotResolve,
} from "./spaceNaming";

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

  it("says pending, not unknown, while the active list has not answered", () => {
    // The distinction is the whole of it: `unknown` licenses a screen to say
    // "no list names this space", which is false against a request in flight.
    expect(nameSpace("sp-research", null, [])).toEqual({
      label: "sp-research",
      kind: "pending",
    });
  });
});

describe("spaceNameNote", () => {
  it("has nothing to add about a live space", () => {
    expect(spaceNameNote({ label: "research", kind: "active" })).toBeNull();
  });

  it("says an archived space is retired rather than emptied", () => {
    const note = spaceNameNote({ label: "reading", kind: "archived" }) ?? "";
    expect(note).toMatch(/still readable/);
    expect(note).toMatch(/archiving is not deletion/i);
  });

  it("never claims a space does not exist, for any answer", () => {
    // The server answers a space that was never created and one the caller
    // holds no grant on with identical words on purpose (Q13 review S3). Copy
    // that resolved the ambiguity would turn any of these into an existence
    // oracle over the whole file.
    const forbidden = /no such space|does not exist|nonexistent|missing space|not found|no record/i;
    for (const kind of ["active", "archived", "unknown", "pending"] as const) {
      expect(spaceNameNote({ label: "sp-x", kind }) ?? "").not.toMatch(forbidden);
    }
  });
});

describe("writeTargetWouldNotResolve", () => {
  /** Every wording nothing user-facing may use about a space. */
  const FORBIDDEN =
    /no such space|does not exist|nonexistent|missing space|not found|no record|unknown space/i;

  it("says the specific true thing about an archived target, and names it", () => {
    const sentence = writeTargetWouldNotResolve(nameSpace("sp-old", ACTIVE, ARCHIVED));

    expect(sentence).toBe(
      "The write target reading has been archived — an archived space stops resolving, so " +
        "nothing new can be filed there.",
    );
    // Not the disjunction: the archived listing answered, so guessing between
    // archived and renamed would be vaguer than what is known.
    expect(sentence).not.toMatch(/renamed/i);
  });

  it("keeps the disjunction when neither list names the target", () => {
    const sentence = writeTargetWouldNotResolve(nameSpace("ghost", ACTIVE, ARCHIVED));

    expect(sentence).toBe(
      "The write target ghost would not resolve — a space stops resolving once it is archived, " +
        "and a renamed space no longer answers to its old name.",
    );
  });

  it("keeps the disjunction for a target the active list still lists", () => {
    // Archived from another session between the list read and the write: the
    // list says `active` and the server said no, so the disjunction is right.
    expect(writeTargetWouldNotResolve(nameSpace("sp-main", ACTIVE, ARCHIVED))).toContain(
      "The write target main would not resolve",
    );
  });

  it("explains the bare reference while the space list is still in flight", () => {
    // `pending` is not `unknown`, and this is where the two stop being
    // interchangeable on screen: a list that has not answered cannot name the
    // target, so the sentence says why rather than leaving a 32-hex id alone.
    const pending = writeTargetWouldNotResolve(nameSpace("sp-old", null, ARCHIVED));
    const unknown = writeTargetWouldNotResolve(nameSpace("sp-old", ACTIVE, []));

    expect(pending).toContain("the space list has not answered yet");
    expect(pending).toContain("its id rather than its name");
    expect(unknown).not.toContain("has not answered");
    expect(pending).not.toBe(unknown);
  });

  it("never claims a space does not exist, for any answer", () => {
    for (const kind of ["active", "archived", "unknown", "pending"] as const) {
      expect(writeTargetWouldNotResolve({ label: "sp-x", kind })).not.toMatch(FORBIDDEN);
    }
  });
});

describe("unresolvedSpaceIds", () => {
  it("names only the ids the active list cannot", () => {
    expect(unresolvedSpaceIds(["sp-main", "sp-old", "sp-gone"], ACTIVE)).toEqual([
      "sp-old",
      "sp-gone",
    ]);
  });

  it("skips the blank id: a row that reported no space is not a space", () => {
    expect(unresolvedSpaceIds(["", "sp-main"], ACTIVE)).toEqual([]);
  });

  it("reports each id once, however many rows carry it", () => {
    expect(unresolvedSpaceIds(["sp-old", "sp-old"], ACTIVE)).toEqual(["sp-old"]);
  });

  it("is empty on a healthy screen, which is what keeps the extra read off", () => {
    expect(unresolvedSpaceIds(["sp-main", "sp-research"], ACTIVE)).toEqual([]);
  });

  it("is empty while the active list is still loading, not everything on screen", () => {
    // With `null` read as `[]`, this returns every id and the lazy read fires
    // the instant any surface mounts — on a file with no archived space at all.
    expect(unresolvedSpaceIds(["sp-main", "sp-research"], null)).toEqual([]);
  });
});

describe("findSpace", () => {
  it("is the one id-or-title match every space lookup shares", () => {
    expect(findSpace(ACTIVE, "sp-main")?.title).toBe("main");
    expect(findSpace(ACTIVE, "main")?.id).toBe("sp-main");
    expect(findSpace(ACTIVE, "sp-old")).toBeUndefined();
  });
});
