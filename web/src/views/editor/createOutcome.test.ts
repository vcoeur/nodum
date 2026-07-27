/**
 * The editor's two space-shaped answers (`createOutcome.ts`).
 *
 * These pin design decision **D1a**, which the phase's own design note calls the
 * risk surface of the split model: *a persisted write target that is rendered
 * too quietly will file nodes into the wrong space.* Two properties carry it,
 * and both are semantics rather than coverage:
 *
 * - the post-create confirmation **always names the landing space**, taken from
 *   the server's answer, and says so when that is not the space that was asked
 *   for;
 * - a refused write target is legible **without asserting the space does not
 *   exist**. The server refuses a nonexistent space and an unreadable one with
 *   the same words on purpose, and copy that resolved the ambiguity would leak
 *   the difference the refusal was built to hide.
 *
 * A third arrived from a browser: **the refusal names the space.** The write
 * target stops resolving because the human archived it, which is precisely the
 * case the active-only listing cannot name — so both refusals read
 * `The write target 18ee0caa66204b5284774855a9d5cb34 would not resolve` at the
 * person who retired `reading` a minute earlier. They resolve over both lists
 * now, and say the specific true thing when the archived one answers.
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import {
  describeDetachedWriteFailure,
  describeLanding,
  describeWriteFailure,
} from "./createOutcome";
import type { NodeOut } from "../../api/types";

/** A space as `GET /api/spaces` renders it — a node of type `space` in meta. */
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

/** The node a create came back with. */
function created(spaceId: string | null): NodeOut {
  return { ...space("node-new", "Fresh"), type: "note", space_id: spaceId };
}

const SPACES: readonly NodeOut[] = [
  space("main", "main"),
  space("01J8ZQ4C7K9V0MZ0R2N6S3XA7B", "research"),
];

/** The archived space id a real write target was left pointing at. */
const RETIRED = "18ee0caa66204b5284774855a9d5cb34";

/** What the lazy archived read hands back once the target cannot be named. */
const ARCHIVED: readonly NodeOut[] = [space(RETIRED, "reading", "archived")];

/** The ordinary case: nothing on screen needed the archived listing. */
const NONE: readonly NodeOut[] = [];

/**
 * A refused write target as the editor meets it.
 *
 * `api/client.ts` normalises every call that names a space, `createNode`
 * included, so this is the *only* shape the editor ever catches — the bare
 * `ApiError` the wire carried never reaches this module.
 */
function refusedTarget(wireStatus = 404): UnknownSpaceError {
  return new UnknownSpaceError("research", wireStatus, "unknown space: research");
}

describe("the landing notice", () => {
  it("names the space in the headline rather than saying only that something was created", () => {
    const notice = describeLanding(created("main"), "main", SPACES);
    expect(notice.title).toBe("Created in main");
    expect(notice.title).not.toBe("Created");
  });

  it("names the space rather than showing its id", () => {
    const notice = describeLanding(
      created("01J8ZQ4C7K9V0MZ0R2N6S3XA7B"),
      "01J8ZQ4C7K9V0MZ0R2N6S3XA7B",
      SPACES,
    );
    expect(notice.title).toBe("Created in research");
    expect(notice.title).not.toContain("01J8ZQ");
  });

  it("says the target is sticky, which is the whole point of showing it", () => {
    const notice = describeLanding(created("main"), "main", SPACES);
    expect(notice.detail).toMatch(/keep landing/i);
  });

  it("treats a target named by name and a landing reported by id as one space", () => {
    // Every space reference is an id *or* a name, so the naive comparison would
    // report a mismatch on the ordinary case of picking `research` from a list.
    const notice = describeLanding(created("01J8ZQ4C7K9V0MZ0R2N6S3XA7B"), "research", SPACES);
    expect(notice.title).toBe("Created in research");
    expect(notice.detail).not.toMatch(/but the server/i);
  });

  it("reports a real mismatch, naming both spaces", () => {
    const notice = describeLanding(created("main"), "research", SPACES);
    expect(notice.title).toBe("Created in main");
    expect(notice.detail).toContain("research");
    expect(notice.detail).toContain("main");
  });

  it("still resolves a landing space the list has never heard of", () => {
    // The space list is fetched separately and may not have answered yet; the
    // confirmation must not go blank while it is missing.
    const notice = describeLanding(created("archive-2024"), "archive-2024", []);
    expect(notice.title).toBe("Created in archive-2024");
  });

  it("says so plainly when the response carried no space at all", () => {
    const notice = describeLanding(created(null), "main", SPACES);
    expect(notice.title).toBe("Created");
    expect(notice.detail).toMatch(/no space/i);
  });
});

