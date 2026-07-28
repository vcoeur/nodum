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
 * Not for tidiness. **An `edit` grant is the only thing that can keep a space's
 * writes out of this queue**, because `edit` is the level that lands `active`
 * rather than `proposed`. Group by agent alone and such a space can be entirely
 * absent, and the human cannot tell "nothing has been proposed here" from
 * "everything written here lands without review". A section naming the agents
 * that hold `edit` is the only place that fact can surface, so
 * {@link groupProposalsBySpace} emits one for every edit-granted space with an
 * empty queue, and the emptiness is the point rather than a reason to leave it
 * out.
 *
 * **What that section may not say is that such an agent never appears here.**
 * Phase 5a's landing seam (`Store.cap_landing`, §8.3) made a grant a *ceiling
 * rather than a mandate*: a writer may file **below** its own grant, and the
 * consolidation runner does exactly that for every inference it makes — the
 * gardener holds `edit` on `main` and files every suggested edge `proposed`
 * anyway, because the inferences are the uncertain half by construction. So the
 * queue can and does hold work from an `edit`-granted agent, right under a
 * section header that used to claim otherwise. What an `edit` grant states is a
 * *permission*: those writes **may** land active without review.
 *
 * ## Where a proposal's space comes from
 *
 * The server states it, for every kind. A **node** proposal carries its own
 * (`node.space_id`); an **edge** and an **update** carry theirs in the reviewer
 * context, where `service._node_ref` puts a `space_id` on every referenced node
 * beside its id and title. Nothing is looked up from here.
 *
 * The *space not reported* section survives that, and is not vestigial: a
 * referenced node that no longer resolves — `undo` took its creation back after
 * an edge to it was proposed — comes back as an id with no title and no space,
 * and there is genuinely nothing to file it under. It is a section that should
 * now be empty on a healthy graph, which is exactly why it must stay honest
 * rather than be deleted.
 */

import type { ProposalOut, SpaceOut } from "../../api/types";
import { timestampMs } from "../../lib";
import { contextRef } from "./proposalText";

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

/**
 * The bucket for proposals whose space the queue does not report.
 *
 * Reachable only through a referenced node that no longer resolves, so on a
 * healthy graph it stays empty — see the module docblock.
 */
export const UNREPORTED_SPACE = "";

/** Why a space section is on screen. */
export type SpaceSectionKind =
  /** It holds proposals. */
  | "queue"
  /** It holds proposals whose referenced node no longer resolves. */
  | "unreported"
  /**
   * It holds none, and an agent on it holds `edit` — so its writes *may* land
   * `active` without passing through here. Not a promise that they always do:
   * a writer may file below its own grant (§8.3), and the gardener does.
   */
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
   * How many of these proposals are edges that leave this space.
   *
   * Filed here under their source (see {@link edgeCrossing}), so the count is
   * what lets the header state the simplification instead of embodying it.
   */
  crossings: number;
  /**
   * Agents holding `edit` here — their writes *may* land `active` without
   * reaching this queue. Populated on a queue section too, and the section may
   * well hold their proposals: a grant is a ceiling, so an `edit` agent that
   * files below it appears here like any other.
   */
  editAgents: string[];
}

/**
 * The agents allowed to write directly into a space.
 *
 * `edit` is the level that *can* land `active` rather than `proposed`. It is a
 * ceiling and not a mandate — `Store.cap_landing` lets a writer file below its
 * own grant, and the consolidation runner files every inference `proposed`
 * whatever the gardener holds — so this answers "who may write here without
 * review", never "whose work is absent below".
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
 * What a section header says about the agents holding `edit` on its space.
 *
 * A sentence rather than a component, because it is the sentence that was
 * **wrong**: it read *"builtin-gardener hold `edit` here and write directly —
 * nothing below was filed by it"*, and was printed immediately above a section
 * headed `agent:builtin-gardener` holding four of the gardener's own edges. Two
 * faults in one line: an inference the landing seam invalidated, and a plural
 * verb on a single agent.
 *
 * @param editAgents Agent ids holding `edit` here, from {@link editGrantedAgents}.
 * @param hasProposals Whether this section is currently holding any — a header
 *   over a queue has to account for the work that is visibly there.
 * @returns The sentence, or null when nobody holds `edit` and there is nothing
 *   to say.
 */
export function editGrantNote(
  editAgents: readonly string[],
  hasProposals: boolean,
): string | null {
  if (editAgents.length === 0) return null;
  const who = editAgents.join(", ");
  const holds = editAgents.length === 1 ? "holds" : "hold";
  const their = editAgents.length === 1 ? "its" : "their";
  const permission =
    `${who} ${holds} edit here, so ${their} writes may land active without passing through ` +
    "this queue.";
  // A grant is a ceiling, not a mandate: a writer may file below it, and the
  // gardener files every inference as a proposal however it is granted. So the
  // header states the permission and then says what it does *not* imply.
  const ceiling = hasProposals
    ? "A grant is a ceiling rather than a mandate, though — a writer may file below its own " +
      "grant, and some of what is waiting here may be exactly that."
    : "A grant is a ceiling rather than a mandate, though: an agent may still file a proposal " +
      "below its own grant, and the gardener does that for every link it infers.";
  return `${permission} ${ceiling}`;
}

