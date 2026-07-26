/**
 * The search view's URL state (`searchState.ts`).
 *
 * Three semantics, not line coverage:
 *
 * - **reading is total.** The query string is user-editable and arrives from
 *   bookmarks and pasted links, so a value nobody recognises falls back to its
 *   default instead of blanking the view or throwing;
 * - **writing omits defaults**, which is what keeps the ordinary search at
 *   `/search?q=knot` and makes a shared link carry only what was chosen;
 * - **a round trip is lossless** for everything the view can actually set —
 *   the property that makes a search bookmarkable at all.
 *
 * The space filter and the meta opt-in (design decisions D1 and D3) are held to
 * the same three, plus the one thing that is specific to them: a space
 * reference is an id **or** a name, and this module must not normalise it —
 * only the server can say which one still resolves.
 */

import { describe, expect, it } from "vitest";
import {
  CLEARABLE_FILTERS,
  DEFAULT_SEARCH_STATE,
  hasActiveFilters,
  readSearchState,
  toSearchParams,
} from "./searchState";
import type { SearchState } from "./searchState";

/** Decode a query string as the router would hand it over. */
function read(queryString: string): SearchState {
  return readSearchState(new URLSearchParams(queryString));
}

/** Encode a state and read the parameters back as a plain object. */
function write(state: Partial<SearchState>): Record<string, string> {
  return Object.fromEntries(toSearchParams({ ...DEFAULT_SEARCH_STATE, ...state }));
}

describe("a bare /search", () => {
  it("reads as the defaults", () => {
    expect(read("")).toEqual(DEFAULT_SEARCH_STATE);
  });

  it("writes nothing at all", () => {
    expect(write({})).toEqual({});
  });

  it("spans every space and excludes meta", () => {
    // D1: a view is unnarrowed until it is narrowed. D3: meta is opt-in.
    expect(DEFAULT_SEARCH_STATE.space).toBe("");
    expect(DEFAULT_SEARCH_STATE.includeMeta).toBe(false);
  });
});

describe("the space filter", () => {
  it("round-trips a space named by name", () => {
    expect(read("space=research").space).toBe("research");
    expect(write({ space: "research" })).toEqual({ space: "research" });
  });

  it("keeps an id verbatim rather than resolving it to anything", () => {
    // A space reference is an id or a name everywhere in nodum, and only the
    // server can say which still resolves — normalising here would guess.
    const id = "01J8ZQ4C7K9V0MZ0R2N6S3XA7B";
    expect(read(`space=${id}`).space).toBe(id);
    expect(write({ space: id })).toEqual({ space: id });
  });

  it("trims a reference a wrapped or hand-edited URL padded", () => {
    expect(read("space=%20research%20").space).toBe("research");
  });

  it("treats a blank space parameter as no filter", () => {
    expect(read("space=").space).toBe("");
    expect(read("space=%20%20").space).toBe("");
    expect(write({ space: "" })).toEqual({});
  });

  it("survives beside the other filters", () => {
    const state = read("q=knot&space=research&type=note&state=any&k=50");
    expect(state.space).toBe("research");
    expect(state.type).toBe("note");
    expect(state.state).toBe("any");
  });
});

describe("the meta opt-in", () => {
  it("is off unless the URL says otherwise", () => {
    expect(read("").includeMeta).toBe(false);
    expect(read("meta=0").includeMeta).toBe(false);
    expect(read("meta=true").includeMeta).toBe(false);
  });

  it("reads and writes the on state", () => {
    expect(read("meta=1").includeMeta).toBe(true);
    expect(write({ includeMeta: true })).toEqual({ meta: "1" });
  });

  it("is independent of the space filter in the URL", () => {
    // They interact server-side — naming `meta` in the filter is itself the
    // opt-in — but they are two parameters and neither implies the other here.
    const state = read("space=meta");
    expect(state.space).toBe("meta");
    expect(state.includeMeta).toBe(false);
    expect(write({ space: "meta" })).toEqual({ space: "meta" });
  });
});

describe("reading is total", () => {
  it("falls back on a state nobody recognises", () => {
    expect(read("state=deleted").state).toBe(DEFAULT_SEARCH_STATE.state);
  });

  it("falls back on an unusable limit and clamps an outrageous one", () => {
    expect(read("k=nonsense").limit).toBe(DEFAULT_SEARCH_STATE.limit);
    expect(read("k=-3").limit).toBe(DEFAULT_SEARCH_STATE.limit);
    expect(read("k=1000000").limit).toBe(200);
  });

  it("reads an unknown arrangement as the fused order", () => {
    expect(read("group=whatever").group).toBe("score");
  });
});

describe("what counts as a filter", () => {
  it("says nothing is filtered on a bare search", () => {
    expect(hasActiveFilters(DEFAULT_SEARCH_STATE)).toBe(false);
    expect(hasActiveFilters({ ...DEFAULT_SEARCH_STATE, query: "knot" })).toBe(false);
  });

  it("counts the space filter and the meta opt-in", () => {
    expect(hasActiveFilters({ ...DEFAULT_SEARCH_STATE, space: "research" })).toBe(true);
    expect(hasActiveFilters({ ...DEFAULT_SEARCH_STATE, includeMeta: true })).toBe(true);
  });

  it("does not count the arrangement, which searched nothing differently", () => {
    expect(hasActiveFilters({ ...DEFAULT_SEARCH_STATE, group: "signal" })).toBe(false);
  });

  it("clears exactly what it counts", () => {
    // The badge that offers the reset and the reset itself read one object, so
    // a control added to one is never missing from the other — the drift that
    // leaves a "clear filters" button unable to clear the newest filter.
    const filtered: SearchState = {
      ...DEFAULT_SEARCH_STATE,
      query: "knot",
      type: "note",
      state: "any",
      createdBy: "agent:researcher",
      space: "research",
      includeMeta: true,
      limit: 100,
      expand: true,
      group: "signal",
    };
    expect(hasActiveFilters(filtered)).toBe(true);
    const cleared = { ...filtered, ...CLEARABLE_FILTERS };
    expect(hasActiveFilters(cleared)).toBe(false);
    // And it clears nothing it does not count.
    expect(cleared.query).toBe("knot");
    expect(cleared.group).toBe("signal");
  });
});

describe("a round trip", () => {
  it("preserves every control the view can set", () => {
    const state: SearchState = {
      query: "knot theory",
      type: "note",
      state: "proposed",
      createdBy: "agent:researcher",
      space: "research",
      includeMeta: true,
      limit: 100,
      expand: true,
      group: "signal",
    };
    expect(readSearchState(toSearchParams(state))).toEqual(state);
  });
});