describe("a write target the server refused", () => {
  it("recognises the refusal the client normalises the create path into", () => {
    expect(describeWriteFailure(refusedTarget(), "research", SPACES, NONE)).not.toBeNull();
  });

  it("recognises it whichever status the wire carried", () => {
    // `POST /api/nodes` answers 404 today, but the class exists precisely
    // because sibling routes answer 400 for the same event.
    expect(describeWriteFailure(refusedTarget(400), "research", SPACES, NONE)).not.toBeNull();
  });

  it("declines every other failure, so the generic save-error copy stands", () => {
    expect(describeWriteFailure(new ApiError(404, "TypeNotFound", "unknown type: memo"), "main", SPACES, NONE)).toBeNull();
    expect(describeWriteFailure(new ApiError(503, "DatabaseBusy", "database is locked"), "main", SPACES, NONE)).toBeNull();
    expect(describeWriteFailure(new TypeError("Failed to fetch"), "main", SPACES, NONE)).toBeNull();
    expect(describeWriteFailure("a string nobody wrapped", "main", SPACES, NONE)).toBeNull();
  });

  it("re-derives nothing: a bare unknown-space ApiError is the client's bug, not this module's", () => {
    // If this ever stops being null, the client stopped normalising `createNode`
    // — and the fix is there, not a second message match here.
    const bare = new ApiError(404, "TypeNotFound", "unknown space: research");
    expect(describeWriteFailure(bare, "research", SPACES, NONE)).toBeNull();
  });

  it("never claims the space does not exist", () => {
    // The refusal is word-for-word identical for a space that was never created
    // and one the caller cannot read. Copy that picked a side would leak it.
    const message = describeWriteFailure(refusedTarget(), "research", SPACES, NONE) ?? "";
    expect(message).not.toMatch(/no such space/i);
    expect(message).not.toMatch(/does not exist/i);
    expect(message).not.toMatch(/never (existed|created)/i);
  });

  it("names the target and points at the control that changes it", () => {
    const message = describeWriteFailure(refusedTarget(), "research", SPACES, NONE) ?? "";
    expect(message).toContain("research");
    expect(message).toMatch(/choose another space/i);
    // Losing the buffer is the editor's cardinal failure; the panel says it did
    // not happen.
    expect(message).toMatch(/still here/i);
  });
});

describe("a refused write the editor has already let go of", () => {
  /**
   * The scenario: the write target is `research`; it is archived from another
   * tab or the CLI; the human types in `/editor` and clicks another node before
   * the 1200 ms debounce fires. `flushBuffer` → `flushLeftover` →
   * `createNode({space:"research"})` → `UnknownSpaceError`, reported through
   * the toast because the buffer it belonged to is no longer on screen.
   *
   * Both of those paths called `toast.showError`, which formats an `ApiError`
   * as `type: message` — so the toast read, verbatim,
   * **"UnknownSpace: unknown space: research"**. The copy rule forbids exactly
   * that: the server's wording is identical for a space that was never created
   * and one the caller holds no grant on, so any surface that renders it is an
   * existence oracle over the whole file.
   */
  it("recognises the same refusal the in-place path does", () => {
    expect(describeDetachedWriteFailure(refusedTarget(), "research", SPACES, NONE)).not.toBeNull();
    expect(describeDetachedWriteFailure(refusedTarget(400), "research", SPACES, NONE)).not.toBeNull();
  });

  it("never renders the server's own wording, which is what the toast used to show", () => {
    const message = describeDetachedWriteFailure(refusedTarget(), "research", SPACES, NONE) ?? "";
    expect(message).not.toMatch(/unknown space/i);
    expect(message).not.toMatch(/UnknownSpace/);
    expect(message).not.toMatch(/no such space/i);
    expect(message).not.toMatch(/does not exist/i);
    expect(message).not.toMatch(/not found/i);
  });

  it("says what changed, and names the space it changed about", () => {
    const message = describeDetachedWriteFailure(refusedTarget(), "research", SPACES, NONE) ?? "";
    expect(message).toContain("research");
    expect(message).toMatch(/archived/i);
    expect(message).toMatch(/renamed/i);
  });

  it("does not promise the text is still there, because it is not", () => {
    // The in-place panel's last sentence is "your text is still here" — true of
    // a buffer on screen, false of one the next document replaced. This path is
    // the only place in the editor where work is genuinely lost, and saying
    // otherwise would send the human back to look for it.
    const message = describeDetachedWriteFailure(refusedTarget(), "research", SPACES, NONE) ?? "";
    expect(message).not.toMatch(/still here/i);
    expect(message).toMatch(/could not be kept/i);
  });

  it("declines everything else, so the shared classifier still describes it", () => {
    expect(
      describeDetachedWriteFailure(new ApiError(503, "DatabaseBusy", "database is locked"), "main", SPACES, NONE),
    ).toBeNull();
    expect(describeDetachedWriteFailure(new TypeError("Failed to fetch"), "main", SPACES, NONE)).toBeNull();
    // Same inverse assertion as the in-place path: a bare `ApiError` carrying
    // the message means the client stopped normalising, and the fix is there.
    expect(
      describeDetachedWriteFailure(
        new ApiError(404, "TypeNotFound", "unknown space: research"),
        "research",
        SPACES,
        NONE,
      ),
    ).toBeNull();
  });
});

