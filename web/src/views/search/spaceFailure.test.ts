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
 *
 * The second property arrived from a browser: **the panel names the space.**
 * The filter refuses on exactly the reference `GET /api/spaces` stopped
 * carrying, so `spaceLabel` could only ever fall back to it — a human who
 * archived `reading` and followed their own bookmark got a panel headed by 32
 * hex characters. Both lists now, and the specific true thing when the archived
 * one answers.
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import { describeSpaceFilterFailure } from "./spaceFailure";
import type { NodeOut } from "../../api/types";

/** A space as `GET /api/spaces` renders it. */
function space(id: string, title: string, state = "active"): NodeOut {
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

const SPACES: readonly NodeOut[] = [space("main", "main")];

/** The archived space id a bookmarked `/search?space=…` link still carries. */
const RETIRED = "18ee0caa66204b5284774855a9d5cb34";

/** What the lazy archived read hands back once the filter cannot be named. */
const ARCHIVED: readonly NodeOut[] = [space(RETIRED, "reading", "archived")];

/** The ordinary case: nothing on screen needed the archived listing. */
const NONE: readonly NodeOut[] = [];

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
    const failure = describeSpaceFilterFailure(refusedBySearch(), SPACES, NONE);
    const copy = `${failure?.title} ${failure?.detail}`;
    expect(copy).not.toMatch(/no such space/i);
    expect(copy).not.toMatch(/does not exist/i);
    expect(copy).not.toMatch(/(unknown|missing|nonexistent) space/i);
    expect(copy).not.toMatch(/not found/i);
  });

  it("says what actually changed, and offers a way out", () => {
    const failure = describeSpaceFilterFailure(refusedBySearch(), SPACES, NONE);
    expect(failure?.detail).toMatch(/archived/i);
    expect(failure?.detail).toMatch(/renamed/i);
    expect(failure?.detail).toMatch(/search every space/i);
  });

  it("names the space the filter asked for, and hands it back for the reset", () => {
    const failure = describeSpaceFilterFailure(refusedBySearch("research"), SPACES, NONE);
    expect(failure?.detail).toContain("research");
    expect(failure?.space).toBe("research");
  });

  it("prefers the space's name when the list still carries it", () => {
    // A grant or a link can name a space by id; the panel should not make the
    // reader match a uuid by eye when the vocabulary is right there.
    const failure = describeSpaceFilterFailure(refusedBySearch("main"), SPACES, NONE);
    expect(failure?.detail).toContain("main");
  });
});

describe("routing", () => {
  it("answers for both wire spellings of the same event", () => {
    // 400 from search, 404 from the listing — one user-visible event, so one
    // panel. Anything keyed on the status would render two.
    const fromSearch = describeSpaceFilterFailure(refusedBySearch(), SPACES, NONE);
    const fromListing = describeSpaceFilterFailure(refusedByListing(), SPACES, NONE);
    expect(fromSearch).toEqual(fromListing);
  });

  it("declines everything else, so the generic search panel still renders", () => {
    // A 404 naming an unknown *type* and a bare 400 are the two cases a
    // status-keyed check would have swallowed.
    expect(
      describeSpaceFilterFailure(new ApiError(404, "TypeNotFound", "unknown type: memo"), SPACES, NONE),
    ).toBeNull();
    expect(
      describeSpaceFilterFailure(new ApiError(400, "ValueError", "k must be positive"), SPACES, NONE),
    ).toBeNull();
    expect(
      describeSpaceFilterFailure(new ApiError(502, "BadGateway", "Bad Gateway"), SPACES, NONE),
    ).toBeNull();
    expect(describeSpaceFilterFailure(new TypeError("Failed to fetch"), SPACES, NONE)).toBeNull();
    expect(describeSpaceFilterFailure("a string nobody wrapped", SPACES, NONE)).toBeNull();
  });
});

describe("the archived filter, which is how this panel is usually reached", () => {
  /** The refusal a bookmarked link to a space since archived produces. */
  function refusedRetired(): UnknownSpaceError {
    return new UnknownSpaceError(RETIRED, 400, `unknown space: ${RETIRED}`);
  }

  it("names the space rather than heading the panel with its id", () => {
    const failure = describeSpaceFilterFailure(refusedRetired(), SPACES, ARCHIVED);
    expect(failure?.detail).toContain("reading");
    expect(failure?.detail).not.toContain(RETIRED);
  });

  it("hands the original reference back untouched for the reset", () => {
    // The panel offers to drop the filter, and the button clears whatever the
    // URL actually carries — the *name* is for reading, not for acting on.
    const failure = describeSpaceFilterFailure(refusedRetired(), SPACES, ARCHIVED);
    expect(failure?.space).toBe(RETIRED);
  });

  it("says which of the two things happened, once it knows", () => {
    const failure = describeSpaceFilterFailure(refusedRetired(), SPACES, ARCHIVED);
    expect(failure?.detail).toMatch(/has been archived/i);
    expect(failure?.detail).not.toMatch(/renamed/i);
    // Archiving is not deletion, and the panel that meets a stale link is a
    // good place to say so.
    expect(failure?.detail).toMatch(/still readable/i);
    expect(failure?.detail).toMatch(/search every space/i);
  });

  it("never claims the space does not exist, named or not", () => {
    const forbidden =
      /no such space|does not exist|nonexistent|missing space|not found|unknown space/i;
    for (const archived of [ARCHIVED, NONE]) {
      const failure = describeSpaceFilterFailure(refusedRetired(), SPACES, archived);
      expect(`${failure?.title} ${failure?.detail}`).not.toMatch(forbidden);
    }
  });

  it("degrades to the reference while the active list is still in flight", () => {
    // Passed through as null: reading it as `[]` would let the panel assert
    // that nothing names a space whose listing has not answered.
    const failure = describeSpaceFilterFailure(refusedRetired(), null, NONE);
    expect(failure?.detail).toContain(RETIRED);
    expect(failure?.detail).toMatch(/renamed/i);
  });
});
