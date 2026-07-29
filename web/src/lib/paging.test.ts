/**
 * The shared pager (`lib/paging.ts`).
 *
 * Two screens page long lists — the journal's event diff and the review queue —
 * and the arithmetic is here so they cannot disagree about what page 3 is. What
 * is asserted is the two properties a pager gets wrong: a page index the list has
 * shrunk past, and a size that arrives as zero.
 */

import { describe, expect, it } from "vitest";
import { pageWindow } from "./paging";

describe("pageWindow", () => {
  it("cuts a long list into pages a browser can lay out", () => {
    // A 500-event cycle rendered whole came to 12 066 DOM nodes and 79 055 px,
    // past what Chrome will screenshot — and the server's cap means a real
    // nightly run reaches it by design.
    const first = pageWindow(500, 0, 25, "event");
    expect(first).toMatchObject({ page: 0, pages: 20, from: 0, to: 25 });
    expect(first.label).toBe("Events 1–25 of 500");
  });

  it("names the items it is paging, so two screens read differently", () => {
    expect(pageWindow(1043, 0, 25, "proposal").label).toBe("Proposals 1–25 of 1043");
    expect(pageWindow(0, 0, 25, "proposal").label).toBe("No proposals");
  });

  it("takes an irregular plural rather than appending an s", () => {
    expect(pageWindow(3, 0, 2, "entry", "entries").label).toBe("Entries 1–2 of 3");
  });

  it("ends on a partial page rather than past the list", () => {
    const last = pageWindow(53, 2, 25, "event");
    expect(last).toMatchObject({ page: 2, pages: 3, from: 50, to: 53 });
    expect(last.label).toBe("Events 51–53 of 53");
  });

  it("clamps a page the list has shrunk past", () => {
    // The events reload after a rollback and the queue shrinks on every accept;
    // leaving the reader on page 12 of a list that now has two would be an empty
    // screen with no way back.
    expect(pageWindow(10, 99, 25, "event")).toMatchObject({ page: 0, pages: 1, from: 0, to: 10 });
    expect(pageWindow(10, -3, 25, "event").page).toBe(0);
  });

  it("has one page and says nothing when there is nothing", () => {
    expect(pageWindow(0, 0, 25, "event")).toMatchObject({ page: 0, pages: 1, from: 0, to: 0 });
    expect(pageWindow(0, 0, 25, "event").label).toBe("No events");
  });

  it("survives a page size of zero rather than dividing by it", () => {
    expect(pageWindow(4, 0, 0, "event")).toMatchObject({ pages: 4, from: 0, to: 1 });
  });
});
