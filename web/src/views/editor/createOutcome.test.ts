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
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import { describeLanding, describeWriteFailure } from "./createOutcome";
import type { NodeOut } from "../../api/types";

/** A space as `GET /api/spaces` renders it — a node of type `space` in meta. */
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

/** The node a create came back with. */
function created(spaceId: string | null): NodeOut {
  return { ...space("node-new", "Fresh"), type: "note", space_id: spaceId };
}

const SPACES: readonly NodeOut[] = [
  space("main", "main"),
  space("01J8ZQ4C7K9V0MZ0R2N6S3XA7B", "research"),
];

/** What the server answers a write target it will not resolve. */
function refusedTarget(status = 404): ApiError {
  return new ApiError(status, "TypeNotFound", "unknown space: research");
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
  it("recognises the bare error the create path throws", () => {
    // `POST /api/nodes` is not one of the two reads the client normalises, so
    // the failure arrives as a plain ApiError and this is the only thing that
    // can tell it apart from any other 404.
    expect(describeWriteFailure(refusedTarget(), "research", SPACES)).not.toBeNull();
  });

  it("recognises the normalised error too, if a create is ever routed through it", () => {
    const normalised = new UnknownSpaceError("research", 404, "unknown space: research");
    expect(describeWriteFailure(normalised, "research", SPACES)).not.toBeNull();
  });

  it("declines every other failure, so the generic save-error copy stands", () => {
    expect(describeWriteFailure(new ApiError(404, "TypeNotFound", "unknown type: memo"), "main", SPACES)).toBeNull();
    expect(describeWriteFailure(new ApiError(503, "DatabaseBusy", "database is locked"), "main", SPACES)).toBeNull();
    expect(describeWriteFailure(new TypeError("Failed to fetch"), "main", SPACES)).toBeNull();
    expect(describeWriteFailure("a string nobody wrapped", "main", SPACES)).toBeNull();
  });

  it("never claims the space does not exist", () => {
    // The refusal is word-for-word identical for a space that was never created
    // and one the caller cannot read. Copy that picked a side would leak it.
    const message = describeWriteFailure(refusedTarget(), "research", SPACES) ?? "";
    expect(message).not.toMatch(/no such space/i);
    expect(message).not.toMatch(/does not exist/i);
    expect(message).not.toMatch(/never (existed|created)/i);
  });

  it("names the target and points at the control that changes it", () => {
    const message = describeWriteFailure(refusedTarget(), "research", SPACES) ?? "";
    expect(message).toContain("research");
    expect(message).toMatch(/choose another space/i);
    // Losing the buffer is the editor's cardinal failure; the panel says it did
    // not happen.
    expect(message).toMatch(/still here/i);
  });
});
