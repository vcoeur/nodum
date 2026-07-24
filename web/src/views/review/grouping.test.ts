/**
 * Deriving agent runs from a flat review queue.
 *
 * Nothing in the schema carries a batch identifier — `events.cycle_id` exists
 * but stays NULL until the Phase-5 consolidation cycle — so a "run" is inferred
 * here from arrival time. These tests pin the inference, because it is the unit
 * a human accepts or rejects and getting it wrong merges two agent runs into
 * one confirmation dialog.
 *
 * Ordering is the other half: agents are ordered by their **oldest** waiting
 * proposal (the thing that has waited longest is at the top of the page) while
 * batches within an agent are **newest first** (the run a reviewer just came to
 * look at). Those two directions are deliberate and opposite.
 */

import { describe, expect, it } from "vitest";
import type { ProposalOut } from "../../api/types";
import {
  BATCH_GAP_MS,
  describeCounts,
  groupProposals,
  PROPOSAL_KINDS,
  proposalKind,
} from "./grouping";

/** Base instant for the fixtures; a real SQLite `datetime('now')` value. */
const T0 = Date.UTC(2026, 6, 24, 21, 0, 0);

/** Render an instant the way SQLite stores it: UTC, no zone marker. */
const at = (msAfterT0: number) =>
  new Date(T0 + msAfterT0).toISOString().slice(0, 19).replace("T", " ");

let sequence = 0;

/** One queue entry. Only the fields the grouping reads are populated. */
function proposal(
  created_by: string,
  msAfterT0: number,
  kind: string = "node",
  created_at = at(msAfterT0),
): ProposalOut {
  sequence += 1;
  return {
    kind,
    id: `p${sequence}`,
    type: "note",
    created_by,
    created_at,
    node: null,
    edge: null,
    version: null,
    context: {},
  };
}

describe("proposalKind", () => {
  it("narrows the three known kinds", () => {
    for (const kind of PROPOSAL_KINDS) {
      expect(proposalKind(proposal("agent:a", 0, kind))).toBe(kind);
    }
  });

  it("returns null for a kind this build does not know", () => {
    // A future proposal kind must not be counted as one of these three.
    expect(proposalKind(proposal("agent:a", 0, "merge"))).toBeNull();
  });
});

