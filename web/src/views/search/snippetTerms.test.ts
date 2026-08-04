/**
 * Splitting a raw query into the terms worth highlighting.
 *
 * `queryTerms` is what a search hit's snippet marks the query's own words
 * with — the vector-signal hits arrive with a plain chunk and no server
 * markers, so the client matches the typed terms itself. It mirrors the
 * server's tokenisation (`search._match_query`: whitespace-separated, all
 * required), and these tests pin the two client-side decisions that sit on
 * top of that: the terms are distinct and capped, because highlighting
 * beyond a handful of words stops being a reading aid and starts being
 * noise.
 */

import { describe, expect, it } from "vitest";
import { queryTerms } from "./snippetTerms";

describe("queryTerms", () => {
  it("splits on whitespace and lowercases", () => {
    expect(queryTerms("Xylem Vessels")).toEqual(["xylem", "vessels"]);
  });

  it("deduplicates, so a repeated word marks once", () => {
    expect(queryTerms("graph graph")).toEqual(["graph"]);
  });

  it("skips empty tokens from collapsed or padded whitespace", () => {
    expect(queryTerms("  xylem   vessels  ")).toEqual(["xylem", "vessels"]);
  });

  it("returns nothing for an empty query", () => {
    expect(queryTerms("")).toEqual([]);
    expect(queryTerms("   ")).toEqual([]);
  });

  it("caps the distinct terms", () => {
    const terms = queryTerms("one two three four five six seven eight nine ten");
    expect(terms).toEqual(["one", "two", "three", "four", "five", "six", "seven", "eight"]);
  });
});
