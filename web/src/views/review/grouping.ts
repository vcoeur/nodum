/**
 * Grouping the review queue: **space, then agent, then batch** (design decision
 * D4).
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
 *
 * ## Why space is the outer level
 *
 * Not for tidiness. **An `edit`-granted space never reaches this queue at
 * all** — its agents write `active` directly, so they file no proposals. Group
 * by agent alone and that territory becomes invisible: the human sees nothing
 * and cannot tell "nothing has been proposed here" from "this space governs
 * itself". A section that says *self-governing — no review* is the only place
 * that fact can surface, so {@link groupProposalsBySpace} emits one for every
 * edit-granted space with an empty queue, and the emptiness is the point rather
 * than a reason to leave it out.
 *
 * ## Where a proposal's space comes from — and where it does not
 *
 * A **node** proposal carries its own (`node.space_id`). An **edge** and an
 * **update** do not: `service._edge_context` and `service._update_context`
 * build the reviewer context out of endpoint and target *id and title only*, so
 * the wire says nothing about their space. {@link proposalSpace} therefore
 * resolves those against an index of node → space that the caller supplies,
 * and a proposal it cannot place goes to an explicit *space not reported*
 * section rather than being filed under a guess. The right fix is one field on
 * each of those two context builders; until then, misfiling a proposal into a
 * space it does not belong to would be worse than admitting the gap.
 */

import type { ProposalOut, SpaceOut } from "../../api/types";
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

/* ------------------------------------------------------------------ */
/* Spaces: the outer grouping level (D4)                                */
/* ------------------------------------------------------------------ */

/** The bucket for proposals whose space the queue does not report. */
export const UNREPORTED_SPACE = "";

/** Why a space section is on screen. */
export type SpaceSectionKind =
  /** It holds proposals. */
  | "queue"
  /** It holds proposals whose space could not be determined. */
  | "unreported"
  /** It holds none, and never will: every agent on it writes `active` directly. */
  | "self-governing";

/** One space's whole share of the queue, or its documented absence from it. */
export interface SpaceSection {
  /** Stable React key. */
  key: string;
  /** The space id, or {@link UNREPORTED_SPACE} for the unplaceable bucket. */
  spaceId: string;
  kind: SpaceSectionKind;
  /** Agents with proposals here, longest-waiting first. Empty when self-governing. */
  agents: AgentGroup[];
  /** Proposals waiting in this space. */
  total: number;
  counts: KindCounts;
  /** This space's oldest waiting proposal; `""` when there are none. */
  oldestAt: string;
  /**
   * Agents holding `edit` here — they land writes `active`, so nothing they do
   * reaches this queue. Populated on a queue section too: a space with both an
   * `edit` agent and waiting proposals means some *other* agent holds only
   * `suggest`, which is worth seeing rather than inferring.
   */
  editAgents: string[];
}

/**
 * The agents that write directly into a space.
 *
 * `edit` is the level that lands `active` rather than `proposed`, so these are
 * exactly the agents whose work never appears in the review queue.
 *
 * @param space One row of `GET /api/spaces`.
 * @returns Agent ids holding `edit`, sorted.
 */
export function editGrantedAgents(space: SpaceOut): string[] {
  return space.grants
    .filter((grant) => grant.level === "edit")
    .map((grant) => grant.agent_id)
    .sort();
}

/**
 * Node ids a proposal's space would have to be read off, but which the proposal
 * itself does not carry.
 *
 * Only edges and updates produce any: a proposed node states its own space.
 * The caller resolves these however it can and hands the answers back through
 * {@link groupProposalsBySpace}'s `nodeSpaces`.
 *
 * @param proposals The queue.
 * @returns Distinct node ids, in first-seen order.
 */
export function referencedNodeIds(proposals: readonly ProposalOut[]): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  const add = (id: string | null | undefined) => {
    if (!id || seen.has(id)) return;
    seen.add(id);
    ids.push(id);
  };
  for (const proposal of proposals) {
    if (proposal.edge) {
      add(proposal.edge.src_id);
      add(proposal.edge.dst_id);
    }
    if (proposal.version) add(proposal.version.node_id);
  }
  return ids;
}

/**
 * Node → space for every node the queue itself states one for.
 *
 * Free, and it covers the commonest case on its own: an agent run that proposes
 * nodes and the `mentions` edges its `[[wikilinks]]` materialised files those
 * edges from a node that is right here in the queue.
 *
 * @param proposals The queue.
 * @returns Space id per node id, for the nodes the queue carries.
 */
export function spacesFromProposals(proposals: readonly ProposalOut[]): Map<string, string> {
  const index = new Map<string, string>();
  for (const proposal of proposals) {
    if (proposal.node?.space_id) index.set(proposal.node.id, proposal.node.space_id);
  }
  return index;
}