describe("the refusal names the space, which is what a human archived", () => {
  /**
   * The observed defect. The write target was `reading`; the human archived
   * `reading` on `/spaces` and went back to `/editor`. Both refusals read
   * *"The write target 18ee0caa66204b5284774855a9d5cb34 would not resolve"* —
   * the id of the space they had retired a minute earlier, in the two places
   * that were supposed to tell them what had happened.
   *
   * The target is archived precisely *because* `GET /api/spaces` no longer
   * carries it, so the active list can never be the answer here; the archived
   * listing is.
   */
  function refusedRetired(): UnknownSpaceError {
    return new UnknownSpaceError(RETIRED, 404, `unknown space: ${RETIRED}`);
  }

  it("names an archived target in the in-place panel rather than printing its id", () => {
    const message = describeWriteFailure(refusedRetired(), RETIRED, SPACES, ARCHIVED) ?? "";
    expect(message).toContain("reading");
    expect(message).not.toContain(RETIRED);
  });

  it("names it in the detached toast too, which is where the text was also lost", () => {
    const message = describeDetachedWriteFailure(refusedRetired(), RETIRED, SPACES, ARCHIVED) ?? "";
    expect(message).toContain("reading");
    expect(message).not.toContain(RETIRED);
    expect(message).toMatch(/could not be kept/i);
  });

  it("says the specific true thing once it knows which of the two happened", () => {
    const message = describeWriteFailure(refusedRetired(), RETIRED, SPACES, ARCHIVED) ?? "";
    expect(message).toMatch(/has been archived/i);
    // Not the disjunction: the archived listing answered, so guessing between
    // archived and renamed would be vaguer than what is known.
    expect(message).not.toMatch(/renamed/i);
    // And still the reason the panel exists.
    expect(message).toMatch(/still here/i);
  });

  it("keeps the disjunction when nothing named the target", () => {
    const message = describeWriteFailure(refusedRetired(), RETIRED, SPACES, NONE) ?? "";
    expect(message).toMatch(/archived/i);
    expect(message).toMatch(/renamed/i);
  });

  it("never claims the space does not exist, named or not", () => {
    const forbidden =
      /no such space|does not exist|nonexistent|missing space|not found|unknown space/i;
    for (const archived of [ARCHIVED, NONE]) {
      expect(describeWriteFailure(refusedRetired(), RETIRED, SPACES, archived) ?? "").not.toMatch(
        forbidden,
      );
      expect(
        describeDetachedWriteFailure(refusedRetired(), RETIRED, SPACES, archived) ?? "",
      ).not.toMatch(forbidden);
    }
  });

  it("degrades to the reference while the active list is still in flight", () => {
    // Null in, null through: `?? []` at the call site would have the copy call
    // a live space unnameable on the strength of a request that has not
    // answered. There is nothing better to show than the reference here, and
    // nothing worse to claim either.
    const message = describeWriteFailure(refusedRetired(), RETIRED, null, NONE) ?? "";
    expect(message).toContain(RETIRED);
    expect(message).toMatch(/archived/i);
    expect(message).toMatch(/renamed/i);
  });

  it("still names the landing space with no list at all", () => {
    // `describeLanding` takes the same nullable list; a node cannot land in an
    // archived space, so it resolves against the active one alone.
    expect(describeLanding(created("main"), "main", null).title).toBe("Created in main");
  });
});
