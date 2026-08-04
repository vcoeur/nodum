/**
 * When a result row names its space, and what it calls it (`resultSpace.ts`).
 *
 * Two properties, both of which have been broken on the real screen.
 *
 * **The rule**: a row states the space when the filter left it unknown, and
 * stays silent when the filter already determined it. Dropping the first makes
 * search unscannable across spaces; dropping the second prints the filter back
 * on every row.
 *
 * **The name**: a hit in a space archived since it was written must read as
 * that space, not as 32 hex characters. The first version of this module ended
 * in `spaceLabel`, whose fallback is the raw reference, and a browser pass
 * found the result it produced — `Restored space check · in
 * 4affabf6d856427886ad48570f5f6e20 · note · active`. That is precisely the
 * sentence the phase's exit criterion exists to prevent.
 */

import { describe, expect, it } from "vitest";
import type { NodeOut, NodeState, SearchHit } from "../../api/types";
import { ANY_SPACE } from "../../components";
import { hitSpaceName, hitSpaceTitle } from "./resultSpace";

/** A space node, trimmed to what the name lookup reads. */
function space(id: string, title: string, state: NodeState = "active"): NodeOut {
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

/** A hit carrying nothing but the field under test. */
function hit(spaceId: string | null): SearchHit {
  return {
    node_id: "n1",
    space_id: spaceId,
    type: "note",
    title: "A note",
    snippet: "…",
    score: 0.1,
    signals: { bm25: 0.1 },
  };
}

const SPACES: NodeOut[] = [space("sp-main", "main"), space("sp-research", "research")];
const RETIRED: NodeOut[] = [space("sp-trial", "trial", "archived")];

describe("hitSpaceName", () => {
  it("names the space by title when the search spans every space", () => {
    expect(hitSpaceName(hit("sp-research"), SPACES, [], ANY_SPACE)).toEqual({
      label: "research",
      kind: "active",
    });
  });

  it("says nothing once the filter has already determined the space", () => {
    // The server ANDs the filter onto every ranked list and onto expansion, so
    // this row provably lives in `research` — printing it would be the filter
    // read back, not a fact about the row.
    expect(hitSpaceName(hit("sp-research"), SPACES, [], "sp-research")).toBeNull();
  });

  it("says nothing under a filter expressed as a name either", () => {
    expect(hitSpaceName(hit("sp-research"), SPACES, [], "research")).toBeNull();
  });

  it("names a hit in an archived space, rather than printing its id", () => {
    // The defect a browser pass found: `in 4affabf6d856427886ad48570f5f6e20`.
    // `GET /api/spaces` is active-only by decision, so the name has to come off
    // the archived list — and the row has to say which of the two it came from.
    expect(hitSpaceName(hit("sp-trial"), SPACES, RETIRED, ANY_SPACE)).toEqual({
      label: "trial",
      kind: "archived",
    });
  });

  it("shows the id rather than a wrong name when neither list holds it", () => {
    expect(hitSpaceName(hit("sp-gone"), SPACES, RETIRED, ANY_SPACE)).toEqual({
      label: "sp-gone",
      kind: "unknown",
    });
  });

  it("reports the space list still loading as pending, not as unknown", () => {
    expect(hitSpaceName(hit("sp-research"), null, [], ANY_SPACE)).toEqual({
      label: "sp-research",
      kind: "pending",
    });
  });

  it("stays silent for a hit the server reported no space for", () => {
    expect(hitSpaceName(hit(null), SPACES, [], ANY_SPACE)).toBeNull();
  });
});

describe("hitSpaceTitle", () => {
  it("offers the filter as the next move for a live space", () => {
    expect(hitSpaceTitle({ label: "research", kind: "active" })).toMatch(
      /Narrow the space filter/,
    );
  });

  it("does not offer a filter that cannot reach an archived space", () => {
    const title = hitSpaceTitle({ label: "trial", kind: "archived" });
    expect(title).toContain("trial");
    expect(title).not.toMatch(/Narrow the space filter/);
    expect(title).toMatch(/still readable/);
  });

  it("never says a space does not exist, whatever the row resolved to", () => {
    const forbidden = /no such space|does not exist|nonexistent|missing space|not found|no record/i;
    for (const kind of ["active", "archived", "unknown", "pending"] as const) {
      expect(hitSpaceTitle({ label: "sp-x", kind })).not.toMatch(forbidden);
    }
  });
});
