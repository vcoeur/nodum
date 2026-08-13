import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import type { NodeOut, TypeOut } from "../../api/types";
import {
  describeNodeBrowseFailure,
  nodeTypeOptions,
  readNodeBrowseState,
  sortNodes,
  toNodeBrowseParams,
} from "./nodes";

const node = (id: string, title: string, created_at: string): NodeOut => ({
  id,
  title,
  created_at,
  updated_at: created_at,
  type: "note",
  state: "active",
  space_id: "main",
  parent_id: null,
  position: null,
  content: "",
  props: {},
  created_by: "human:owner",
});

const type = (id: string, name: string): TypeOut => ({
  id,
  name,
  parent_type_id: null,
  json_schema: {},
  is_builtin: false,
});

describe("node browse URL state", () => {
  it("round-trips non-default filters and sort", () => {
    const state = readNodeBrowseState(
      new URLSearchParams("type=note&space=main&state=active&sort=title-desc"),
    );
    expect(toNodeBrowseParams(state).toString()).toBe(
      "type=note&space=main&state=active&sort=title-desc",
    );
  });

  it("drops invalid values to defaults", () => {
    expect(readNodeBrowseState(new URLSearchParams("state=lost&sort=global"))).toEqual({
      type: "",
      space: "",
      state: "",
      sort: "created-asc",
    });
  });
});

describe("node type options", () => {
  const types = [type("note", "Note"), type("concept", "Concept")];

  it("keeps an absent shared filter represented but unavailable", () => {
    expect(nodeTypeOptions(types, "retired")).toContainEqual({
      value: "retired",
      label: "retired",
      unlisted: true,
    });
  });

  it("represents a valid type selected by name without duplicating it", () => {
    expect(nodeTypeOptions(types, "Concept")).toEqual([
      { value: "note", label: "Note", unlisted: false },
      { value: "Concept", label: "Concept", unlisted: false },
    ]);
  });
});

describe("node browse failures", () => {
  const archivedSpace = node("space-1", "Research", "2026-01-01");
  archivedSpace.type = "space";
  archivedSpace.state = "archived";

  it("names an archived space without exposing the server refusal", () => {
    const failure = describeNodeBrowseFailure(
      new UnknownSpaceError("space-1", 404, "unknown space: space-1"),
      "",
      [],
      [archivedSpace],
    );
    expect(failure).toEqual({
      title: "That space filter could not be applied",
      detail:
        "Research has been archived, so the server will no longer apply it as a space filter. Its nodes remain in the graph. Clear the filter or choose an active space.",
      clear: "space",
    });
    expect(`${failure.title} ${failure.detail}`).not.toMatch(/unknown space|not found/i);
  });

  it("preserves and explains a stale node-type filter", () => {
    expect(
      describeNodeBrowseFailure(
        new ApiError(404, "TypeNotFound", "unknown node type: retired"),
        "retired",
        [],
        [],
      ),
    ).toEqual({
      title: "That node type filter could not be applied",
      detail:
        "retired is no longer in the node-type catalog. The shared URL has been preserved; clear this filter or choose a current type.",
      clear: "type",
    });
  });
});

describe("sortNodes", () => {
  const nodes = [node("b", "Alpha", "2026-01-02"), node("a", "Beta", "2026-01-01")];

  it("sorts the returned slice without mutating it", () => {
    expect(sortNodes(nodes, "title-desc").map((item) => item.title)).toEqual(["Beta", "Alpha"]);
    expect(nodes.map((item) => item.id)).toEqual(["b", "a"]);
  });
});