/**
 * The intro over the sections that hold nothing but name an `edit` grant.
 *
 * Its old wording — *"Those writes land `active` immediately, so nothing from
 * them ever reaches this queue"* — is the same false inference
 * {@link editGrantNote} exists to correct, one level up.
 *
 * @param spaceCount How many such sections there are.
 */
export function selfGoverningNote(spaceCount: number): string {
  const spaces = `${spaceCount} space${spaceCount === 1 ? "" : "s"}`;
  return (
    `${spaces} where an agent holds edit. Those writes may land active immediately, so work ` +
    "done there can bypass this queue entirely — the zero below is that permission and not an " +
    "empty inbox, and it is not a claim that nothing has been written there. Nor is it a " +
    "promise: a grant is a ceiling, so an agent may still file a proposal below it, and one " +
    "that does appears in the queue above like any other."
  );
}

/**
 * Which space a proposal belongs to, or null when the server did not say.
 *
 * - A **node** states its own on the row, always.
 * - An **update** takes the space of the node it targets, off `context.node`.
 * - An **edge** takes its **source**'s space, off `context.src`, and its
 *   target's only when the source no longer resolves. An edge is stored as
 *   `src → dst` and the assertion originates at the subject; note that
 *   reviewing a cross-space edge in fact needs authority on *both* endpoint
 *   spaces (`Store.edge_landing_state`), so filing it under one is a
 *   simplification the section header must not pretend otherwise about —
 *   {@link edgeCrossing} and {@link SpaceSection.crossings} are what stop it
 *   pretending.
 *
 * Null means the referenced node did not come back — undone, or otherwise gone
 * — which is the whole of what {@link UNREPORTED_SPACE} now covers.
 *
 * @param proposal One queue entry.
 * @returns The space id, or null when the queue reports none.
 */
export function proposalSpace(proposal: ProposalOut): string | null {
  if (proposal.node) return proposal.node.space_id;
  if (proposal.edge) {
    return (
      contextRef(proposal.context, "src")?.spaceId ??
      contextRef(proposal.context, "dst")?.spaceId ??
      null
    );
  }
  if (proposal.version) return contextRef(proposal.context, "node")?.spaceId ?? null;
  return null;
}

/** An edge proposal whose two endpoints live in different spaces. */
export interface EdgeCrossing {
  /** The **source**'s space — the section this proposal is filed under. */
  from: string;
  /** The **target**'s space, which the filing says nothing about. */
  to: string;
}

/**
 * The crossing an edge proposal makes, or null when it makes none.
 *
 * A cross-space edge is filed under its source's space alone (see
 * {@link proposalSpace}), while accepting it in fact needs `edit` on **both**
 * endpoint spaces — `Store.edge_landing_state` is what decides where such an
 * edge lands. That gap is a deliberate simplification of the grouping, and
 * this is what keeps it from being a silent one: a queue that files a crossing
 * under one space and then says nothing about the second is asserting, by
 * omission, that reviewing it is a single-space act.
 *
 * Null for anything that is not a crossing we can see: a node or update
 * proposal, an edge inside one space, and — deliberately — an edge with an
 * endpoint the server could not resolve. An unresolved endpoint reports no
 * space at all, and "no space reported" is not evidence of a different one.
 *
 * @param proposal One queue entry.
 * @returns Both endpoint spaces when they differ, else null.
 */
export function edgeCrossing(proposal: ProposalOut): EdgeCrossing | null {
  if (!proposal.edge) return null;
  const from = contextRef(proposal.context, "src")?.spaceId ?? null;
  const to = contextRef(proposal.context, "dst")?.spaceId ?? null;
  if (from === null || to === null || from === to) return null;
  return { from, to };
}

/** Options for {@link groupProposalsBySpace}. */
export interface SpaceGroupingOptions {
  /** Gap that ends a batch; defaults to {@link BATCH_GAP_MS}. */
  gapMs?: number;
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
 * that an `edit`-granted space's silence here is a *permission* rather than
 * chance — work done there may land without review — which is the whole of D4.
 * What they may not say is that such a space can never appear above: a grant is
 * a ceiling, and a writer that files below it queues up like any other. They are
 * emitted only when the space list is known: with `spaces` null (still loading,
 * or the request failed) the view has no way to tell an edit-granted space from
 * any other, and inventing the distinction would be worse than admitting it is
 * unknown.
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
  const buckets = new Map<string, ProposalOut[]>();
  for (const proposal of proposals) {
    const spaceId = proposalSpace(proposal) ?? UNREPORTED_SPACE;
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
      crossings: bucket.filter((proposal) => edgeCrossing(proposal) !== null).length,
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
      crossings: 0,
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
