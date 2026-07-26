/**
 * Deriving agent runs from a flat review queue, and filing them under spaces.
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
 *
 * ## The space level (design decision D4)
 *
 * The outer grouping carries a claim the queue by itself cannot make: **an
 * `edit`-granted space never reaches this queue at all**, because its agents
 * write `active`. Grouped by agent alone that territory is simply missing, and
 * "nothing was proposed here" is indistinguishable from "this space governs
 * itself". So the section for such a space is emitted *because* it is empty,
 * and the tests below pin all three ways that could quietly stop working:
 *
 * - the self-governing section vanishing (D4's original bug, restored);
 * - it appearing for a space that is merely quiet, which would tell the human
 *   that a `suggest`-granted space governs itself when it does not;
 * - it appearing when the space list is unknown, which would state a fact the
 *   view is in no position to know.
 *
 * The other half is where a space comes from. The server states one for every
 * kind — a node on its row, an edge and an update in the reviewer context, where
 * `service._node_ref` carries `space_id` beside id and title — so the only thing
 * that lands in the unreported bucket is a proposal whose referenced node no
 * longer resolves at all. That bucket is tested *because* it should now be
 * empty: a queue that quietly filed such a proposal under a guess would be worse
 * than one that admits it.
 */

import { describe, expect, it } from "vitest";
import type { GrantOut, NodeOut, ProposalOut, SpaceOut, VersionOut } from "../../api/types";
import {
  BATCH_GAP_MS,
  describeCounts,
  editGrantedAgents,
  groupProposals,
  groupProposalsBySpace,
  PROPOSAL_KINDS,
  proposalKind,
  proposalSpace,
  UNREPORTED_SPACE,
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

/** A node row, as a proposed-node entry carries it. */
function node(id: string, spaceId: string | null): NodeOut {
  return {
    id,
    space_id: spaceId,
    type: "note",
    parent_id: null,
    position: null,
    title: id,
    content: "",
    props: {},
    state: "proposed",
    created_by: "agent:a",
    created_at: at(0),
    updated_at: at(0),
  };
}

/** A proposed node, the one kind that states its own space. */
function nodeProposal(agent: string, ms: number, nodeId: string, spaceId: string | null) {
  return { ...proposal(agent, ms, "node"), node: node(nodeId, spaceId) };
}

/**
 * One referenced node as `service._node_ref` renders it into `context`.
 *
 * A null space is the shape a node that no longer resolves comes back as: the
 * id alone, with neither a title nor a space.
 */
function ref(id: string, spaceId: string | null) {
  return spaceId === null ? { id } : { id, title: id, space_id: spaceId };
}

/** A proposed edge; the server reports each endpoint's space in `context`. */
function edgeProposal(
  agent: string,
  ms: number,
  src: string,
  dst: string,
  srcSpace: string | null = null,
  dstSpace: string | null = null,
): ProposalOut {
  const base = proposal(agent, ms, "edge");
  return {
    ...base,
    context: { src: ref(src, srcSpace), dst: ref(dst, dstSpace) },
    edge: {
      id: base.id,
      src_id: src,
      dst_id: dst,
      type: "mentions",
      props: {},
      confidence: null,
      created_by: agent,
      state: "proposed",
      valid_from: null,
      valid_to: null,
      created_at: base.created_at,
    },
  };
}

/** A proposed version update; the target node's space rides in `context`. */
function updateProposal(
  agent: string,
  ms: number,
  targetNodeId: string,
  targetSpace: string | null = null,
): ProposalOut {
  const base = proposal(agent, ms, "update");
  const version: VersionOut = {
    id: sequence,
    node_id: targetNodeId,
    title: null,
    content: "",
    props: {},
    actor: agent,
    event_seq: sequence,
    state: "proposed",
    proposed_fields: ["content"],
    created_at: base.created_at,
  };
  return { ...base, version, context: { node: ref(targetNodeId, targetSpace) } };
}

/** One `(agent, space, level)` grant row. */
function grant(agentId: string, spaceId: string, level: string): GrantOut {
  return { agent_id: agentId, space_id: spaceId, level, created_at: at(0) };
}

/** A space as `GET /api/spaces` renders it. */
function space(id: string, title: string, grants: GrantOut[] = []): SpaceOut {
  return { ...node(id, "meta"), title, state: "active", node_count: 1, grants };
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

describe("editGrantedAgents", () => {
  it("names only the agents that write directly", () => {
    // `edit` lands `active`; `read` and `suggest` do not. Only `edit` explains
    // an absence from the review queue.
    const research = space("sp-research", "research", [
      grant("agent:reader", "sp-research", "read"),
      grant("agent:scribe", "sp-research", "edit"),
      grant("agent:scout", "sp-research", "suggest"),
      grant("agent:archivist", "sp-research", "edit"),
    ]);
    expect(editGrantedAgents(research)).toEqual(["agent:archivist", "agent:scribe"]);
  });

  it("is empty for a space nothing is granted on", () => {
    expect(editGrantedAgents(space("sp-a", "a"))).toEqual([]);
  });
});

describe("proposalSpace", () => {
  it("takes a proposed node's own space", () => {
    expect(proposalSpace(nodeProposal("agent:a", 0, "n-1", "sp-a"))).toBe("sp-a");
  });

  it("takes an edge's space from the source endpoint the server reported", () => {
    expect(proposalSpace(edgeProposal("agent:a", 0, "n-src", "n-dst", "sp-a", "sp-b"))).toBe(
      "sp-a",
    );
  });

  it("files a cross-space edge under its source, deliberately", () => {
    // An edge is stored `src → dst` and the assertion originates at the subject.
    // Reviewing it in fact needs `edit` on *both* endpoint spaces, so this is a
    // simplification — pinned here so it is a decision and not a drift.
    expect(proposalSpace(edgeProposal("agent:a", 0, "n-src", "n-dst", "sp-a", "sp-b"))).not.toBe(
      "sp-b",
    );
  });

  it("falls back to an edge's target when the source no longer resolves", () => {
    expect(proposalSpace(edgeProposal("agent:a", 0, "n-src", "n-dst", null, "sp-b"))).toBe("sp-b");
  });

  it("takes an update's space from the node it targets", () => {
    expect(proposalSpace(updateProposal("agent:a", 0, "n-target", "sp-a"))).toBe("sp-a");
  });

  it("returns null only when the referenced node no longer resolves", () => {
    // The one case the server genuinely cannot report: an `undo` took the
    // endpoint's creation back, so the context is an id with nothing on it.
    expect(proposalSpace(edgeProposal("agent:a", 0, "n-1", "n-2"))).toBeNull();
    expect(proposalSpace(updateProposal("agent:a", 0, "n-target"))).toBeNull();
  });
});

describe("groupProposalsBySpace", () => {
  const research = space("sp-research", "research", [
    grant("agent:scout", "sp-research", "suggest"),
  ]);
  const journal = space("sp-journal", "journal", [grant("agent:scribe", "sp-journal", "edit")]);
  const quiet = space("sp-quiet", "quiet", [grant("agent:reader", "sp-quiet", "read")]);
  const spaces = [research, journal, quiet];

  it("returns nothing for an empty queue and no spaces", () => {
    expect(groupProposalsBySpace([], null)).toEqual([]);
  });

  it("groups space first, then agent, then run", () => {
    const sections = groupProposalsBySpace(
      [
        nodeProposal("agent:scout", 0, "n-1", "sp-research"),
        nodeProposal("agent:other", 1_000, "n-2", "sp-research"),
        nodeProposal("agent:scout", 2_000, "n-3", "sp-main"),
      ],
      spaces,
    );
    const queues = sections.filter((section) => section.kind === "queue");
    expect(queues.map((section) => section.spaceId)).toEqual(["sp-research", "sp-main"]);
    expect(queues[0]!.agents.map((agent) => agent.agent)).toEqual([
      "agent:scout",
      "agent:other",
    ]);
    expect(queues[0]!.agents[0]!.batches).toHaveLength(1);
  });

  it("counts per space, which is the number a human governs by", () => {
    const sections = groupProposalsBySpace(
      [
        nodeProposal("agent:a", 0, "n-1", "sp-research"),
        nodeProposal("agent:a", 1_000, "n-2", "sp-research"),
        nodeProposal("agent:a", 2_000, "n-3", "sp-main"),
      ],
      spaces,
    );
    const byId = new Map(sections.map((section) => [section.spaceId, section]));
    expect(byId.get("sp-research")!.total).toBe(2);
    expect(byId.get("sp-research")!.counts).toEqual({ node: 2, edge: 0, update: 0 });
    expect(byId.get("sp-main")!.total).toBe(1);
  });

  it("orders spaces by their oldest waiting proposal, not by volume", () => {
    const sections = groupProposalsBySpace(
      [
        nodeProposal("agent:a", 60_000, "n-1", "sp-loud"),
        nodeProposal("agent:a", 61_000, "n-2", "sp-loud"),
        nodeProposal("agent:a", 62_000, "n-3", "sp-loud"),
        nodeProposal("agent:a", 0, "n-4", "sp-patient"),
      ],
      null,
    );
    expect(sections.map((section) => section.spaceId)).toEqual(["sp-patient", "sp-loud"]);
  });

  it("emits a self-governing section for an edit-granted space with nothing waiting", () => {
    // The decision D4 exists for. Without this the space is absent, and absent
    // is indistinguishable from "nobody has proposed anything here".
    const sections = groupProposalsBySpace([], spaces);
    expect(sections).toHaveLength(1);
    expect(sections[0]!.kind).toBe("self-governing");
    expect(sections[0]!.spaceId).toBe("sp-journal");
    expect(sections[0]!.total).toBe(0);
    expect(sections[0]!.editAgents).toEqual(["agent:scribe"]);
  });

  it("does not call a merely quiet space self-governing", () => {
    // `read` and `suggest` grants produce proposals (or nothing at all); only
    // `edit` explains an absence. Claiming otherwise tells the human a space
    // needs no review when it does.
    const sections = groupProposalsBySpace([], [research, quiet]);
    expect(sections).toEqual([]);
  });

  it("claims nothing about self-governance while the space list is unknown", () => {
    // Null is "the list failed or has not arrived". Emitting sections from an
    // empty list would say every space is ordinary, which is a fact this view
    // does not have.
    expect(groupProposalsBySpace([], null)).toEqual([]);
    const sections = groupProposalsBySpace(
      [nodeProposal("agent:a", 0, "n-1", "sp-research")],
      null,
    );
    expect(sections.every((section) => section.kind !== "self-governing")).toBe(true);
  });

  it("keeps a space out of the self-governing list once it holds a proposal", () => {
    // An `edit` agent files none, so a proposal here came from a *different*
    // agent — the space has a real queue and belongs with the others.
    const sections = groupProposalsBySpace(
      [nodeProposal("agent:scout", 0, "n-1", "sp-journal")],
      spaces,
    );
    expect(sections).toHaveLength(1);
    expect(sections[0]!.kind).toBe("queue");
    // …and the `edit` grant is still reported, because it explains who did not
    // file this.
    expect(sections[0]!.editAgents).toEqual(["agent:scribe"]);
  });

  it("puts the self-governing sections last, after everything actionable", () => {
    const sections = groupProposalsBySpace(
      [nodeProposal("agent:scout", 0, "n-1", "sp-research")],
      spaces,
    );
    expect(sections.map((section) => section.kind)).toEqual(["queue", "self-governing"]);
  });

  it("files a proposal it cannot place into an explicit bucket, never into a guess", () => {
    const sections = groupProposalsBySpace([updateProposal("agent:a", 0, "n-unknown")], spaces);
    const unreported = sections.find((section) => section.kind === "unreported")!;
    expect(unreported.spaceId).toBe(UNREPORTED_SPACE);
    expect(unreported.total).toBe(1);
    // And it is not silently attributed to any real space.
    expect(sections.filter((section) => section.kind === "queue")).toEqual([]);
  });

  it("places every kind from what the server reported, with no lookups", () => {
    // The commonest agent run: a proposed node, the `mentions` edge its
    // `[[wikilink]]` materialised, and an edit to something already there. All
    // three land in one section, and none of it needed a `getNode`.
    const sections = groupProposalsBySpace(
      [
        nodeProposal("agent:a", 0, "n-1", "sp-research"),
        edgeProposal("agent:a", 1_000, "n-1", "n-existing", "sp-research", "sp-research"),
        updateProposal("agent:a", 2_000, "n-existing", "sp-research"),
      ],
      spaces,
    );
    const research_ = sections.find((section) => section.spaceId === "sp-research")!;
    expect(research_.total).toBe(3);
    expect(research_.counts).toEqual({ node: 1, edge: 1, update: 1 });
    expect(sections.some((section) => section.kind === "unreported")).toBe(false);
  });

  it("sends a cross-space edge to its source's section, not to both", () => {
    const sections = groupProposalsBySpace(
      [edgeProposal("agent:a", 0, "n-src", "n-dst", "sp-research", "sp-journal")],
      spaces,
    );
    const queues = sections.filter((section) => section.kind === "queue");
    expect(queues.map((section) => section.spaceId)).toEqual(["sp-research"]);
  });

  it("sorts the unreported bucket among the queues by age, not to the bottom", () => {
    // They are real proposals waiting on a human. Burying them under every
    // named space would hide work behind a presentation choice.
    const sections = groupProposalsBySpace(
      [
        updateProposal("agent:a", 0, "n-unknown"),
        nodeProposal("agent:a", 60_000, "n-1", "sp-research"),
      ],
      spaces,
    );
    expect(sections.map((section) => section.kind)).toEqual([
      "unreported",
      "queue",
      "self-governing",
    ]);
  });

  it("gives every section a distinct, stable key", () => {
    const proposals = [
      nodeProposal("agent:a", 0, "n-1", "sp-research"),
      nodeProposal("agent:a", 1_000, "n-2", "sp-main"),
      updateProposal("agent:a", 2_000, "n-unknown"),
    ];
    const keys = groupProposalsBySpace(proposals, spaces).map((section) => section.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(groupProposalsBySpace(proposals, spaces).map((section) => section.key)).toEqual(keys);
  });

  it("passes the batch gap through to the agent level", () => {
    const proposals = [
      nodeProposal("agent:a", 0, "n-1", "sp-research"),
      nodeProposal("agent:a", 600_000, "n-2", "sp-research"),
    ];
    expect(groupProposalsBySpace(proposals, null, { gapMs: 60_000 })[0]!.agents[0]!.batches)
      .toHaveLength(2);
    expect(groupProposalsBySpace(proposals, null, { gapMs: 900_000 })[0]!.agents[0]!.batches)
      .toHaveLength(1);
  });

  it("never loses a proposal to the regrouping", () => {
    // Whatever the space level does, every waiting item still has to be
    // reachable — a proposal that fell out of the sections would be one no
    // human can accept or reject through this view.
    const proposals = [
      nodeProposal("agent:a", 0, "n-1", "sp-research"),
      nodeProposal("agent:b", 1_000, "n-2", null),
      edgeProposal("agent:a", 2_000, "n-1", "n-2"),
      updateProposal("agent:c", 3_000, "n-unknown"),
    ];
    const sections = groupProposalsBySpace(proposals, spaces);
    const seen = sections.flatMap((section) =>
      section.agents.flatMap((agent) => agent.batches.flatMap((batch) => batch.proposals)),
    );
    expect(seen).toHaveLength(proposals.length);
    expect(new Set(seen.map((item) => item.id)).size).toBe(proposals.length);
    expect(sections.reduce((sum, section) => sum + section.total, 0)).toBe(proposals.length);
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