/**
 * Which space a proposal belongs to, or null when nothing says.
 *
 * - A **node** states its own, always.
 * - An **update** takes the space of the node it targets.
 * - An **edge** takes its **source**'s space, and its target's only when the
 *   source is unknown. An edge is stored as `src → dst` and the assertion
 *   originates at the subject; note that reviewing a cross-space edge in fact
 *   needs authority on *both* endpoint spaces (`Store.edge_landing_state`), so
 *   filing it under one is a simplification the section header should not
 *   pretend otherwise about.
 *
 * @param proposal One queue entry.
 * @param nodeSpaces Space id per node id, for the endpoints and targets the
 *   proposal itself does not carry.
 * @returns The space id, or null when it cannot be determined.
 */
export function proposalSpace(
  proposal: ProposalOut,
  nodeSpaces: ReadonlyMap<string, string> = new Map(),
): string | null {
  if (proposal.node) return proposal.node.space_id;
  if (proposal.edge) {
    return (
      nodeSpaces.get(proposal.edge.src_id) ?? nodeSpaces.get(proposal.edge.dst_id) ?? null
    );
  }
  if (proposal.version) return nodeSpaces.get(proposal.version.node_id) ?? null;
  return null;
}

/** Options for {@link groupProposalsBySpace}. */
export interface SpaceGroupingOptions {
  /** Gap that ends a batch; defaults to {@link BATCH_GAP_MS}. */
  gapMs?: number;
  /**
   * Space id per node id, for the edge endpoints and update targets the queue
   * does not carry itself. Merged under what the queue does state.
   */
  nodeSpaces?: ReadonlyMap<string, string>;
}

/**
 * Group the queue into space sections, each split into agents and their runs.
 *
 * Sections holding proposals come first, ordered by their oldest waiting
 * proposal — the queue is a waiting list and the thing that has waited longest
 * belongs at the top, which is the same rule agents are ordered by one level
 * down. The *space not reported* bucket sorts among them for the same reason:
 * it holds real proposals, and burying it under every named space would hide
 * work rather than explain it.
 *
 * **Self-governing sections come last and hold nothing.** They exist to say
 * that an `edit`-granted space is absent from this queue *by design* rather
 * than by chance, which is the whole of D4. They are emitted only when the
 * space list is known: with `spaces` null (still loading, or the request
 * failed) the view has no way to tell a self-governing space from any other,
 * and inventing the distinction would be worse than admitting it is unknown.
 *
 * A space that is merely empty — no proposals, no `edit` grant — gets no
 * section. Nothing has been proposed there and nothing governs it, so there is
 * no fact to state; emitting one per space would bury the queue under the file.
 *
 * @param proposals The queue as the server returned it (oldest first).
 * @param spaces Every active space, or null when the list is unknown.
 * @param options See {@link SpaceGroupingOptions}.
 */
export function groupProposalsBySpace(
  proposals: readonly ProposalOut[],
  spaces: readonly SpaceOut[] | null,
  options: SpaceGroupingOptions = {},
): SpaceSection[] {
  const index = spacesFromProposals(proposals);
  for (const [nodeId, spaceId] of options.nodeSpaces ?? []) {
    if (!index.has(nodeId)) index.set(nodeId, spaceId);
  }

  const buckets = new Map<string, ProposalOut[]>();
  for (const proposal of proposals) {
    const spaceId = proposalSpace(proposal, index) ?? UNREPORTED_SPACE;
    const bucket = buckets.get(spaceId);
    if (bucket) bucket.push(proposal);
    else buckets.set(spaceId, [proposal]);
  }

  const grantsBySpace = new Map<string, string[]>();
  for (const space of spaces ?? []) grantsBySpace.set(space.id, editGrantedAgents(space));

  const sections: SpaceSection[] = [];
  for (const [spaceId, bucket] of buckets) {
    const agents = groupProposals(bucket, options.gapMs);
    const counts = emptyCounts();
    for (const proposal of bucket) count(counts, proposal);
    sections.push({
      key: spaceId || "(unreported)",
      spaceId,
      kind: spaceId === UNREPORTED_SPACE ? "unreported" : "queue",
      agents,
      total: bucket.length,
      counts,
      oldestAt: agents[0]?.oldestAt ?? "",
      editAgents: grantsBySpace.get(spaceId) ?? [],
    });
  }
  sections.sort((a, b) => (timestampMs(a.oldestAt) ?? 0) - (timestampMs(b.oldestAt) ?? 0));

  const selfGoverning: SpaceSection[] = [];
  for (const space of spaces ?? []) {
    if (buckets.has(space.id)) continue;
    const editAgents = grantsBySpace.get(space.id) ?? [];
    if (editAgents.length === 0) continue;
    selfGoverning.push({
      key: space.id,
      spaceId: space.id,
      kind: "self-governing",
      agents: [],
      total: 0,
      counts: emptyCounts(),
      oldestAt: "",
      editAgents,
    });
  }
  selfGoverning.sort((a, b) => a.spaceId.localeCompare(b.spaceId));

  return [...sections, ...selfGoverning];
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
