/**
 * The space picker's vocabulary (`spaceOptions.ts`).
 *
 * What is asserted is the two properties the control's honesty rests on, not
 * the shape of the array:
 *
 * - **the current selection is always representable.** A filter set to a space
 *   that has since been archived must keep saying so; an option list that
 *   quietly dropped it would render "Any space" over a narrowed read;
 * - **an id and a name are the same space.** Every nodum surface resolves both,
 *   so a picker that treated them as two entries would offer the human a
 *   duplicate and then disagree with the server about which was selected.
 */

import { describe, expect, it } from "vitest";
import type { NodeOut } from "../api/types";
import { ANY_SPACE, resolveSpaceValue, spaceLabel, spaceOptions } from "./spaceOptions";

/** A space node, trimmed to what the picker reads. */
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
    created_by: "human",
    created_at: "2026-07-26 09:00:00",
    updated_at: "2026-07-26 09:00:00",
  };
}

const SPACES: NodeOut[] = [
  space("sp-main", "main"),
  space("sp-research", "research"),
  space("sp-meta", "meta"),
];

describe("spaceLabel", () => {
  it("names a space by its title", () => {
    expect(spaceLabel(SPACES, "sp-research")).toBe("research");
  });

  it("accepts the name as readily as the id", () => {
    expect(spaceLabel(SPACES, "research")).toBe("research");
  });

  it("falls back to the bare reference for a space the list does not hold", () => {
    expect(spaceLabel(SPACES, "sp-archived")).toBe("sp-archived");
  });

  it("falls back to the id for a space with no title", () => {
    expect(spaceLabel([space("sp-odd", null)], "sp-odd")).toBe("sp-odd");
  });
});

describe("resolveSpaceValue", () => {
  it("maps a name onto the id the options carry", () => {
    expect(resolveSpaceValue(SPACES, "research")).toBe("sp-research");
  });

  it("leaves an id alone", () => {
    expect(resolveSpaceValue(SPACES, "sp-research")).toBe("sp-research");
  });

  it("leaves the any-space sentinel alone", () => {
    expect(resolveSpaceValue(SPACES, ANY_SPACE)).toBe(ANY_SPACE);
  });

  it("passes an unresolvable reference through rather than erasing it", () => {
    expect(resolveSpaceValue(SPACES, "sp-archived")).toBe("sp-archived");
  });
});

describe("spaceOptions", () => {
  it("leads with the any-space sentinel, so the default is the first choice", () => {
    const options = spaceOptions(SPACES);
    expect(options[0]).toEqual({ value: ANY_SPACE, label: "Any space" });
  });

  it("offers the sentinel alone when there are no spaces to offer", () => {
    expect(spaceOptions([])).toEqual([{ value: ANY_SPACE, label: "Any space" }]);
  });

  it("sorts the spaces by label, not by the server's id order", () => {
    expect(spaceOptions(SPACES).map((option) => option.label)).toEqual([
      "Any space",
      "main",
      "meta",
      "research",
    ]);
  });

  it("keeps a selection the list does not hold, and marks it unlisted", () => {
    const options = spaceOptions(SPACES, "sp-archived");
    expect(options.at(-1)).toEqual({
      value: "sp-archived",
      label: "sp-archived",
      unlisted: true,
    });
  });

  it("does not duplicate a selection given by name instead of by id", () => {
    const options = spaceOptions(SPACES, "research");
    expect(options.filter((option) => option.label === "research")).toHaveLength(1);
    expect(options.some((option) => option.unlisted)).toBe(false);
  });

  it("adds nothing for the any-space selection", () => {
    expect(spaceOptions(SPACES, ANY_SPACE)).toHaveLength(SPACES.length + 1);
  });

  it("takes a caller's own sentinel label", () => {
    expect(spaceOptions(SPACES, ANY_SPACE, "Every space")[0]?.label).toBe("Every space");
  });
});