describe("groupProposals", () => {
  it("returns nothing for an empty queue", () => {
    expect(groupProposals([])).toEqual([]);
  });

  it("splits one agent's queue at a gap wider than the threshold", () => {
    const groups = groupProposals([
      proposal("agent:researcher", 0),
      proposal("agent:researcher", 20_000),
      proposal("agent:researcher", 40_000),
      // Ten minutes later: a different run.
      proposal("agent:researcher", 640_000),
      proposal("agent:researcher", 650_000),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.batches.map((batch) => batch.proposals.length)).toEqual([2, 3]);
  });

  it("puts the newest run first within an agent", () => {
    const groups = groupProposals([
      proposal("agent:researcher", 0),
      proposal("agent:researcher", 640_000),
    ]);
    expect(groups[0]!.batches[0]!.startedAt).toBe(at(640_000));
    expect(groups[0]!.batches[1]!.startedAt).toBe(at(0));
  });

  it("keeps proposals inside a run in arrival order", () => {
    // The batch is what a manifest dialog enumerates; reversing it there would
    // list a run backwards.
    const groups = groupProposals([
      proposal("agent:researcher", 0),
      proposal("agent:researcher", 5_000),
      proposal("agent:researcher", 10_000),
    ]);
    const batch = groups[0]!.batches[0]!;
    expect(batch.proposals.map((item) => item.created_at)).toEqual([at(0), at(5_000), at(10_000)]);
    expect(batch.startedAt).toBe(at(0));
    expect(batch.endedAt).toBe(at(10_000));
  });

  it("treats a gap of exactly the threshold as the same run", () => {
    // The split is on a gap strictly wider than the threshold, so the boundary
    // is not ambiguous.
    const together = groupProposals([
      proposal("agent:a", 0),
      proposal("agent:a", BATCH_GAP_MS),
    ]);
    expect(together[0]!.batches).toHaveLength(1);

    const apart = groupProposals([
      proposal("agent:a", 0),
      proposal("agent:a", BATCH_GAP_MS + 1_000),
    ]);
    expect(apart[0]!.batches).toHaveLength(2);
  });

  it("takes the gap as a parameter, so a caller can widen it", () => {
    const proposals = [proposal("agent:a", 0), proposal("agent:a", 600_000)];
    expect(groupProposals(proposals, 60_000)[0]!.batches).toHaveLength(2);
    expect(groupProposals(proposals, 900_000)[0]!.batches).toHaveLength(1);
  });

  it("never merges two agents into one run, however close in time", () => {
    // Two agents filing in the same second are two runs, not one batch.
    const groups = groupProposals([
      proposal("agent:researcher", 0),
      proposal("agent:librarian", 1_000),
      proposal("agent:researcher", 2_000),
    ]);
    expect(groups.map((group) => group.agent)).toEqual(["agent:researcher", "agent:librarian"]);
    expect(groups[0]!.total).toBe(2);
    expect(groups[1]!.total).toBe(1);
  });

  it("orders agents by their oldest waiting proposal, not by volume", () => {
    // The queue is a waiting list: what has waited longest comes first, even
    // if another agent filed far more.
    const groups = groupProposals([
      proposal("agent:loud", 60_000),
      proposal("agent:loud", 61_000),
      proposal("agent:loud", 62_000),
      proposal("agent:patient", 0),
    ]);
    expect(groups.map((group) => group.agent)).toEqual(["agent:patient", "agent:loud"]);
    expect(groups[0]!.oldestAt).toBe(at(0));
  });

  it("sorts an out-of-order queue before batching it", () => {
    const groups = groupProposals([
      proposal("agent:a", 640_000),
      proposal("agent:a", 0),
      proposal("agent:a", 10_000),
    ]);
    expect(groups[0]!.batches.map((batch) => batch.proposals.length)).toEqual([1, 2]);
    expect(groups[0]!.oldestAt).toBe(at(0));
  });

  it("counts kinds per batch and per agent", () => {
    const groups = groupProposals([
      proposal("agent:a", 0, "node"),
      proposal("agent:a", 1_000, "edge"),
      proposal("agent:a", 2_000, "edge"),
      proposal("agent:a", 640_000, "update"),
    ]);
    expect(groups[0]!.counts).toEqual({ node: 1, edge: 2, update: 1 });
    // Newest run first, so the update batch is index 0.
    expect(groups[0]!.batches[0]!.counts).toEqual({ node: 0, edge: 0, update: 1 });
    expect(groups[0]!.batches[1]!.counts).toEqual({ node: 1, edge: 2, update: 0 });
  });

  it("counts an unknown kind into nothing rather than into a wrong bucket", () => {
    const groups = groupProposals([proposal("agent:a", 0, "merge")]);
    expect(groups[0]!.counts).toEqual({ node: 0, edge: 0, update: 0 });
    expect(groups[0]!.total).toBe(1);
  });

  it("gives every batch a distinct, stable React key", () => {
    const proposals = [
      proposal("agent:a", 0),
      proposal("agent:a", 640_000),
      proposal("agent:b", 0),
    ];
    const keys = groupProposals(proposals).flatMap((group) =>
      group.batches.map((batch) => batch.key),
    );
    expect(new Set(keys).size).toBe(keys.length);
    // Stable across calls: the key must not be a render counter.
    expect(groupProposals(proposals).flatMap((g) => g.batches.map((b) => b.key))).toEqual(keys);
  });

  it("keeps an unparseable timestamp in the queue instead of dropping the proposal", () => {
    // A row the client cannot date still has to be reviewable.
    const groups = groupProposals([
      proposal("agent:a", 0),
      proposal("agent:a", 0, "node", "not a timestamp"),
    ]);
    expect(groups[0]!.total).toBe(2);
    expect(groups[0]!.batches.flatMap((batch) => batch.proposals)).toHaveLength(2);
  });
});

describe("describeCounts", () => {
  it("skips the zeroes", () => {
    expect(describeCounts({ node: 3, edge: 0, update: 0 })).toBe("3 nodes");
  });

  it("keeps the kinds in a fixed order", () => {
    expect(describeCounts({ node: 1, edge: 2, update: 3 })).toBe(
      "1 node · 2 edges · 3 version updates",
    );
  });

  it("says `version update` rather than `update`, which alone means nothing", () => {
    expect(describeCounts({ node: 0, edge: 0, update: 1 })).toBe("1 version update");
  });

  it("is empty when there is nothing to count", () => {
    expect(describeCounts({ node: 0, edge: 0, update: 0 })).toBe("");
  });
});
