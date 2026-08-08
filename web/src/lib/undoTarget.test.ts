import { describe, expect, it } from "vitest";
import { undoableSeq } from "./undoTarget";
import type { EventOut } from "../api/types";

function event(over: Partial<EventOut> = {}): EventOut {
  return {
    seq: 42,
    actor: "human:alice",
    op: "node.archive",
    payload: { before: { id: "n1", state: "active" }, after: { id: "n1", state: "archived" } },
    cycle_id: null,
    created_at: "2026-08-08 10:00:00",
    ...over,
  };
}

const WRITE = { op: "node.archive", rowId: "n1" };

describe("undoableSeq", () => {
  it("returns the head's seq when it is the write just made", () => {
    expect(undoableSeq([event()], WRITE)).toBe(42);
  });

  it("refuses when the head is a different op", () => {
    // Something landed in between. A bare undo here would reverse a stranger's
    // write under a label naming the human's own action.
    expect(undoableSeq([event({ op: "node.update" })], WRITE)).toBeNull();
  });

  it("refuses when the head names a different row", () => {
    const other = event({
      payload: { before: { id: "n2" }, after: { id: "n2", state: "archived" } },
    });
    expect(undoableSeq([other], WRITE)).toBeNull();
  });

  it("refuses a cycle-stamped event", () => {
    // `service.undo` refuses these outright and points at `rollback`, so an
    // undo button over one would offer something the server will not do.
    expect(undoableSeq([event({ cycle_id: "c1" })], WRITE)).toBeNull();
  });

  it("refuses an empty log", () => {
    expect(undoableSeq([], WRITE)).toBeNull();
  });

  it("reads the id off `before` when a delete left no `after`", () => {
    const deleted = event({ op: "node.delete", payload: { before: { id: "n1" }, after: null } });
    expect(undoableSeq([deleted], { op: "node.delete", rowId: "n1" })).toBe(42);
  });

  it("refuses a payload with no row id at all", () => {
    expect(undoableSeq([event({ payload: { before: null, after: null } })], WRITE)).toBeNull();
  });

  it("looks only at the head, never further back", () => {
    // The write is in the log but no longer the last thing that happened, so
    // undoing "the latest" would reverse the wrong one.
    const log = [event({ op: "edge.create", seq: 43 }), event({ seq: 42 })];
    expect(undoableSeq(log, WRITE)).toBeNull();
  });
});
