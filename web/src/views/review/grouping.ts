/**
 * Grouping the review queue by agent and by batch.
 *
 * `list_proposals` returns a flat list ordered oldest-first, and nothing in the
 * schema carries a batch identifier — `cycle_id` exists on `events` but is
 * always NULL until the Phase-5 consolidation cycle fills it. A batch is
 * therefore **derived here**: proposals by one actor that arrived in a burst
 * are one run of that agent, which is the unit a human actually accepts or
 * rejects. The derivation is deliberately visible in the UI ("grouped by
 * arrival time") so nobody mistakes it for a server-side fact.
 *
 * If a later phase gives proposals a real batch id (`cycle_id` on the event, a
 * run id on the row), replace {@link groupProposals} with a lookup on it and
 * delete the clustering — the rest of the view only consumes the shape below.
 */

import type { ProposalOut } from "../../api/types";
import { timestampMs } from "../../lib";

/** The three things an agent can propose. `ProposalOut.kind` is a bare string. */
export type ProposalKind = "node" | "edge" | "update";

/** Every kind, in the order they are counted and shown. */
export const PROPOSAL_KINDS: readonly ProposalKind[] = ["node", "edge", "update"];

/**
 * The gap that ends a batch.
 *
 * An agent run files its proposals in one pass, seconds apart at most; a gap
 * wider than this is a different run. Two minutes is loose enough to survive a
 * slow embedding pass mid-run and tight enough that yesterday's run never
 * merges into today's.
 */
export const BATCH_GAP_MS = 120_000;

/** How many of each kind a group holds. */
export type KindCounts = Record<ProposalKind, number>;

/** One derived batch: a burst of proposals from a single agent. */
export interface ProposalBatch {
  /** Stable React key: agent plus the batch's first timestamp. */
  key: string;
  /** The proposing actor, verbatim (`agent:researcher`, …). */
  agent: string;
  /** Proposals in arrival order. */
  proposals: ProposalOut[];
  /** Timestamp of the first proposal in the batch. */
  startedAt: string;
  /** Timestamp of the last proposal in the batch. */
  endedAt: string;
  counts: KindCounts;
}

/** One agent's whole share of the queue, split into batches. */
export interface AgentGroup {
  agent: string;
  batches: ProposalBatch[];
  total: number;
  counts: KindCounts;
  /** Timestamp of this agent's oldest waiting proposal. */
  oldestAt: string;
}

/** Narrow `ProposalOut.kind` (typed as `string`) to the three known kinds. */
export function proposalKind(proposal: ProposalOut): ProposalKind | null {
  return PROPOSAL_KINDS.includes(proposal.kind as ProposalKind)
    ? (proposal.kind as ProposalKind)
    : null;
}

/** An empty counter, for accumulating into. */
function emptyCounts(): KindCounts {
  return { node: 0, edge: 0, update: 0 };
}

/** Add one proposal to a counter, ignoring a kind this build does not know. */
function count(counts: KindCounts, proposal: ProposalOut): void {
  const kind = proposalKind(proposal);
  if (kind !== null) counts[kind] += 1;
}

/**
 * Group a flat queue into agents, each split into arrival-time batches.
 *
 * Agents are ordered by their oldest waiting proposal, so the thing that has
 * been waiting longest is the thing at the top of the page. Within an agent,
 * batches are newest-first: a run that just landed is the one a reviewer came
 * to look at.
 *
 * @param proposals The queue as the server returned it (oldest first).
 * @param gapMs Gap that ends a batch; defaults to {@link BATCH_GAP_MS}.
 */
export function groupProposals(
  proposals: readonly ProposalOut[],
  gapMs: number = BATCH_GAP_MS,
): AgentGroup[] {
  const byAgent = new Map<string, ProposalOut[]>();
  for (const proposal of proposals) {
    const bucket = byAgent.get(proposal.created_by);
    if (bucket) bucket.push(proposal);
    else byAgent.set(proposal.created_by, [proposal]);
  }

  const groups: AgentGroup[] = [];
  for (const [agent, agentProposals] of byAgent) {
    const ordered = [...agentProposals].sort(
      (a, b) => (timestampMs(a.created_at) ?? 0) - (timestampMs(b.created_at) ?? 0),
    );

    const batches: ProposalBatch[] = [];
    let current: ProposalOut[] = [];
    let previousMs: number | null = null;

    const flush = () => {
      if (current.length === 0) return;
      const first = current[0];
      const last = current[current.length - 1];
      if (!first || !last) return;
      const counts = emptyCounts();
      for (const proposal of current) count(counts, proposal);
      batches.push({
        key: `${agent}@${first.created_at}#${first.id}`,
        agent,
        proposals: current,
        startedAt: first.created_at,
        endedAt: last.created_at,
        counts,
      });
      current = [];
    };

    for (const proposal of ordered) {
      const ms = timestampMs(proposal.created_at);
      if (previousMs !== null && ms !== null && ms - previousMs > gapMs) flush();
      current.push(proposal);
      if (ms !== null) previousMs = ms;
    }
    flush();

    const counts = emptyCounts();
    for (const proposal of ordered) count(counts, proposal);
    const oldest = ordered[0];
    groups.push({
      agent,
      // Newest run first: it is what a reviewer just came to look at.
      batches: batches.reverse(),
      total: ordered.length,
      counts,
      oldestAt: oldest ? oldest.created_at : "",
    });
  }

  groups.sort((a, b) => (timestampMs(a.oldestAt) ?? 0) - (timestampMs(b.oldestAt) ?? 0));
  return groups;
}

/** Render a counter as "3 nodes · 12 edges", skipping the zeroes. */
export function describeCounts(counts: KindCounts): string {
  const parts: string[] = [];
  for (const kind of PROPOSAL_KINDS) {
    const value = counts[kind];
    if (value === 0) continue;
    const noun = kind === "update" ? "version update" : kind;
    parts.push(`${value} ${noun}${value === 1 ? "" : "s"}`);
  }
  return parts.join(" · ");
}
