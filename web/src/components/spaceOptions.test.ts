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
 *
 * A third joined them once the archived name could reach this module, and it is
 * the one that would have caught the defect this file was extended for:
 * **naming an archived selection must not make archived spaces selectable.** An
 * archived space is one a human may *leave* and never *return to* — offering it
 * would file work into a space the server refuses, which is the exact failure
 * D1a exists to prevent. The two cases are asserted together on purpose: the
 * name appears, and the choices are still only the active list.
 */

import { describe, expect, it } from "vitest";
import type { NodeOut } from "../api/types";
import {
  ANY_SPACE,
  resolveSpaceValue,
  spaceLabel,
  spaceOptions,
  unlistedMark,
} from "./spaceOptions";
import { nameSpace } from "./spaceNaming";

/** A space node, trimmed to what the picker reads. */
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

const SPACES: NodeOut[] = [
  space("sp-main", "main"),
  space("sp-research", "research"),
  space("sp-meta", "meta"),
];

/** What the lazy archived read hands back, once something on screen needs it. */
const ARCHIVED: NodeOut[] = [space("18ee0caa66204b5284774855a9d5cb34", "reading", "archived")];

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
      archived: false,
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
    expect(spaceOptions(SPACES, ANY_SPACE, null, "Every space")[0]?.label).toBe("Every space");
  });
});

describe("an archived space that is already the selection", () => {
  /**
   * The scenario, observed in a browser: the write target is `reading`, the
   * human archives `reading` on `/spaces`, and opens `/editor`. The target
   * survives on purpose — rewriting it to `main` would file the next node
   * somewhere nobody chose — so the picker has to render a value
   * `GET /api/spaces` no longer carries. It rendered
   * `18ee0caa66204b5284774855a9d5cb34 (unavailable)`.
   */
  const target = "18ee0caa66204b5284774855a9d5cb34";
  const targetName = nameSpace(target, SPACES, ARCHIVED);

  it("names it instead of printing its id", () => {
    const options = spaceOptions(SPACES, target, targetName);
    expect(options.at(-1)).toEqual({
      value: target,
      label: "reading",
      unlisted: true,
      archived: true,
    });
    expect(options.map((option) => option.label)).not.toContain(target);
  });

  it("does not make archived spaces selectable, which is the point of archiving", () => {
    // The regression guard. Naming the selection may only ever add *the
    // selection*: everything a human can newly choose is still the active list
    // and the sentinel. A picker that offered `reading` would let them file a
    // node into a space the server refuses — worse than the bare id it fixed.
    const options = spaceOptions(SPACES, target, targetName);
    const selectable = options.filter((option) => option.unlisted !== true);
    expect(selectable.map((option) => option.value)).toEqual([
      ANY_SPACE,
      "sp-main",
      "sp-meta",
      "sp-research",
    ]);
    expect(selectable.map((option) => option.label)).not.toContain("reading");
  });

  it("leaves with the selection: choose another space and it is gone", () => {
    // What makes the option honest. It exists because a controlled `<select>`
    // must render its own value, not because the space is on offer — so the
    // moment the value is something else there is no trace of it.
    const options = spaceOptions(SPACES, "sp-main", nameSpace("sp-main", SPACES, ARCHIVED));
    expect(options.some((option) => option.label === "reading")).toBe(false);
    expect(options.some((option) => option.unlisted)).toBe(false);
  });

  it("marks it archived rather than unavailable, because those differ", () => {
    // "Unavailable" is the shrug for a reference nothing named. This space is
    // there, retired, and everything written in it is still readable.
    const options = spaceOptions(SPACES, target, targetName);
    expect(unlistedMark(options.at(-1)?.archived)).toBe("(archived)");
  });

  it("still says unavailable when neither list named the selection", () => {
    const unknownName = nameSpace("sp-gone", SPACES, ARCHIVED);
    const options = spaceOptions(SPACES, "sp-gone", unknownName);
    expect(options.at(-1)).toEqual({
      value: "sp-gone",
      label: "sp-gone",
      unlisted: true,
      archived: false,
    });
    expect(unlistedMark(options.at(-1)?.archived)).toBe("(unavailable)");
  });

  it("does not claim archived for a selection resolved before the lists answered", () => {
    // `pending` is not `unknown` and neither is `archived`: with the active
    // list in flight nothing has been ruled out, so the mark stays the shrug.
    const options = spaceOptions([], target, nameSpace(target, null, []));
    expect(options.at(-1)?.archived).toBe(false);
    expect(options.at(-1)?.label).toBe(target);
  });
});

describe("unlistedMark", () => {
  it("keeps the two words apart, since they say different things", () => {
    expect(unlistedMark(true)).toBe("(archived)");
    expect(unlistedMark(false)).toBe("(unavailable)");
    expect(unlistedMark()).toBe("(unavailable)");
  });
});
