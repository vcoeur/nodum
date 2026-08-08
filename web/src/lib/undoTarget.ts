/**
 * Which event an undo affordance is allowed to reverse.
 *
 * `POST /api/undo` with no `seq` reverses "the latest reversible event", and
 * that is exactly what a button in a browser must never send. Between the write
 * a human just made and the moment they reach for its undo, an agent holding
 * `edit` can have written; a bare undo would then reverse *that* — silently,
 * under a label naming the human's own action. So the surfaces here undo **one
 * named seq**, and only ever the seq they can prove is the write they just
 * made.
 *
 * The proof is deliberately narrow, and every rejection here is a refusal to
 * offer undo rather than a failed undo:
 *
 * - the log head has to carry the op the write emits;
 * - it has to name the same row;
 * - it must carry no `cycle_id` — `service.undo` refuses a cycle-stamped event
 *   and points at `rollback`, and a button that produced that refusal would be
 *   offering something it cannot do.
 *
 * Anything else means something landed in between, and the honest answer is a
 * confirmation with no undo on it.
 */

import type { EventOut, JsonObject } from "../api/types";

/** The write an undo affordance is about. */
export interface WriteJustMade {
  /** The event op it emits — `node.archive` for a node retirement. */
  op: string;
  /** The id of the row it touched. */
  rowId: string;
}

/**
 * The seq to offer an undo for, or null when the log head is not that write.
 *
 * @param events The event log, newest first — `GET /api/events` order.
 * @param write The write just made.
 * @returns The event's seq, or null when undo must not be offered.
 */
export function undoableSeq(events: readonly EventOut[], write: WriteJustMade): number | null {
  const head = events[0];
  if (head === undefined) return null;
  if (head.op !== write.op) return null;
  // A consolidation cycle is the unit a human takes back; `service.undo`
  // refuses its events outright and names `rollback` instead.
  if (head.cycle_id !== null) return null;
  if (eventRowId(head.payload) !== write.rowId) return null;
  return head.seq;
}

/**
 * The row id an event's payload names, or null when it names none.
 *
 * A state transition records `{before, after}` row dicts; `after` is the state
 * the row was left in, so it is the one that identifies what was touched. A
 * create has no `before` and a delete no `after`, so both halves are read.
 *
 * @param payload The event payload.
 */
function eventRowId(payload: JsonObject): string | null {
  for (const key of ["after", "before"] as const) {
    const side = payload[key];
    if (side === null || typeof side !== "object" || Array.isArray(side)) continue;
    const id = (side as JsonObject)["id"];
    if (typeof id === "string") return id;
  }
  return null;
}
