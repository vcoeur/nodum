import { describe, expect, it } from "vitest";
import { DEFAULT_SEARCH_STATE } from "./searchState";
import type { SearchState } from "./searchState";
import { describeNoResults } from "./noResults";

const state = (patch: Partial<SearchState> = {}): SearchState => ({
  ...DEFAULT_SEARCH_STATE,
  query: "how does compaction work as a state store",
  ...patch,
});

describe("describeNoResults", () => {
  it("never advises typing fewer words", () => {
    // The matcher used to require every term, so shortening the query was the
    // only way through. It now ORs the terms under an IDF quorum: a rare word
    // is what earns a match and a common one costs nothing, so "try fewer
    // words" is close to the opposite of the right advice — dropping the rare
    // word is exactly how a searchable question stops being searchable.
    const advice = describeNoResults(state()).join(" ").toLowerCase();
    expect(advice).not.toContain("fewer words");
    expect(advice).not.toContain("anded");
    expect(advice).not.toContain("every term");
  });

  it("names the rule that replaced the conjunction", () => {
    // The quorum weighs a term by how much it discriminates, which is the one
    // fact a human needs to act on: the fix for an empty result is a rarer
    // word, not a shorter query.
    const advice = describeNoResults(state()).join(" ").toLowerCase();
    expect(advice).toContain("rarest");
  });

  it("offers the filters only when some are set", () => {
    expect(describeNoResults(state()).join(" ")).not.toContain("filter");
    expect(describeNoResults(state({ type: "note" })).join(" ")).toContain("filter");
  });

  it("offers neighbours only while the expansion is off", () => {
    expect(describeNoResults(state()).join(" ")).toContain("neighbour");
    expect(describeNoResults(state({ expand: true })).join(" ")).not.toContain("neighbour");
  });

  it("suggests adding a distinctive word only when the query is short", () => {
    // A one-word query has nothing to weigh, so the quorum cannot help it; a
    // long question already carries several rare terms and the advice would be
    // noise.
    expect(describeNoResults(state({ query: "compaction" })).join(" ")).toContain(
      "another distinctive word",
    );
    expect(describeNoResults(state()).join(" ")).not.toContain("another distinctive word");
  });

  it("always returns at least one sentence", () => {
    const bare = describeNoResults(state({ expand: true, type: "note" }));
    expect(bare.length).toBeGreaterThan(0);
  });
});
