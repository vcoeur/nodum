/**
 * When a result row names its space (`resultSpace.ts`).
 *
 * The property under test is the *rule*, not the string: a row states the
 * space when the filter left it unknown, and stays silent when the filter
 * already determined it. Both halves matter — dropping the first makes search
 * unscannable across spaces, dropping the second prints the filter back on
 * every row.
 */

import { describe, expect, it } from "vitest";
import type { NodeOut, SearchHit } from "../../api/types";
import { ANY_SPACE } from "../../components";
import { hitSpaceLabel } from "./resultSpace";

/** A space node, trimmed to what the label lookup reads. */
function space(id: string, title: string): NodeOut {
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

describe("hitSpaceLabel", () => {
  it("names the space by title when the search spans every space", () => {
    expect(hitSpaceLabel(hit("sp-research"), SPACES, ANY_SPACE)).toBe("research");
  });

  it("says nothing once the filter has already determined the space", () => {
    // The server ANDs the filter onto every ranked list and onto expansion, so
    // this row provably lives in `research` — printing it would be the filter
    // read back, not a fact about the row.
    expect(hitSpaceLabel(hit("sp-research"), SPACES, "sp-research")).toBeNull();
  });

  it("says nothing under a filter expressed as a name either", () => {
    expect(hitSpaceLabel(hit("sp-research"), SPACES, "research")).toBeNull();
  });

  it("falls back to the raw reference while the space list is unknown", () => {
    expect(hitSpaceLabel(hit("sp-research"), [], ANY_SPACE)).toBe("sp-research");
  });

  it("names an archived space by its id rather than rendering blank", () => {
    expect(hitSpaceLabel(hit("sp-retired"), SPACES, ANY_SPACE)).toBe("sp-retired");
  });

  it("stays silent for a hit the server reported no space for", () => {
    expect(hitSpaceLabel(hit(null), SPACES, ANY_SPACE)).toBeNull();
  });
});
