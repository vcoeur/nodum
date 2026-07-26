/**
 * The search view's refused-space-filter copy (`spaceFailure.ts`).
 *
 * One property does the real work here, and it is a security property rather
 * than a wording preference: **the panel must not claim the space does not
 * exist.** The server refuses a nonexistent space and one the caller cannot read
 * with identical words on purpose, so a panel that picked a side would hand back
 * the distinction the refusal was built to withhold — and it would do it in the
 * one place a human is looking.
 *
 * The rest is routing: this copy replaces the generic "the search was refused"
 * panel for exactly one failure and for no other, whichever status the wire
 * happened to carry.
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import { describeSpaceFilterFailure } from "./spaceFailure";
import type { NodeOut } from "../../api/types";

/** A space as `GET /api/spaces` renders it. */
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

const SPACES: readonly NodeOut[] = [space("main", "main")];

/** What `GET /api/search` throws once the client has normalised it (wire: 400). */
function refusedBySearch(spaceRef = "research"): UnknownSpaceError {
  return new UnknownSpaceError(spaceRef, 400, `unknown space: ${spaceRef}`);
}

/** The same event from the listing, which answers 404. */
function refusedByListing(spaceRef = "research"): UnknownSpaceError {
  return new UnknownSpaceError(spaceRef, 404, `unknown space: ${spaceRef}`);
}

describe("the refusal that must stay ambiguous", () => {
  it("never claims the space does not exist", () => {
    const failure = describeSpaceFilterFailure(refusedBySearch(), SPACES);
    const copy = `${failure?.title} ${failure?.detail}`;
    expect(copy).not.toMatch(/no such space/i);
    expect(copy).not.toMatch(/does not exist/i);
    expect(copy).not.toMatch(/(unknown|missing|nonexistent) space/i);
    expect(copy).not.toMatch(/not found/i);
  });

  it("says what actually changed, and offers a way out", () => {
    const failure = describeSpaceFilterFailure(refusedBySearch(), SPACES);
    expect(failure?.detail).toMatch(/archived/i);
    expect(failure?.detail).toMatch(/renamed/i);
    expect(failure?.detail).toMatch(/search every space/i);
  });

  it("names the space the filter asked for, and hands it back for the reset", () => {
    const failure = describeSpaceFilterFailure(refusedBySearch("research"), SPACES);
    expect(failure?.detail).toContain("research");
    expect(failure?.space).toBe("research");
  });

  it("prefers the space's name when the list still carries it", () => {
    // A grant or a link can name a space by id; the panel should not make the
    // reader match a uuid by eye when the vocabulary is right there.
    const failure = describeSpaceFilterFailure(refusedBySearch("main"), SPACES);
    expect(failure?.detail).toContain("main");
  });
});

describe("routing", () => {
  it("answers for both wire spellings of the same event", () => {
    // 400 from search, 404 from the listing — one user-visible event, so one
    // panel. Anything keyed on the status would render two.
    const fromSearch = describeSpaceFilterFailure(refusedBySearch(), SPACES);
    const fromListing = describeSpaceFilterFailure(refusedByListing(), SPACES);
    expect(fromSearch).toEqual(fromListing);
  });

  it("declines everything else, so the generic search panel still renders", () => {
    // A 404 naming an unknown *type* and a bare 400 are the two cases a
    // status-keyed check would have swallowed.
    expect(
      describeSpaceFilterFailure(new ApiError(404, "TypeNotFound", "unknown type: memo"), SPACES),
    ).toBeNull();
    expect(
      describeSpaceFilterFailure(new ApiError(400, "ValueError", "k must be positive"), SPACES),
    ).toBeNull();
    expect(
      describeSpaceFilterFailure(new ApiError(502, "BadGateway", "Bad Gateway"), SPACES),
    ).toBeNull();
    expect(describeSpaceFilterFailure(new TypeError("Failed to fetch"), SPACES)).toBeNull();
    expect(describeSpaceFilterFailure("a string nobody wrapped", SPACES)).toBeNull();
  });
});
