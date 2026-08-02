/**
 * The dream journal's derivations (`journal.ts`).
 *
 * What is asserted here is the honesty of the reading, because every failure
 * mode of this view is a sentence that is subtly untrue about a record the
 * human is about to act on:
 *
 * - a **rehearsal** writes no graph event, so its `proposed`/`applied` lists are
 *   empty and a naive read reports "found nothing to change" about a run that
 *   would have proposed fourteen links;
 * - a **truncated** event list must not read as a complete one, since the
 *   reader is deciding whether to reverse "everything below";
 * - a `{}` **metrics** object is a real answer (a rollback computes none), not
 *   missing data to paper over with dashes — and it is *three* real answers,
 *   which are not interchangeable;
 * - a **conflict** has to name which row, what the cycle did to it and what has
 *   changed it since — decision C4's whole argument is that "rollback failed"
 *   leaves a human with nothing to do;
 * - a **recorded failure** is a string the server wrote, and the first one a
 *   scoped cycle actually produces is `open_cycle`'s own *"TypeNotFound: unknown
 *   space: 909a…"*. Splicing it into the headline broke the frontend contract's
 *   one unconditional rule in exactly the named shape, so `FORBIDDEN` below runs
 *   over **every string `cycleWork` can produce**, branch by branch.
 *
 * ## The fixtures are the server's shapes, not convenient ones
 *
 * `cycles.scope` is the **resolved space id** — `open_cycle` runs the reference
 * through `_resolve_space` before the row is written — so a fixture saying
 * `scope: "research"` asserts a shape the server cannot produce, and a view that
 * printed 32 hex characters on every row shipped green underneath it. Every
 * space reference, node id, edge id and cycle id below is the 32-hex string the
 * server really writes. *A fixture that cannot reach the branch is not coverage
 * of it*, and this file has now paid for that lesson once.
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import { describeError } from "../../lib";
import type { SpaceName } from "../../components";
import type {
  CycleMetrics,
  CycleOut,
  EventOut,
  JsonObject,
  NodeOut,
  RollbackBlockerOut,
  RollbackConflictOut,
  RollbackOut,
} from "../../api/types";
import {
  ABANDON_ACTION_LABEL,
  ABANDON_CONFIRM,
  abandonAvailability,
  abandonOutcome,
  actorLabel,
  cycleCaveats,
  cycleFailures,
  cycleProvenance,
  cycleScopeIds,
  cycleScopeSpaces,
  cycleWork,
  describeBlocker,
  describeConflict,
  describeEvent,
  describeRecordedFailure,
  describeRunFailure,
  emptyEventsNote,
  endpointLabel,
  eventWindow,
  eventWindowNote,
  MAX_NAMED_DEPENDANTS,
  metricRows,
  nameIdsIn,
  noMetricsNote,
  readAcceptance,
  readConsolidationReport,
  readOperationReport,
  referencedNodeIds,
  rollbackAvailability,
  rollbackOutcome,
  rollbackPlan,
  rollbackRefusal,
  rowHeadlines,
  RUNNING_ACTIONS_HINT,
  SCOPE_CONTROL_HINT,
  shortId,
  STOP_ACTION_HINT,
  STOP_ACTION_LABEL,
  STOP_CONFIRM,
  STOP_IS_NOTICED_AT_A_MODEL_CALL,
  stopAvailability,
  stopOutcome,
  stopRecord,
  verdictNodeIds,
} from "./journal";

/* ------------------------------------------------------------------ */
/* Ids, in the shapes the server writes them                            */
/* ------------------------------------------------------------------ */

/** `uuid4().hex` — what `cycles.id`, `nodes.id` and `edges.id` all look like. */
const CYCLE_ID = "6b1f0f2c9a4d4f0e8c7b5a3d2e1f0a9b";
const ROLLBACK_CYCLE_ID = "0abcdef1234556780abcdef123455678";
/** A user-created space's id — what `cycles.scope` carries, never its name. */
const RESEARCH_ID = "f73944650d5c4255a0aa5421308f62b0";
const EDGE_ID = "e1a2b3c4d5e6f708192a3b4c5d6e7f80";
const NODE_ID = "a1a2b3c4d5e6f708192a3b4c5d6e7f80";
const SRC_ID = "4f2ad9c1b0e34a75b6c8d9e0f1a2b3c4";
const DST_ID = "91bc7e2d5a084c31a2b3c4d5e6f70819";

/** Anything that is 32 hex characters is an id nobody should be reading. */
const BARE_ID = /[0-9a-f]{32}/;

/** `research`, resolved the way a view resolves it before writing a sentence. */
const RESEARCH: SpaceName = { label: "research", kind: "active" };

/* ------------------------------------------------------------------ */
/* Fixtures                                                             */
/* ------------------------------------------------------------------ */

/** A cycle row as `GET /api/cycles` sends it. */
function cycle(overrides: Partial<CycleOut> = {}): CycleOut {
  return {
    id: CYCLE_ID,
    trigger: "scheduled",
    triggered_by: "scheduler",
    scope: null,
    dry_run: false,
    status: "completed",
    report: null,
    started_at: "2026-07-27 02:00:00",
    finished_at: "2026-07-27 02:00:12",
    rolled_back_by: null,
    stop_requested: false,
    stop_requested_by: null,
    stop_requested_at: null,
    ...overrides,
  };
}

/** What `cycles.stop_requested_at` looks like: SQLite's UTC, with no zone marker. */
const STOPPED_AT = "2026-07-27 02:00:07";

/** One job outcome as the runner dumps it into `report.jobs`. */
function job(name: string, overrides: JsonObject = {}): JsonObject {
  return {
    name,
    examined: 0,
    proposed: [],
    applied: [],
    skipped: [],
    notes: [],
    detail: {},
    truncated: false,
    error: null,
    ...overrides,
  };
}

/** `n` fabricated ids, for a `proposed`/`applied` list whose length is the point. */
function ids(count: number): string[] {
  return Array.from({ length: count }, (_unused, index) => `id-${index}`);
}

/** A consolidation report as `cycles.report` carries it. */
function report(jobs: JsonObject[], overrides: JsonObject = {}): JsonObject {
  return { scope: null, dry_run: false, jobs, metrics: {}, failed: [], ...overrides };
}

/** A night's work: three duplicates flagged, fourteen links proposed, two edges pruned. */
const A_NIGHT = [
  job("duplicate_candidates", { proposed: ids(3), detail: { matched: 3 } }),
  job("link_maintenance", {
    proposed: ids(14),
    applied: ids(2),
    detail: { inferred: 14, prunable: 2 },
  }),
  job("housekeeping"),
  job("neglect_report", { detail: { neglected_count: 0, threshold_days: 90 } }),
];

/** An event as `list_events(cycle_id=…)` sends it. */
function event(overrides: Partial<EventOut> = {}): EventOut {
  return {
    seq: 41,
    actor: "agent:builtin-gardener",
    op: "edge.propose",
    payload: {},
    cycle_id: CYCLE_ID,
    created_at: "2026-07-27 02:00:03",
    ...overrides,
  };
}

/** An edge row as an event payload carries it — the raw table row. */
function edgeRow(overrides: JsonObject = {}): JsonObject {
  return {
    id: EDGE_ID,
    src_id: SRC_ID,
    dst_id: DST_ID,
    type_id: "relates_to",
    props: '{"job": "link_maintenance"}',
    confidence: 0.84,
    created_by: "agent:builtin-gardener",
    state: "proposed",
    valid_from: null,
    valid_to: null,
    created_at: "2026-07-27 02:00:03",
    ...overrides,
  };
}

/** A node row as an event payload carries it. */
function nodeRow(overrides: JsonObject = {}): JsonObject {
  return {
    id: NODE_ID,
    space_id: "main",
    type_id: "note",
    parent_id: null,
    position: 3.0,
    title: "Kafka Streams",
    content: "the original body",
    props: "{}",
    state: "active",
    created_by: "human:alice",
    created_at: "2026-07-01 09:00:00",
    updated_at: "2026-07-27 02:00:05",
    ...overrides,
  };
}

/** A rollback response, clean unless conflicts or blockers are given. */
function rollback(overrides: Partial<RollbackOut> = {}): RollbackOut {
  return {
    cycle_id: CYCLE_ID,
    rollback_cycle_id: null,
    dry_run: true,
    reversed_events: [],
    skipped_events: [],
    restored_nodes: [],
    restored_edges: [],
    deleted_nodes: [],
    deleted_edges: [],
    redirects_removed: [],
    conflicts: [],
    blockers: [],
    ...overrides,
  };
}

/** One conflict row out of a dry run or a 409 body. */
function conflict(overrides: Partial<RollbackConflictOut> = {}): RollbackConflictOut {
  return {
    kind: "edge",
    row_id: EDGE_ID,
    cycle_event_seq: 42,
    cycle_event_op: "edge.propose",
    conflicting_seq: 57,
    conflicting_op: "edge.accept",
    conflicting_actor: "human:alice",
    conflicting_cycle_id: null,
    ...overrides,
  };
}

/**
 * `count` distinct 32-hex ids, for a dependant list whose length is the point.
 *
 * Real id shapes rather than `dep-0`, because what these fixtures exercise is
 * the shortening and the rule against putting 32 hex characters on a screen,
 * and a short fake id would assert neither of them.
 */
function hexIds(count: number): string[] {
  return Array.from(
    { length: count },
    (_unused, index) => `c${index}a2b3c4d5e6f708192a3b4c5d6e7f8${index}`,
  );
}

/** `service._named_rows`: the first five ids, then a count of what is left. */
function namedRows(rowIds: readonly string[]): string {
  const shown = rowIds.slice(0, 5).join(", ");
  const rest = rowIds.length - 5;
  return rest > 0 ? `${shown} and ${rest} more` : shown;
}

/**
 * One blocker row out of a dry run: a node the cycle created that is now a
 * parent, which is `service._delete_blocker`'s first branch.
 *
 * `reason` is built the way the service builds it — the guard's own sentence,
 * bare ids and all — because the whole question this module answers is what a
 * dialog may do with a string the server wrote.
 */
function blocker(overrides: Partial<RollbackBlockerOut> = {}): RollbackBlockerOut {
  const dependants = overrides.dependants ?? hexIds(2);
  const rowId = overrides.row_id ?? NODE_ID;
  return {
    kind: "node",
    cycle_event_seq: 43,
    cycle_event_op: "node.create",
    reason:
      `node ${rowId} still has ${dependants.length} child node(s) (${namedRows(dependants)}) — ` +
      "take those back first: undo their creation, or roll back the cycle that made them",
    ...overrides,
    row_id: rowId,
    dependants,
  };
}

/** A space node as `GET /api/spaces` sends it, minus the two space-only fields. */
function spaceNode(id: string, title: string): NodeOut {
  return {
    id,
    space_id: "meta",
    type: "space",
    parent_id: null,
    position: 1,
    title,
    content: "",
    props: {},
    state: "active",
    created_by: "human:alice",
    created_at: "2026-07-01 09:00:00",
    updated_at: "2026-07-01 09:00:00",
  };
}

describe("readConsolidationReport", () => {
  it("reads the runner's report", () => {
    // `report.scope` is `cycle.scope`, which `open_cycle` resolved to an id.
    const parsed = readConsolidationReport(report(A_NIGHT, { scope: RESEARCH_ID }));
    expect(parsed?.scope).toBe(RESEARCH_ID);
    expect(parsed?.jobs.map((entry) => entry.name)).toEqual([
      "duplicate_candidates",
      "link_maintenance",
      "housekeeping",
      "neglect_report",
    ]);
    expect(parsed?.jobs[1]?.proposed).toBe(14);
    expect(parsed?.jobs[1]?.applied).toBe(2);
  });

  it("is null for a report that is not the runner's", () => {
    // A rollback and a one-op curative cycle both write `{"op": …}`, which is
    // exactly why `CycleDetailOut.metrics` is `{}` for them.
    expect(
      readConsolidationReport({ op: "rollback_cycle", rolled_back: ROLLBACK_CYCLE_ID }),
    ).toBeNull();
    expect(readConsolidationReport(null)).toBeNull();
  });

  it("survives a job object missing every field", () => {
    // The wire type is `dict | None`; a render that threw here would take the
    // whole journal down over one unfamiliar key.
    const parsed = readConsolidationReport({ jobs: [{}, null, "not a job"] });
    expect(parsed?.jobs).toHaveLength(1);
    expect(parsed?.jobs[0]).toMatchObject({ name: "unnamed job", proposed: 0, applied: 0 });
  });

  it("names the jobs that raised, with what came out of them", () => {
    const parsed = readConsolidationReport(
      report(A_NIGHT, { failed: [{ job: "link_maintenance", error: "boom" }] }),
    );
    expect(parsed?.failed).toEqual([{ job: "link_maintenance", error: "boom" }]);
  });
});

describe("readAcceptance", () => {
  it("reads the curation job's per-(proposer, type) rates", () => {
    const detail: JsonObject = {
      acceptance: [
        { proposer: "agent:researcher", kind: "edge", type: "supports", accepted: 2, rejected: 1, rate: 0.666667 },
      ],
    };
    expect(readAcceptance(detail)).toEqual([
      { proposer: "agent:researcher", kind: "edge", type: "supports", accepted: 2, rejected: 1, rate: 0.666667 },
    ]);
  });

  it("is empty for a job that carried no acceptance list", () => {
    // A cycle that ran no curation job — or one whose scan found no history —
    // has no `acceptance` key at all, and a malformed row must be dropped
    // rather than thrown, exactly as the rest of this untyped wire is read.
    expect(readAcceptance({})).toEqual([]);
    expect(readAcceptance({ acceptance: [{ proposer: "agent:researcher", rate: "half" }, null] })).toEqual(
      [],
    );
    expect(readAcceptance(null)).toEqual([]);
  });
});

describe("readOperationReport", () => {
  it("reads a one-op report by its name", () => {
    const parsed = readOperationReport({ op: "rollback_cycle", rolled_back: "abc", reversed: 3 });
    expect(parsed).toMatchObject({ op: "rollback_cycle", rolledBack: "abc", reversed: 3 });
    expect(parsed?.abandoned).toBe(false);
  });

  it("reads an abandoned run by its own discriminator, with no op to match", () => {
    // The finding. `abandon_cycle`'s report carried `op: "abandon_cycle"` for
    // one round *only* because this reader returned null without an `op` key and
    // five readings below matched that string — a key on the server kept alive
    // by a client, over a report whose own comment says to branch on
    // `abandoned`. Dropping it must not make an abandoned cycle read as no
    // report at all.
    const parsed = readOperationReport(ABANDON_REPORT);
    expect(parsed).not.toBeNull();
    expect(parsed?.abandoned).toBe(true);
    expect(parsed?.op).toBeNull();
    expect(parsed?.abandonedBy).toBe("human:owner");
    // And the abandon did not fail, so nothing reads as an error.
    expect(parsed?.error).toBeNull();
  });

  it("is null for a report that is neither", () => {
    expect(readOperationReport(report(A_NIGHT))).toBeNull();
    expect(readOperationReport({ abandoned: false })).toBeNull();
    expect(readOperationReport(null)).toBeNull();
  });
});

describe("cycleWork", () => {
  it("reads a night's work as a sentence rather than a row of ids", () => {
    // The scenario the view exists for: "merged 3 duplicates, added 14 links,
    // archived 2 stale edges" is what a human wakes to.
    expect(cycleWork(cycle({ report: report(A_NIGHT) }))).toBe(
      "Flagged 3 duplicate candidates, proposed 14 links and retired 2 stale edges.",
    );
  });

  it("counts one of a thing without an s", () => {
    const one = [
      job("duplicate_candidates", { proposed: ids(1) }),
      job("link_maintenance", { applied: ids(1) }),
    ];
    expect(cycleWork(cycle({ report: report(one) }))).toBe(
      "Flagged 1 duplicate candidate and retired 1 stale edge.",
    );
  });

  it("says plainly when a cycle found nothing to change", () => {
    expect(cycleWork(cycle({ report: report([job("link_maintenance"), job("housekeeping")]) }))).toBe(
      "Ran 2 jobs and found nothing to change.",
    );
  });

  it("reports a rehearsal in the conditional, off the candidate counts", () => {
    // The bug this pins: a dry run writes no graph event, so `proposed` and
    // `applied` are empty by construction and reading them would report
    // "found nothing to change" about a run that would have proposed fourteen
    // links. The counts live in each job's `detail`.
    const rehearsed = [
      job("duplicate_candidates", { proposed: [], detail: { matched: 3 } }),
      job("link_maintenance", { proposed: [], applied: [], detail: { inferred: 14, prunable: 2 } }),
    ];
    expect(cycleWork(cycle({ dry_run: true, report: report(rehearsed, { dry_run: true }) }))).toBe(
      "Would have flagged 3 duplicate candidates, proposed 14 links and retired 2 stale edges.",
    );
  });

  it("says a rehearsal that would have done nothing was a rehearsal", () => {
    expect(
      cycleWork(cycle({ dry_run: true, report: report([job("link_maintenance")], { dry_run: true }) })),
    ).toBe("Rehearsed 1 job and found nothing to change.");
  });

  it("carries the neglect report's count and its threshold", () => {
    const neglect = [job("neglect_report", { detail: { neglected_count: 7, threshold_days: 90 } })];
    expect(cycleWork(cycle({ report: report(neglect) }))).toBe(
      "Noted 7 nodes untouched for 90 days.",
    );
  });

  it("says what the curation job learned and annotated", () => {
    // Convention nodes are proposals (they land in `proposed`); annotations
    // are rows on the annotations table, so their count comes from the job's
    // `detail` in both modes — the same shape on a rehearsal, whose lists are
    // empty by construction.
    const curated = [
      job("curation", {
        proposed: ids(1),
        detail: { acceptance: [], annotations: ["a1"], conventions: [] },
      }),
    ];
    expect(cycleWork(cycle({ report: report(curated) }))).toBe(
      "Learned 1 acceptance convention and annotated 1 queue item.",
    );
    const rehearsed = [
      job("curation", {
        proposed: [],
        detail: {
          acceptance: [],
          annotations: [{ kind: "edge", id: EDGE_ID, rate: 0.666667, dry_run: true }],
          conventions: [{ node: null, proposer: "agent:researcher", edge_type: "supports", rate: 0.666667 }],
        },
      }),
    ];
    expect(cycleWork(cycle({ dry_run: true, report: report(rehearsed, { dry_run: true }) }))).toBe(
      "Would have learned 1 acceptance convention and annotated 1 queue item.",
    );
  });

  it("names the jobs that failed rather than losing them behind the counts", () => {
    const said = cycleWork(
      cycle({
        status: "failed",
        report: report(A_NIGHT, { failed: [{ job: "link_maintenance", error: "boom" }] }),
      }),
    );
    expect(said).toContain("1 job failed: link_maintenance.");
  });

  it("says a cycle that failed before any job ran did exactly that, and no more", () => {
    // The runner writes this case with `jobs: []` and one failure whose `job`
    // is the empty string — a scope the gardener holds no grant on, say. "Ran 0
    // jobs and found nothing to change" would be true about the wrong event,
    // and the *reason* is the server's own text, which belongs to
    // `cycleFailures` and not to a headline.
    const said = cycleWork(
      cycle({
        status: "failed",
        report: report([], {
          failed: [{ job: "", error: "GrantNotPermitted: open a cycle on research" }],
        }),
      }),
    );
    expect(said).toBe("The cycle failed before any job ran.");
    expect(said).not.toContain("0 jobs");
    expect(said).not.toContain("GrantNotPermitted");
  });

  it("gives a job it does not recognise a clause of its own", () => {
    // 5b adds jobs. A sentence that silently dropped one would under-report a
    // night's work, which is worse than a clause in neutral words.
    const said = cycleWork(cycle({ report: report([job("abstraction", { proposed: ids(4) })]) }));
    expect(said).toBe("Proposed 4 rows (abstraction).");
  });

  it("reads a rollback cycle as what it was", () => {
    const said = cycleWork(
      cycle({
        trigger: "rollback",
        report: { op: "rollback_cycle", rolled_back: ROLLBACK_CYCLE_ID, reversed: 12 },
      }),
    );
    expect(said).toBe("Took 0abcdef1… back — 12 events reversed.");
  });

  it("reads a one-op curative cycle as one operation", () => {
    expect(cycleWork(cycle({ trigger: "curative", report: { op: "merge_nodes" } }))).toBe(
      "One curative operation: merge_nodes.",
    );
  });

  it("reads an abandoned run as an interrupted run, not as a curative operation", () => {
    // The shape the new abandon control makes reachable from a browser.
    // `close_cycle` replaces the interrupted run's report with `{"op":
    // "abandon_cycle", …}`, so the generic one-op branch rendered "One curative
    // operation: abandon_cycle. It failed." — wrong three ways: abandoning is
    // not a curative operation, the operation did not fail, and the entry is a
    // consolidation run rather than a single-row write.
    const said = cycleWork(cycle({ trigger: "manual", status: "failed", report: ABANDON_REPORT }));
    expect(said).toBe(
      "Interrupted, and never finished. Its entry was closed by hand so that what it had " +
        "already written could be rolled back.",
    );
    expect(said).not.toContain("curative");
  });

  it("says an operation failed without quoting what came out of it", () => {
    const said = cycleWork(
      cycle({ status: "failed", report: { op: "merge_nodes", error: "node not found" } }),
    );
    expect(said).toBe("One curative operation: merge_nodes. It failed.");
    expect(said).not.toContain("node not found");
  });

  it("says a running cycle is running rather than that it did nothing", () => {
    expect(cycleWork(cycle({ status: "running", report: null, finished_at: null }))).toContain(
      "Running now",
    );
  });

  it("admits a missing report rather than inventing a summary", () => {
    expect(cycleWork(cycle({ report: null }))).toBe("No report was recorded for this cycle.");
  });
});

/* ------------------------------------------------------------------ */
/* The headline carries nothing the server wrote                        */
/* ------------------------------------------------------------------ */

/** Copy that would resolve the server's deliberate ambiguity about a space. */
const FORBIDDEN = ["no such space", "does not exist", "no record of", "unknown space", "not found"];

/**
 * The failure a scoped cycle really records in the whole-cycle slot, in the
 * shape `nodum.consolidate` writes it (`f"{type(failure).__name__}: {failure}"`).
 *
 * `_require_gardener_scope`'s refusal, and it is the **first** thing a scoped run
 * meets on a default install: migration `0014` grants the gardener `main` and
 * `meta`, the journal's scope picker offers every space in the file, and the
 * message echoes *the reference the caller supplied* — which for the one caller
 * that reaches this path by clicking is a space **id**. It appears twice, once in
 * the sentence and once in the remedy the message spells out. It reaches
 * `failed[{job: "", …}]` with `jobs: []`, because it is raised inside
 * `_consolidate_locked`'s `try` and caught by its `except BaseException`.
 */
const UNGRANTED_SCOPE =
  `GrantNotPermitted: the gardener holds no grant on space '${RESEARCH_ID}', so it cannot ` +
  `consolidate it: migration 0014 seeds builtin-gardener with 'main' and 'meta' only, and ` +
  `every other space is an explicit grant. Run: ` +
  `nodum grant builtin-gardener ${RESEARCH_ID} edit`;

/**
 * The scope refusal, in the one slot that can actually carry it — a **job**.
 *
 * Not `failed[{job: ""}]`. `_consolidate_locked` calls `open_cycle` *outside* its
 * own `try`, so a `TypeNotFound` raised resolving the caller's scope happens
 * before the cycle row exists and can never be written into any report: a fixture
 * putting this string in the whole-cycle slot asserts a shape the server cannot
 * produce, which is the mistake this file has now paid for twice. What *can*
 * carry it is a job — `_Context.nodes()` passes the scope to
 * `list_nodes(space=…, principal=gardener)` on every read, so a space archived
 * mid-run (which makes every grant on it inert) reaches `_run_jobs`'s per-job
 * handler and is recorded under that job's name.
 */
const RECORDED_SCOPE_REFUSAL = `TypeNotFound: unknown space: ${RESEARCH_ID}`;

/**
 * The report an **abandoned** cycle wears, verbatim from `service.abandon_cycle`.
 *
 * `abandoned` is the whole discriminator, and it carries **no `op`**. It used to
 * carry `{"op": "abandon_cycle"}`, which is what forced this view to match a
 * magic string — a key the server kept for one round solely because this file's
 * readings needed it, over a report whose own comment said to branch on
 * `abandoned` instead. `close_cycle` still replaces whatever the interrupted run
 * had written, so this is what an abandoned *consolidation* run reads back as:
 * not a job list, and not a curative operation either.
 */
const ABANDON_REPORT: JsonObject = {
  abandoned: true,
  abandoned_by: "human:owner",
  // **Not `error`.** The abandon succeeded; the run is what failed, which
  // `status` already says.
  detail:
    "the run was interrupted and never closed itself; a human closed its journal entry so that " +
    "what it had already written could be rolled back",
};

/** Every forbidden phrasing at once — the adversarial half of the guard. */
const HOSTILE = `Boom: no such space, it does not exist, we have no record of it, unknown space: ${RESEARCH_ID}, not found`;

/**
 * Every branch `cycleWork` has, each with the worst thing the wire could carry
 * in every slot that holds a server-written string.
 *
 * The guard runs over the *whole* list rather than over the one branch the
 * blocker was found in: the point of the fix is that no branch quotes a report,
 * and a guard applied to one function's happy path is how the last one shipped.
 */
const EVERY_BRANCH: CycleOut[] = [
  cycle({ report: report(A_NIGHT) }),
  cycle({ dry_run: true, report: report(A_NIGHT, { dry_run: true }) }),
  cycle({ report: report([job("link_maintenance")]) }),
  cycle({ dry_run: true, report: report([job("link_maintenance")], { dry_run: true }) }),
  cycle({ report: report([job("abstraction", { proposed: ids(4) })]) }),
  cycle({ report: report([job("neglect_report", { detail: { neglected_count: 7 } })]) }),
  // Failed outside every job: the shape that produced the blocker, carrying the
  // message the runner really writes into it.
  cycle({
    status: "failed",
    scope: RESEARCH_ID,
    report: report([], { failed: [{ job: "", error: UNGRANTED_SCOPE }] }),
  }),
  cycle({
    status: "failed",
    scope: RESEARCH_ID,
    report: report([], { failed: [{ job: "", error: HOSTILE }] }),
  }),
  // Failed inside a job, with the scope refusal in the one slot that can hold
  // it, the hostile text, and a hostile job *name*.
  cycle({
    status: "failed",
    scope: RESEARCH_ID,
    report: report(A_NIGHT, {
      failed: [{ job: "link_maintenance", error: RECORDED_SCOPE_REFUSAL }],
    }),
  }),
  cycle({
    status: "failed",
    report: report(A_NIGHT, { failed: [{ job: "link_maintenance", error: HOSTILE }] }),
  }),
  cycle({ status: "failed", report: report(A_NIGHT, { failed: [{ job: HOSTILE, error: HOSTILE }] }) }),
  cycle({ report: report([job(HOSTILE, { proposed: ids(2) })]) }),
  // A report with no jobs and no failures at all.
  cycle({ report: report([]) }),
  // The operation reports, clean and failed, with hostile text in both slots.
  cycle({ trigger: "curative", report: { op: "merge_nodes" } }),
  cycle({ trigger: "curative", status: "failed", report: { op: "bulk_relink", error: HOSTILE } }),
  cycle({ trigger: "curative", status: "failed", report: { op: HOSTILE, error: HOSTILE } }),
  cycle({
    trigger: "rollback",
    report: { op: "rollback_cycle", rolled_back: ROLLBACK_CYCLE_ID, reversed: 12 },
  }),
  cycle({ trigger: "rollback", status: "failed", report: { op: "rollback_cycle", error: HOSTILE } }),
  // `shortId` leaves a *short* value whole, so a short hostile `rolled_back` is
  // the one way a server string could otherwise reach the headline unshortened.
  cycle({ trigger: "rollback", report: { op: "rollback_cycle", rolled_back: "not found" } }),
  cycle({ trigger: "rollback", report: { op: "rollback_cycle", rolled_back: HOSTILE } }),
  // An abandoned run: a report with no `op` at all, on a consolidation cycle.
  cycle({ trigger: "manual", status: "failed", report: ABANDON_REPORT }),
  cycle({
    trigger: "manual",
    status: "failed",
    report: { abandoned: true, abandoned_by: HOSTILE, detail: HOSTILE },
  }),
  // No report at all, and one still being written.
  cycle({ report: null }),
  cycle({ status: "running", report: null, finished_at: null }),
];

describe("cycleWork, over every branch", () => {
  it("never says a space does not exist, whatever the report carried", () => {
    // The blocker, generalised. `failed[].error` was interpolated straight into
    // the sentence, so a real scoped cycle rendered "The cycle failed before any
    // job ran: TypeNotFound: unknown space: 909a3060…" as the `<h1>` on
    // /journal/:id and as the link text in the list — the frontend contract's
    // one unconditional rule, broken in exactly the named shape.
    for (const entry of EVERY_BRANCH) {
      const said = cycleWork(entry).toLowerCase();
      for (const phrase of FORBIDDEN) expect(said).not.toContain(phrase);
    }
  });

  it("never puts a bare 32-hex id on the screen", () => {
    // The other half of the same finding. Ids are shortened where they are shown
    // at all, and a report's own text is not shown.
    for (const entry of EVERY_BRANCH) {
      expect(cycleWork(entry)).not.toMatch(BARE_ID);
    }
  });

  it("quotes no part of a recorded failure, whatever it says", () => {
    // The sharper property, and the one that holds regardless of what the server
    // sends: the headline is built from counts and registered names, so a
    // sentinel dropped into every message slot cannot appear in it.
    const sentinel = "SENTINEL-a1b2c3";
    const entries: CycleOut[] = [
      cycle({ report: report([], { failed: [{ job: "", error: sentinel }] }) }),
      cycle({ report: report(A_NIGHT, { failed: [{ job: "link_maintenance", error: sentinel }] }) }),
      cycle({ report: { op: "merge_nodes", error: sentinel } }),
      cycle({ report: { op: "rollback_cycle", rolled_back: CYCLE_ID, error: sentinel } }),
    ];
    for (const entry of entries) expect(cycleWork(entry)).not.toContain(sentinel);
  });

  it("still answers with a non-empty sentence on every branch", () => {
    // A guard that silenced the headline would pass the three above and lose the
    // view; the sentence is the reason this screen exists.
    for (const entry of EVERY_BRANCH) expect(cycleWork(entry).length).toBeGreaterThan(0);
  });
});

describe("cycleFailures", () => {
  it("carries the reason the headline no longer does", () => {
    const lines = cycleFailures(
      cycle({ report: report(A_NIGHT, { failed: [{ job: "link_maintenance", error: "boom" }] }) }),
    );
    expect(lines).toEqual(["The job link_maintenance raised: boom"]);
  });

  it("says a space stopped resolving without saying it is missing", () => {
    // Recorded under the **job** that met it: `open_cycle` resolves the caller's
    // scope before the cycle row exists, so this string can only reach a report
    // through `_run_jobs`'s per-job handler — a space archived mid-run.
    const lines = cycleFailures(
      cycle({
        status: "failed",
        scope: RESEARCH_ID,
        report: report(A_NIGHT, {
          failed: [{ job: "link_maintenance", error: RECORDED_SCOPE_REFUSAL }],
        }),
      }),
      RESEARCH,
    );
    expect(lines).toHaveLength(1);
    const said = lines[0]!.toLowerCase();
    for (const phrase of FORBIDDEN) expect(said).not.toContain(phrase);
    expect(lines[0]).not.toMatch(BARE_ID);
    expect(lines[0]).toContain('the scope "research"');
    expect(lines[0]).toContain("archived or renamed");
  });

  it("names the space the gardener holds no grant on, rather than spelling it twice", () => {
    // The blocker, in the shape the server actually writes: this is the refusal
    // a scoped run meets on a default install, and it carried a bare 32-hex id
    // twice — once in the sentence and once in the remedy — through the list
    // *and* the detail page.
    const lines = cycleFailures(
      cycle({
        status: "failed",
        scope: RESEARCH_ID,
        report: report([], { failed: [{ job: "", error: UNGRANTED_SCOPE }] }),
      }),
      RESEARCH,
    );
    expect(lines).toHaveLength(1);
    expect(lines[0]).not.toMatch(BARE_ID);
    expect(lines[0]).toContain("The cycle failed before any job ran:");
    expect(lines[0]).toContain('the scope "research"');
    expect(lines[0]).toContain("nodum grant builtin-gardener 'research' edit");
  });

  it("names a curative operation's own failure", () => {
    expect(cycleFailures(cycle({ report: { op: "merge_nodes", error: "node not found" } }))).toEqual(
      ["The operation merge_nodes failed: node not found"],
    );
  });

  it("says an abandoned run was interrupted, not that the abandon failed", () => {
    // The server's own line says the right thing under a prefix that does not:
    // "The operation abandon_cycle failed: the run was interrupted…". The
    // operation succeeded; the run it closed is what never finished.
    const lines = cycleFailures(
      cycle({ trigger: "manual", status: "failed", report: ABANDON_REPORT }),
    );
    expect(lines).toHaveLength(1);
    expect(lines[0]).not.toContain("abandon_cycle failed");
    expect(lines[0]).toContain("interrupted and never closed itself");
    // Who closed it, named the way every other actor on this screen is.
    expect(lines[0]).toContain("owner closed its journal entry");
    // And the thing a reader has to know: abandoning undid nothing.
    expect(lines[0]).toContain("Nothing it wrote was undone");
  });

  it("has nothing to say about a clean cycle", () => {
    expect(cycleFailures(cycle({ report: report(A_NIGHT) }))).toEqual([]);
    expect(cycleFailures(cycle({ report: null }))).toEqual([]);
    expect(cycleFailures(cycle({ report: { op: "merge_nodes" } }))).toEqual([]);
  });
});

describe("describeRecordedFailure", () => {
  it("routes a recorded refusal through the same discriminator a live one takes", () => {
    // Not a second copy of the match: `recordedUnknownSpace` is `client.ts`'s
    // own `unknown space:` regex, reading a string that never came back through
    // `fetch`. Both wordings therefore move together.
    const said = describeRecordedFailure(RECORDED_SCOPE_REFUSAL, RESEARCH).toLowerCase();
    for (const phrase of FORBIDDEN) expect(said).not.toContain(phrase);
    expect(said).toContain("would not resolve");
  });

  it("names the scope generically when nothing has resolved it", () => {
    const said = describeRecordedFailure(RECORDED_SCOPE_REFUSAL, null);
    expect(said).toContain("the scope it named");
    expect(said).not.toMatch(BARE_ID);
  });

  it("rewrites the gardener's own scope refusal, naming the space and the remedy", () => {
    // The **second** shape to reach a screen verbatim, and the one a default
    // install meets first: migration 0014 grants the gardener main and meta, the
    // picker offers everything, and the message echoes the caller's reference —
    // a 32-hex id, printed twice.
    const said = describeRecordedFailure(UNGRANTED_SCOPE, RESEARCH);
    expect(said).not.toMatch(BARE_ID);
    expect(said).toContain("holds no grant on the scope \"research\"");
    // The remedy is the whole value of the server's sentence, so it survives —
    // with the space named, and quoted, because a title may contain a space and
    // `nodum grant` resolves an id or a name.
    expect(said).toContain("nodum grant builtin-gardener 'research' edit");
    for (const phrase of FORBIDDEN) expect(said.toLowerCase()).not.toContain(phrase);
  });

  it("keeps the remedy usable when nothing has resolved the scope", () => {
    const said = describeRecordedFailure(UNGRANTED_SCOPE, null);
    expect(said).not.toMatch(BARE_ID);
    expect(said).toContain("the scope it named");
    expect(said).toContain("nodum grant builtin-gardener <space> edit");
  });

  it("fails closed on a shape it does not know, rather than printing its ids", () => {
    // The point of the fix. Two message shapes have now reached a screen
    // verbatim, so the default is inverted: an unfamiliar failure still says
    // what the server said — which is what keeps it legible — but no id
    // survives it, so a *third* shape cannot ship one silently.
    const unknown = `MergeRefused: node ${NODE_ID} was merged into ${SRC_ID} on 2026-07-27`;
    const said = describeRecordedFailure(unknown, RESEARCH);
    expect(said).not.toMatch(BARE_ID);
    expect(said).toContain("MergeRefused");
    expect(said).toContain("was merged into");
    expect(said).toContain("a1a2b3c4d5e6…");
  });

  it("shows a recorded failure that names no row exactly as the server wrote it", () => {
    // The bargain is unchanged for everything that carries no id: this module
    // owns the refusals whose *wording* is a decision, not every message the
    // service can produce.
    const plain = "ProviderUnavailable: no embedding provider is configured";
    expect(describeRecordedFailure(plain, RESEARCH)).toBe(plain);
    expect(describeRecordedFailure("boom", null)).toBe("boom");
  });
});

describe("nameIdsIn", () => {
  it("takes every raw id out of a server sentence, not only the first", () => {
    // The gardener's refusal names its scope twice. A replace that stopped at
    // the first match would have left the second on the screen.
    const said = nameIdsIn(`space ${NODE_ID} still holds 1 node (${SRC_ID})`);
    expect(said).not.toMatch(BARE_ID);
    expect(said).toBe("space a1a2b3c4d5e6… still holds 1 node (4f2ad9c1b0e3…)");
  });

  it("uses the page's own name for a row it knows, and shortens the rest", () => {
    const names = new Map([[NODE_ID, "note: Kafka Streams"]]);
    const said = nameIdsIn(`node ${NODE_ID} still has 1 child node (${SRC_ID})`, (id) =>
      names.get(id) ?? null,
    );
    expect(said).toContain("node note: Kafka Streams still has");
    expect(said).toContain("4f2ad9c1b0e3…");
    expect(said).not.toMatch(BARE_ID);
  });

  it("leaves a sentence with no id in it alone", () => {
    expect(nameIdsIn("revoke them first")).toBe("revoke them first");
  });
});

describe("actorLabel", () => {
  it("names a human the way the header does", () => {
    // The header greets them as "owner"; "Run on demand by human:owner" prints
    // an id at someone whose name is on the same screen.
    expect(actorLabel("human:owner")).toBe("owner");
  });

  it("says an agent's kind in words rather than as a prefix", () => {
    expect(actorLabel("agent:builtin-gardener")).toBe("the agent builtin-gardener");
  });

  it("leaves anything that is not an actor string alone", () => {
    expect(actorLabel("scheduler")).toBe("scheduler");
    expect(actorLabel("")).toBe("");
    expect(actorLabel(":oops")).toBe(":oops");
    expect(actorLabel("human:")).toBe("human:");
  });
});

describe("cycleProvenance", () => {
  it("keeps who asked apart from what ran, and names neither by id", () => {
    // `triggered_by` is who *asked*; the events inside are the gardener's. The
    // sentence names the trigger so the two are never collapsed — and names the
    // human rather than spelling their actor string.
    expect(cycleProvenance(cycle())).toBe("Run on the nightly schedule, across the whole file.");
    expect(
      cycleProvenance(
        cycle({ trigger: "manual", triggered_by: "human:owner", scope: RESEARCH_ID }),
        RESEARCH,
      ),
    ).toBe("Run on demand by owner, confined to the space research.");
  });

  it("names a scoped cycle's space rather than printing its id", () => {
    // The bug: `cycle.scope` is always the **resolved id** (`open_cycle` →
    // `_resolve_space`), so this sentence read "confined to the space
    // f73944650d5c4255a0aa5421308f62b0" on every entry in the list and in the
    // detail header, for every space a human ever created.
    const scoped = cycle({ scope: RESEARCH_ID });
    expect(cycleProvenance(scoped, RESEARCH)).toContain("confined to the space research");
    expect(cycleProvenance(scoped, RESEARCH)).not.toMatch(BARE_ID);
    expect(cycleProvenance(cycle({ scope: null }))).toContain("across the whole file");
  });

  it("marks a scope archived since the cycle ran", () => {
    // A cycle can perfectly well name a space retired since — `GET /api/spaces`
    // will not carry it, and the lazy archived read is what names it.
    const said = cycleProvenance(cycle({ scope: RESEARCH_ID }), {
      label: "research",
      kind: "archived",
    });
    expect(said).toContain("confined to the space research, archived since");
  });

  it("falls back to the reference only while nothing has resolved it", () => {
    // `null` here means "no resolution supplied", which is the loading window.
    expect(cycleProvenance(cycle({ scope: RESEARCH_ID }), null)).toContain(RESEARCH_ID);
  });

  it("names the two cycles nobody asked for directly", () => {
    expect(cycleProvenance(cycle({ trigger: "rollback" }))).toContain("Opened by a rollback");
    expect(
      cycleProvenance(cycle({ trigger: "curative", triggered_by: "human:owner" })),
    ).toContain("curative operation owner asked for");
  });
});

describe("cycleScopeIds", () => {
  it("collects each scope once, and skips the unscoped cycles", () => {
    // What drives the lazy archived-space read: the ids the screen names.
    const listed = [
      cycle({ scope: RESEARCH_ID }),
      cycle({ scope: null }),
      cycle({ scope: RESEARCH_ID }),
      cycle({ scope: "main" }),
    ];
    expect(cycleScopeIds(listed)).toEqual([RESEARCH_ID, "main"]);
    expect(cycleScopeIds([])).toEqual([]);
  });
});

describe("cycleScopeSpaces", () => {
  it("drops meta, because a cycle confined to it is a guaranteed no-op", () => {
    // `consolidate._is_curatable` excludes every node in meta, so a cycle
    // scoped there examines nothing and closes reporting "Ran 4 jobs and found
    // nothing to change" — a clean-looking night from a control that could
    // never have done anything.
    const spaces = [
      spaceNode("main", "main"),
      spaceNode("meta", "meta"),
      spaceNode(RESEARCH_ID, "research"),
    ];
    expect(cycleScopeSpaces(spaces)?.map((space) => space.id)).toEqual(["main", RESEARCH_ID]);
  });

  it("passes an unknown list through as unknown", () => {
    // Null is "not answered yet" and reading it as empty would offer nothing.
    expect(cycleScopeSpaces(null)).toBeNull();
  });
});

describe("SCOPE_CONTROL_HINT", () => {
  it("does not describe a write control as a read filter", () => {
    // The panel reuses `SpaceFilter`, whose own tooltip promises "Narrow this
    // view to one space. It never widens what you can see." — true of every
    // listing it filters, and the opposite of what the button beside it does
    // here, where the choice decides what the gardener acts on.
    expect(SCOPE_CONTROL_HINT).not.toContain("Narrow this view");
    expect(SCOPE_CONTROL_HINT).not.toContain("never widens");
    expect(SCOPE_CONTROL_HINT).toContain("what the gardener acts on");
    expect(SCOPE_CONTROL_HINT).toContain("not what you can see");
  });
});

describe("cycleCaveats", () => {
  it("has nothing to say about an ordinary completed cycle", () => {
    expect(cycleCaveats(cycle())).toEqual([]);
  });

  it("says a reversed cycle is not standing, and names what reversed it", () => {
    const lines = cycleCaveats(cycle({ status: "rolled_back", rolled_back_by: ROLLBACK_CYCLE_ID }))
      .join(" ");
    expect(lines).toContain("rolled back by cycle 0abcdef1…");
    expect(lines).toContain("Nothing it wrote is still standing");
  });

  it("says a rehearsal wrote no graph event", () => {
    const ran = cycle({ dry_run: true, report: report(A_NIGHT, { dry_run: true }) });
    expect(cycleCaveats(ran).join(" ")).toContain("no graph event was written");
    expect(cycleCaveats(ran).join(" ")).toContain("every job ran");
  });

  it("says a failed cycle's writes are real and stay", () => {
    // The dangerous assumption is that "failed" means "nothing happened".
    const lines = cycleCaveats(cycle({ status: "failed", report: report(A_NIGHT) })).join(" ");
    expect(lines).toContain("is real and stays");
    expect(lines).toContain("rollback");
  });

  it("tells a stopped run apart from a run that fell over, in the one status they share", () => {
    // The question the kill switch exists to keep answerable: a human reading a
    // `failed` cycle at 09:00 has to know whether the operator stopped that run
    // or the process died. A stopped run closes itself `failed`, so the status
    // cannot answer it — read without the stop this caveat called an obeyed
    // instruction a fault.
    const stopped = cycleCaveats(
      cycle({
        status: "failed",
        report: report(A_NIGHT),
        stop_requested: true,
        stop_requested_by: "human:owner",
        stop_requested_at: STOPPED_AT,
      }),
    ).join(" ");
    expect(stopped).toContain("a stop was asked for on it");
    expect(stopped).toContain("rather than a fault");
    expect(stopped).toContain("is real and stays");
    // And a cycle that really did fall over is still described as one.
    const failed = cycleCaveats(cycle({ status: "failed", report: report(A_NIGHT) })).join(" ");
    expect(failed).not.toContain("rather than a fault");
    expect(failed).toContain("before the failure");
  });

  it("says a stop on a running entry has reversed nothing and closed nothing", () => {
    const said = cycleCaveats(
      cycle({
        status: "running",
        finished_at: null,
        report: null,
        stop_requested: true,
        stop_requested_by: "human:owner",
        stop_requested_at: STOPPED_AT,
      }),
    ).join(" ");
    expect(said).toContain("Still running");
    expect(said).toContain("A stop has been asked for");
    expect(said).toContain("reverses anything");
  });

  it("does not read a completed run that was stopped as a failure", () => {
    // Reachable today and not an edge case: the deterministic jobs make no
    // provider call and so no stop check, so a cycle stopped mid-run finishes
    // and closes `completed` carrying the stamp. A caveat that read alarm into
    // that would be describing the opposite of what happened.
    const said = cycleCaveats(
      cycle({
        report: report(A_NIGHT),
        stop_requested: true,
        stop_requested_by: "human:owner",
        stop_requested_at: STOPPED_AT,
      }),
    ).join(" ");
    expect(said).toContain("completed anyway");
    expect(said).toContain("noticed at the next check");
    expect(said).not.toContain("failed");
  });

  it("composes, because a failed rehearsal is both", () => {
    expect(
      cycleCaveats(
        cycle({ status: "failed", dry_run: true, report: report(A_NIGHT, { dry_run: true }) }),
      ),
    ).toHaveLength(2);
  });

  it("does not claim every job ran on a cycle whose headline says none did", () => {
    // Both halves of the contradiction, in the one shape that produces it: a
    // scoped rehearsal the gardener holds no grant on. `cycleWork` reads "The
    // cycle failed before any job ran." — and the caveats under it read "every
    // job ran".
    const entry = cycle({
      status: "failed",
      dry_run: true,
      scope: RESEARCH_ID,
      report: report([], { dry_run: true, failed: [{ job: "", error: UNGRANTED_SCOPE }] }),
    });
    expect(cycleWork(entry)).toBe("The cycle failed before any job ran.");
    const said = cycleCaveats(entry).join(" ");
    expect(said).not.toContain("every job ran");
    expect(said).toContain("no job has reported");
  });

  it("does not promise a rollback beside a button saying there is nothing to reverse", () => {
    // The other half. `rollbackAvailability` disables the button on a rehearsal
    // — "A rehearsal emitted no graph event, so there is nothing to reverse" —
    // while this caveat told the same reader a rollback was what takes it back.
    const rehearsal = cycle({
      status: "failed",
      dry_run: true,
      report: report(A_NIGHT, { dry_run: true }),
    });
    const said = cycleCaveats(rehearsal).join(" ");
    expect(said).not.toContain("is real and stays");
    expect(said).toContain("nothing to reverse");
    expect(rollbackAvailability(rehearsal).reason).toContain("nothing to reverse");
  });

  it("still says a real failed cycle's writes stand, which is the dangerous case", () => {
    // The branch must not swallow the warning that matters: a `BaseException`
    // out of `_run_jobs` empties the job list while leaving whatever the jobs
    // had already written in the graph.
    const said = cycleCaveats(
      cycle({ status: "failed", report: report([], { failed: [{ job: "", error: "boom" }] }) }),
    ).join(" ");
    expect(said).toContain("is real and stays");
  });
});

describe("abandonAvailability", () => {
  it("offers the door out of a cycle a crash left running", () => {
    // `POST /api/cycles/{id}/abandon` and `nodum cycle-abandon` both shipped and
    // no surface here offered either, so a `running` cycle's writes were
    // irreversible everywhere: rollback refuses one that has not closed, and
    // undo refuses every event a cycle stamped.
    expect(abandonAvailability(cycle({ status: "running", finished_at: null }))).toEqual({
      available: true,
      reason: null,
    });
  });

  it("refuses a cycle that has already said how it ended, in those words", () => {
    // `service.abandon_cycle`'s one refusal, stated in front of the button
    // rather than met after clicking it.
    for (const status of ["completed", "failed", "rolled_back"]) {
      const verdict = abandonAvailability(cycle({ status }));
      expect(verdict.available).toBe(false);
      expect(verdict.reason).toContain(`closed ${status}`);
      expect(verdict.reason).toContain("would overwrite that");
    }
  });
});

describe("ABANDON_CONFIRM", () => {
  it("says outright that it reverses nothing", () => {
    // The dangerous misreading: that abandoning cancels the run or takes its
    // writes back. It closes the row and nothing else.
    const said = ABANDON_CONFIRM.join(" ");
    expect(said).toContain("does not reverse anything");
    expect(said).toContain("still in the graph");
  });

  it("says what closing the entry is for", () => {
    const said = ABANDON_CONFIRM.join(" ");
    expect(said).toContain("makes those writes reversible");
    expect(said).toContain("Roll the cycle back afterwards");
  });

  it("admits it does not stop a cycle that is genuinely still running", () => {
    // Nothing in `abandon_cycle` checks that the process is dead, so the confirm
    // must not imply it does.
    expect(ABANDON_CONFIRM.join(" ")).toContain("Nothing here stops a cycle that is genuinely");
  });
});

describe("abandonOutcome", () => {
  it("reports the close without claiming anything was undone", () => {
    const said = abandonOutcome(cycle({ status: "failed" }));
    expect(said).toContain("closed as failed");
    expect(said).toContain("Nothing it wrote has changed");
    expect(said).toContain("rolled back now");
    expect(said).not.toMatch(BARE_ID);
  });
});

describe("stopAvailability", () => {
  it("offers the kill switch on a running cycle nobody has stopped yet", () => {
    // `service.request_stop` and migration `0015` both shipped and no surface
    // reached either — the door-nothing-opens defect, on the one screen that
    // displays a running cycle.
    expect(stopAvailability(cycle({ status: "running", finished_at: null }))).toEqual({
      available: true,
      reason: null,
    });
  });

  it("refuses a cycle that has already said how it ended, and says a stop needs a live run", () => {
    for (const status of ["completed", "failed", "rolled_back"]) {
      const verdict = stopAvailability(cycle({ status }));
      expect(verdict.available).toBe(false);
      expect(verdict.reason).toContain(`closed ${status}`);
      expect(verdict.reason).toContain("instruction to a live run");
    }
  });

  it("gives way to the record once a stop is already on the entry", () => {
    // The service makes a second stop a *no-op* rather than an error, so that a
    // human pressing twice never doubts the first press. Re-offering the button
    // would re-create that doubt on the screen: it would change nothing and say
    // nothing. What replaces it is who asked, which is more than a second press
    // would have told anybody.
    const verdict = stopAvailability(
      cycle({
        status: "running",
        finished_at: null,
        stop_requested: true,
        stop_requested_by: "human:owner",
        stop_requested_at: STOPPED_AT,
      }),
    );
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("already been asked for");
    expect(verdict.reason).toContain("first asker");
  });
});

describe("RUNNING_ACTIONS_HINT", () => {
  it("names both controls by the strings the controls render", () => {
    // A `running` entry offers two irreversible verbs that look alike, and
    // nothing on the page can tell a human which one they want — whether the
    // process behind the row is alive is not a fact the server has. So the copy
    // states both situations, and it names each control through its own exported
    // label so a reworded button cannot leave this sentence pointing at nothing.
    expect(RUNNING_ACTIONS_HINT).toContain(STOP_ACTION_LABEL);
    expect(RUNNING_ACTIONS_HINT).toContain(ABANDON_ACTION_LABEL);
  });

  it("gives each verb its situation, and says what neither of them does", () => {
    expect(RUNNING_ACTIONS_HINT).toContain("going right now");
    expect(RUNNING_ACTIONS_HINT).toContain("never going to finish");
    expect(RUNNING_ACTIONS_HINT).toContain("Neither reverses anything");
    expect(RUNNING_ACTIONS_HINT).toContain("rolling the cycle back");
  });
});

describe("stopRecord", () => {
  it("names who asked without spelling their actor string, and hands the time back raw", () => {
    // The *when* stays the server's string on purpose: SQLite writes
    // `datetime('now')` with no zone marker, so it goes through `lib/time` in
    // the view and must never meet `new Date()` here.
    const record = stopRecord(
      cycle({
        status: "running",
        finished_at: null,
        stop_requested: true,
        stop_requested_by: "human:owner",
        stop_requested_at: STOPPED_AT,
      }),
    );
    expect(record).toEqual({ by: "owner", at: STOPPED_AT });
  });

  it("is null for a run nobody asked to stop", () => {
    expect(stopRecord(cycle())).toBeNull();
  });

  it("still names somebody if a stamp ever arrives without a requester", () => {
    // The server's CHECK constraint makes this unstorable, and the branch exists
    // anyway: every read of this wire is defensive, and "null asked this run to
    // stop" is the sentence that would otherwise reach a screen.
    const record = stopRecord(
      cycle({ stop_requested: true, stop_requested_by: null, stop_requested_at: STOPPED_AT }),
    );
    expect(record?.by).toBe("Somebody");
  });
});

describe("STOP_CONFIRM", () => {
  it("says outright that it reverses nothing", () => {
    const said = STOP_CONFIRM.join(" ");
    expect(said).toContain("does not reverse anything");
    expect(said).toContain("stays in the graph");
    expect(said).toContain("Rolling the cycle back afterwards");
  });

  it("says it is not an abandon, and names the control that is", () => {
    // The two misreadings this button sits between. A reader who takes a stop
    // for a gentler abandon will use it on a run nothing is going to finish,
    // where it does nothing at all — so the confirm points at the other control
    // by the same string that control renders.
    const said = STOP_CONFIRM.join(" ");
    expect(said).toContain("not abandoning");
    expect(said).toContain("a live run obeys");
    expect(said).toContain(ABANDON_ACTION_LABEL);
  });

  it("admits the deterministic jobs will not notice it", () => {
    // The line that costs something and is said anyway. `AgentRun.chat` checks
    // the switch before a provider call and that is the only check there is, so
    // a stop asked for during a deterministic cycle is recorded and that run
    // finishes. Promising a wind-down that would not arrive is the copy defect
    // this module exists to prevent — and
    // `test_the_deterministic_runner_consults_no_stop_switch_and_the_copy_says_so`
    // is what keeps this sentence answerable to the runner.
    const said = STOP_CONFIRM.join(" ");
    expect(said).toContain("deterministic jobs make none");
    expect(said).toContain("finishes even after you stop it");
  });

  it("names no id and never says a space does not exist", () => {
    const said = STOP_CONFIRM.join(" ").toLowerCase();
    for (const phrase of FORBIDDEN) expect(said).not.toContain(phrase);
    expect(STOP_CONFIRM.join(" ")).not.toMatch(BARE_ID);
  });
});

describe("stopOutcome", () => {
  it("reports the instruction and never the outcome", () => {
    // The row comes back still `running`. A toast saying "stopped" would claim
    // something about a run that is still writing, which is the one thing this
    // screen cannot make good on.
    const said = stopOutcome(
      cycle({ status: "running", finished_at: null, stop_requested: true }),
    );
    expect(said).toContain("has been asked to stop");
    expect(said).toContain("still running");
    expect(said).toContain("nothing it wrote has changed");
    expect(said).not.toMatch(BARE_ID);
  });
});

describe("the stop control promises no wind-down anywhere", () => {
  /**
   * Every string this app puts in front of a human about the stop switch.
   *
   * Four surfaces, and three of them used to promise a wind-down in their own
   * words: the button's tooltip ("ask this run to wind down and close its own
   * entry"), the two-control hint ("the run closes its own entry when it
   * notices") and the toast a human reads immediately after pressing ("the
   * entry closes when the run notices"). Only the confirm carried the caveat.
   * The code is right and the copy was wrong, which is the whole shape of this
   * defect class — so what is asserted is that every one of them now carries
   * the same sentence, not that each has been reworded well.
   */
  const stopCopy = (): Record<string, string> => {
    const running = cycle({ status: "running", finished_at: null });
    return {
      "the button's tooltip": STOP_ACTION_HINT,
      "the two-control hint": RUNNING_ACTIONS_HINT,
      "the confirm dialog": STOP_CONFIRM.join(" "),
      "the toast after pressing": stopOutcome(running),
      "the already-stopped reason":
        stopAvailability(
          cycle({
            status: "running",
            finished_at: null,
            stop_requested: true,
            stop_requested_by: "human:owner",
            stop_requested_at: STOPPED_AT,
          }),
        ).reason ?? "",
      "the running caveat": cycleCaveats(
        cycle({ status: "running", finished_at: null, stop_requested: true }),
      ).join(" "),
    };
  };

  it("carries the one caveat in every place a human meets the switch", () => {
    for (const [surface, said] of Object.entries(stopCopy())) {
      expect(said, surface).toContain(STOP_IS_NOTICED_AT_A_MODEL_CALL);
    }
  });

  it("promises a wind-down in none of them", () => {
    // The three wordings that were on the screen, plus the word itself. A stop
    // is *recorded*; whether anything winds down depends on a check that today
    // only a model call makes.
    const promised = [
      "wind down",
      "closes its own entry when it notices",
      "closes when the run notices",
      "notices and closes itself",
    ];
    for (const [surface, said] of Object.entries(stopCopy())) {
      for (const phrase of promised) {
        expect(said.toLowerCase(), `${surface} still promises "${phrase}"`).not.toContain(phrase);
      }
    }
  });

  it("keeps the race the review drove live readable: a stopped run may complete", () => {
    // The live pass stopped a consolidation and watched it run to `completed`,
    // exactly as the confirm warned. The caveat has to stay true of that, and
    // the entry a human then opens has to explain it rather than alarm them.
    const completed = cycleCaveats(cycle({ status: "completed", stop_requested: true })).join(" ");
    expect(completed).toContain("completed anyway");
    expect(completed).toContain("a run with none left to make finishes");
    expect(STOP_IS_NOTICED_AT_A_MODEL_CALL).toContain("finishes even after you stop it");
  });
});

describe("rollbackAvailability", () => {
  it("offers the action on a finished, real cycle", () => {
    expect(rollbackAvailability(cycle())).toEqual({ available: true, reason: null });
    expect(rollbackAvailability(cycle(), true)).toEqual({ available: true, reason: null });
  });

  it("refuses a running cycle, and names the way out rather than stopping there", () => {
    // The refusal used to be the whole of what this screen said about a cycle a
    // crash left `running` — a dead end, on the one screen that displays the
    // stuck entry, while `abandon` was a route away and a CLI command.
    const verdict = rollbackAvailability(cycle({ status: "running", finished_at: null }));
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("has not finished writing");
    expect(verdict.reason).toContain("abandon it first");
  });

  it("refuses one already rolled back, and points at the way to re-apply it", () => {
    const verdict = rollbackAvailability(
      cycle({ status: "rolled_back", rolled_back_by: ROLLBACK_CYCLE_ID }),
    );
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("Roll that one back to re-apply this one");
  });

  it("refuses a rehearsal, because it emitted nothing to reverse", () => {
    const verdict = rollbackAvailability(cycle({ dry_run: true }));
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("nothing to reverse");
  });

  it("refuses a cycle that wrote no graph event, rather than letting the preflight do it", () => {
    // A no-op curative cycle — a `bulk_relink` whose selector matched nothing —
    // used to get a live button whose preflight then answered
    // "InvalidTransition: wrote no graph events". The page has read the event
    // list; it can say so in front of the click, as the other three do.
    const verdict = rollbackAvailability(
      cycle({ trigger: "curative", report: { op: "bulk_relink" } }),
      false,
    );
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("no graph event");
    expect(verdict.reason).toContain("curative operation that matched nothing");
  });

  it("says why an abandoned run wrote nothing, rather than blaming a curative op", () => {
    // The same sentence one shape along: an abandoned run that died early is not
    // "a curative operation that matched nothing".
    const verdict = rollbackAvailability(
      cycle({ trigger: "manual", status: "failed", report: ABANDON_REPORT }),
      false,
    );
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("interrupted before it wrote anything");
    expect(verdict.reason).not.toContain("curative");
  });

  it("keeps offering the action while the answer is unknown", () => {
    // An undecidable refusal belongs to the preflight, not to a disabled button.
    expect(rollbackAvailability(cycle(), null).available).toBe(true);
  });
});

describe("metricRows", () => {
  const metrics: CycleMetrics = {
    before: {
      orphan_rate: 0.25,
      link_density: 1.4,
      duplicate_candidates: 2,
      queue_age_days: 12.5,
      neglect_rate: 0.1,
    },
    after: {
      orphan_rate: 0.18,
      link_density: 1.62,
      duplicate_candidates: 5,
      queue_age_days: 12.5,
      neglect_rate: 0.1,
    },
  };

  it("renders the five metrics in reading order", () => {
    expect(metricRows(metrics).map((row) => row.key)).toEqual([
      "orphan_rate",
      "link_density",
      "duplicate_candidates",
      "queue_age_days",
      "neglect_rate",
    ]);
  });

  it("renders each metric in its own unit", () => {
    const rows = new Map(metricRows(metrics).map((row) => [row.key, row]));
    expect(rows.get("orphan_rate")?.before).toBe("25.0%");
    expect(rows.get("link_density")?.after).toBe("1.62");
    expect(rows.get("duplicate_candidates")?.after).toBe("5");
    expect(rows.get("queue_age_days")?.before).toBe("12.5 d");
  });

  it("gives a ratio's delta in percentage points, not in percent of a percent", () => {
    const rows = new Map(metricRows(metrics).map((row) => [row.key, row]));
    expect(rows.get("orphan_rate")?.delta).toBe("−7.0 pp");
    expect(rows.get("duplicate_candidates")?.delta).toBe("+3");
    expect(rows.get("link_density")?.delta).toBe("+0.22");
  });

  it("says no change rather than a signed zero", () => {
    const rows = new Map(metricRows(metrics).map((row) => [row.key, row]));
    expect(rows.get("queue_age_days")?.delta).toBe("no change");
    expect(rows.get("queue_age_days")?.direction).toBe("flat");
  });

  it("says no change for a movement too small to print, rather than −0.0 d", () => {
    // Observed live. `queue_age_days` is a median over timestamps, so an
    // unchanged queue re-measured seconds later moves by microseconds: far above
    // any raw epsilon and far below the tenth of a day the cell renders. It came
    // out as "−0.0 d" with a down arrow, beside four metrics reading "no change".
    const drift: CycleMetrics = {
      before: { queue_age_days: 12.5, orphan_rate: 0.25, link_density: 1.4 },
      after: { queue_age_days: 12.499_977, orphan_rate: 0.250_000_4, link_density: 1.400_002 },
    };
    for (const row of metricRows(drift)) {
      expect(row.delta).toBe("no change");
      expect(row.direction).toBe("flat");
      expect(row.delta).not.toContain("0.0");
    }
  });

  it("still reports a movement that does print", () => {
    // The guard above must not swallow a real change at the rendered precision.
    const small: CycleMetrics = {
      before: { queue_age_days: 12.5 },
      after: { queue_age_days: 12.44 },
    };
    expect(metricRows(small)[0]?.delta).toBe("−0.1 d");
    expect(metricRows(small)[0]?.direction).toBe("down");
  });

  it("warns that fresh proposals pull the queue age down", () => {
    // The table deliberately does not colour a direction, so the note is the
    // only place this can be said — and it has to be, because filing seven
    // proposals makes the queue worse and this number smaller.
    const note = metricRows(metrics).find((row) => row.key === "queue_age_days")?.note ?? "";
    expect(note).toContain("Fresh proposals");
    expect(note).toContain("pulls this down");
  });

  it("reports direction and never a verdict", () => {
    // A cycle that flags duplicates *raises* duplicate_candidates by working, so
    // "up is bad" is a judgement this view is not in a position to make.
    for (const row of metricRows(metrics)) {
      expect(["up", "down", "flat", "unknown"]).toContain(row.direction);
    }
    const rows = new Map(metricRows(metrics).map((row) => [row.key, row]));
    expect(rows.get("orphan_rate")?.direction).toBe("down");
    expect(rows.get("duplicate_candidates")?.direction).toBe("up");
  });

  it("is empty for a cycle that computed none", () => {
    // A rollback and a one-op curative cycle report `{}` — a real answer, and
    // the caller's cue to say so rather than draw a table of dashes.
    expect(metricRows({})).toEqual([]);
    expect(metricRows({ before: {}, after: {} })).toEqual([]);
  });

  it("dashes a metric only one snapshot carried, rather than inventing a delta", () => {
    const partial: CycleMetrics = { before: {}, after: { orphan_rate: 0.2 } };
    const row = metricRows(partial)[0];
    expect(row?.before).toBe("—");
    expect(row?.after).toBe("20.0%");
    expect(row?.delta).toBe("—");
    expect(row?.direction).toBe("unknown");
  });

  it("renders a metric this build does not know about rather than dropping it", () => {
    // The wire shape is an object keyed by name precisely so 5b's two
    // judgement-dependent metrics can arrive without a migration.
    const future: CycleMetrics = {
      before: { unresolved_contradictions: 4 },
      after: { unresolved_contradictions: 2 },
    };
    const row = metricRows(future)[0];
    expect(row?.key).toBe("unresolved_contradictions");
    expect(row?.label).toBe("unresolved_contradictions");
    expect(row?.delta).toBe("−2.00");
  });
});

describe("noMetricsNote", () => {
  it("tells a failed consolidation cycle apart from a rollback", () => {
    // The bug: a consolidation cycle that failed outside every job has
    // `metrics == {}` too, and was told "a rollback and a one-op curative cycle
    // do not compute them" — two things its cycle is not.
    const failed = cycle({
      status: "failed",
      report: report([], { failed: [{ job: "", error: UNGRANTED_SCOPE }] }),
    });
    expect(noMetricsNote(failed)).toContain("failed before it could measure anything");
    expect(noMetricsNote(failed)).not.toContain("rollback");
  });

  it("keeps the original sentence for the cycles it was written about", () => {
    const curative = cycle({ trigger: "curative", report: { op: "merge_nodes" } });
    expect(noMetricsNote(curative)).toContain("one-op curative cycle");
    const rolled = cycle({ trigger: "rollback", report: { op: "rollback_cycle" } });
    expect(noMetricsNote(rolled)).toContain("rollback");
  });

  it("tells an abandoned run apart from a rollback and a curative cycle", () => {
    // Same defect one shape along: an abandoned consolidation run wears an
    // operation report, and was told "a rollback and a one-op curative cycle do
    // not compute them" — two things it is not.
    const said = noMetricsNote(
      cycle({ trigger: "manual", status: "failed", report: ABANDON_REPORT }),
    );
    expect(said).toContain("interrupted before it could write any");
    expect(said).not.toContain("curative");
  });

  it("says a running cycle has not written its metrics yet", () => {
    const running = cycle({ status: "running", report: null, finished_at: null });
    expect(noMetricsNote(running)).toContain("still running");
  });

  it("admits a report that says nothing", () => {
    expect(noMetricsNote(cycle({ report: null }))).toContain("no report");
  });
});

describe("emptyEventsNote", () => {
  it("does not claim every job ran when none did", () => {
    // Reachable two ways where no job ran at all, and the page's own headline
    // says so: "Every job ran and none of them found anything to change" is
    // false about both.
    const failed = cycle({
      status: "failed",
      report: report([], { failed: [{ job: "", error: UNGRANTED_SCOPE }] }),
    });
    const said = emptyEventsNote(failed);
    expect(said).toContain("No job ran");
    expect(said).not.toContain("Every job ran");
  });

  it("says an abandoned run died early rather than that an operation matched nothing", () => {
    // An abandoned run usually *did* write — that is the whole reason closing
    // its entry matters — so an empty list here means it never got that far.
    const said = emptyEventsNote(
      cycle({ trigger: "manual", status: "failed", report: ABANDON_REPORT }),
    );
    expect(said).toContain("interrupted before it wrote anything");
    expect(said).not.toContain("curative");
  });

  it("says a curative cycle matched nothing rather than talking about jobs", () => {
    const said = emptyEventsNote(cycle({ trigger: "curative", report: { op: "bulk_relink" } }));
    expect(said).toContain("matched nothing");
    expect(said).not.toContain("job");
  });

  it("keeps the true sentence for a consolidation cycle that really ran", () => {
    const said = emptyEventsNote(cycle({ report: report([job("link_maintenance")]) }));
    expect(said).toContain("Every job ran");
  });

  it("says a rehearsal's empty list is the point rather than a gap", () => {
    const said = emptyEventsNote(cycle({ dry_run: true, report: report(A_NIGHT, { dry_run: true }) }));
    expect(said).toContain("rehearsal");
    expect(said).not.toContain("nothing to change");
  });

  it("answers with something for a cycle carrying no report at all", () => {
    expect(emptyEventsNote(cycle({ report: null })).length).toBeGreaterThan(0);
  });
});

describe("describeEvent", () => {
  it("reads a proposed edge as something added, and names both ends", () => {
    const change = describeEvent(
      event({ op: "edge.propose", payload: { before: null, after: edgeRow() } }),
    );
    expect(change.shape).toBe("added");
    expect(change.subject).toBe("edge");
    expect(change.headline).toBe("relates_to: 4f2ad9c1… → 91bc7e2d…");
    expect(change.op).toBe("edge.propose");
    expect(change.rowId).toBe(EDGE_ID);
  });

  it("hands the endpoints out as ids so the view can name them", () => {
    // Every event a consolidation cycle produces is an edge, so a headline built
    // here out of two shortened ids made the whole diff unreadable and
    // unclickable — while the review queue, on the same build, rendered the same
    // two rows as "event sourcing → Event Sourcing".
    const change = describeEvent(event({ payload: { before: null, after: edgeRow() } }));
    expect(change.edge).toEqual({ type: "relates_to", srcId: SRC_ID, dstId: DST_ID });
    expect(change.nodeTitle).toBeNull();
  });

  it("hands a node event its own title, which the payload already carries", () => {
    const change = describeEvent(
      event({ op: "node.create", payload: { before: null, after: nodeRow() } }),
    );
    expect(change.nodeTitle).toBe("Kafka Streams");
    expect(change.edge).toBeNull();
  });

  it("lists only what a create actually landed, not every empty column", () => {
    const change = describeEvent(
      event({ payload: { before: null, after: edgeRow({ props: "{}" }) } }),
    );
    const fields = change.fields.map((field) => field.field);
    expect(fields).toContain("state");
    expect(fields).toContain("confidence");
    // `props` came back as the empty object, so there is nothing to read.
    expect(fields).not.toContain("props");
    // Neither validity column has a value on a fresh edge; `valid_from` still
    // has no writer anywhere in the system.
    expect(fields).not.toContain("valid_from");
    expect(fields).not.toContain("valid_to");
    expect(change.fields.every((field) => field.before === null)).toBe(true);
  });

  it("shows valid_to closing beside the archive, because they are two facts", () => {
    // A supersede records both — `valid_to` (when it stopped being true) and
    // `archived` (it is no longer live) — and the diff showed only the second,
    // which is half of the only write this wave gave that column.
    const change = describeEvent(
      event({
        seq: 51,
        op: "edge.supersede",
        payload: {
          before: edgeRow({ state: "active", valid_to: null }),
          after: edgeRow({ state: "archived", valid_to: "2026-07-27 02:00:09" }),
        },
      }),
    );
    expect(change.fields).toEqual([
      { field: "state", before: "active", after: "archived" },
      { field: "valid_to", before: "—", after: "2026-07-27 02:00:09" },
    ]);
  });

  it("reads an archive as a retirement and shows the state moving", () => {
    const change = describeEvent(
      event({
        seq: 44,
        op: "edge.archive",
        payload: { before: edgeRow({ state: "active" }), after: edgeRow({ state: "archived" }) },
      }),
    );
    expect(change.shape).toBe("retired");
    expect(change.fields).toEqual([{ field: "state", before: "active", after: "archived" }]);
  });

  it("reads a rollback that put a row back as a restore", () => {
    const change = describeEvent(
      event({
        op: "edge.rollback",
        payload: { before: edgeRow({ state: "archived" }), after: edgeRow({ state: "active" }) },
      }),
    );
    expect(change.shape).toBe("restored");
  });

  it("reads a rollback that deleted a created row as a removal", () => {
    // `node.rollback` covers a restore *and* a delete, which is why the shape
    // is read off the payload rather than off the op name.
    const change = describeEvent(
      event({ op: "node.rollback", payload: { before: nodeRow(), after: null } }),
    );
    expect(change.shape).toBe("removed");
    expect(change.op).toBe("node.rollback");
  });

  it("shows only the fields a node update actually moved", () => {
    const change = describeEvent(
      event({
        op: "node.update",
        payload: {
          before: nodeRow({ title: "Kafka Stream" }),
          after: nodeRow({ title: "Kafka Streams" }),
        },
      }),
    );
    expect(change.shape).toBe("changed");
    expect(change.subject).toBe("node");
    expect(change.headline).toBe("note: Kafka Streams");
    expect(change.fields).toEqual([
      { field: "title", before: "Kafka Stream", after: "Kafka Streams" },
    ]);
  });

  it("cuts a long value rather than pouring a whole node into the diff", () => {
    const long = "x".repeat(400);
    const change = describeEvent(
      event({
        op: "node.update",
        payload: { before: nodeRow({ content: "short" }), after: nodeRow({ content: long }) },
      }),
    );
    const content = change.fields.find((field) => field.field === "content");
    expect(content?.after).toHaveLength(201);
    expect(content?.after?.endsWith("…")).toBe(true);
  });

  it("reads an audit entry as recorded, with no row and no fields", () => {
    const change = describeEvent(
      event({ op: "asset.download", actor: "human:alice", payload: { token: "tok-1" } }),
    );
    expect(change.shape).toBe("recorded");
    expect(change.subject).toBe("other");
    expect(change.headline).toBe("asset.download");
    expect(change.rowId).toBeNull();
    expect(change.edge).toBeNull();
    expect(change.fields).toEqual([]);
  });

  it("keeps the seq, the actor and the timestamp the log recorded", () => {
    const change = describeEvent(event({ seq: 99, actor: "agent:builtin-gardener" }));
    expect(change.seq).toBe(99);
    expect(change.actor).toBe("agent:builtin-gardener");
    expect(change.createdAt).toBe("2026-07-27 02:00:03");
  });
});

describe("endpointLabel", () => {
  it("prefers the resolved title over the id", () => {
    expect(endpointLabel(SRC_ID, "Event Sourcing")).toBe("Event Sourcing");
  });

  it("falls back to the shortened id while the lookup has not answered", () => {
    // `undefined` is "not asked yet"; `null` is "asked, and it has no title".
    expect(endpointLabel(SRC_ID, undefined)).toBe("4f2ad9c1…");
    expect(endpointLabel(SRC_ID, null)).toBe("4f2ad9c1…");
    expect(endpointLabel(SRC_ID, "   ")).toBe("4f2ad9c1…");
  });

  it("says so plainly when the payload named no endpoint", () => {
    expect(endpointLabel(null, "anything")).toBe("?");
  });
});

describe("referencedNodeIds", () => {
  it("names every endpoint once, in reading order", () => {
    // The rule behind the title lookup: one request per node, so what is fetched
    // has to be what is on screen, deduplicated.
    const changes = [
      describeEvent(event({ payload: { before: null, after: edgeRow() } })),
      describeEvent(event({ seq: 42, payload: { before: null, after: edgeRow() } })),
      describeEvent(
        event({
          seq: 43,
          payload: { before: null, after: edgeRow({ src_id: DST_ID, dst_id: SRC_ID }) },
        }),
      ),
    ];
    expect(referencedNodeIds(changes)).toEqual([SRC_ID, DST_ID]);
  });

  it("asks for nothing on behalf of a node event or an audit entry", () => {
    // A node's title is in its own payload; an audit entry names no row at all.
    const changes = [
      describeEvent(event({ op: "node.create", payload: { before: null, after: nodeRow() } })),
      describeEvent(event({ op: "asset.download", payload: { token: "tok-1" } })),
    ];
    expect(referencedNodeIds(changes)).toEqual([]);
  });
});

describe("rowHeadlines", () => {
  it("names each row the cycle touched, newest write winning", () => {
    // What lets the rollback confirm say "Meeting 2026-07-01" instead of 32 hex
    // characters: the conflicting row is by definition one this cycle wrote, so
    // its event is on the same page and that payload carries the title.
    const changes = [
      describeEvent(
        event({
          seq: 60,
          op: "node.update",
          payload: { before: nodeRow(), after: nodeRow({ title: "Meeting 2026-07-01" }) },
        }),
      ),
      describeEvent(
        event({ seq: 41, op: "node.create", payload: { before: null, after: nodeRow() } }),
      ),
    ];
    expect(rowHeadlines(changes).get(NODE_ID)).toBe("note: Meeting 2026-07-01");
  });

  it("knows nothing about a row no event on this page named", () => {
    expect(rowHeadlines([]).get(NODE_ID)).toBeUndefined();
  });

  it("names an edge row by its endpoints once the titles are known", () => {
    // The blocker: `EventChange.headline` is pure over ids by construction — it
    // is computed before any lookup — so the rollback dialog printed
    // "relates_to: 4f2ad9c1… → 91bc7e2d…" for an edge the event list two inches
    // behind it was rendering as "event sourcing → Event Sourcing", having
    // resolved both ends already.
    const changes = [describeEvent(event({ payload: { before: null, after: edgeRow() } }))];
    const titles = new Map([
      [SRC_ID, "event sourcing"],
      [DST_ID, "Event Sourcing"],
    ]);
    expect(rowHeadlines(changes, titles).get(EDGE_ID)).toBe(
      "relates_to: event sourcing → Event Sourcing",
    );
    expect(rowHeadlines(changes, titles).get(EDGE_ID)).not.toMatch(BARE_ID);
  });

  it("falls back to the shortened endpoint id for a title still in flight", () => {
    // An absent key is "not answered yet" and a null one is "answered with
    // nothing"; both read the same way here, which is what lets the dialog
    // render before the lookups land.
    const changes = [describeEvent(event({ payload: { before: null, after: edgeRow() } }))];
    const half = new Map<string, string | null>([
      [SRC_ID, "event sourcing"],
      [DST_ID, null],
    ]);
    expect(rowHeadlines(changes, half).get(EDGE_ID)).toBe("relates_to: event sourcing → 91bc7e2d…");
    expect(rowHeadlines(changes, new Map()).get(EDGE_ID)).toBe(
      "relates_to: 4f2ad9c1… → 91bc7e2d…",
    );
  });

  it("leaves a node row named by its own payload, titles or no titles", () => {
    // A node event carries its title; there is nothing to look up and nothing
    // the map may override.
    const changes = [
      describeEvent(event({ op: "node.create", payload: { before: null, after: nodeRow() } })),
    ];
    const titles = new Map([[NODE_ID, "something else entirely"]]);
    expect(rowHeadlines(changes, titles).get(NODE_ID)).toBe("note: Kafka Streams");
  });
});

describe("verdictNodeIds", () => {
  const edgeChange = describeEvent(event({ payload: { before: null, after: edgeRow() } }));
  const nodeChange = describeEvent(
    event({ seq: 43, op: "node.create", payload: { before: null, after: nodeRow() } }),
  );

  it("asks for the endpoints of the edge rows the verdict names", () => {
    // Bounded by the verdict rather than by the cycle: a rollback dialog over a
    // 500-event cycle that reports two conflicts looks up four titles, not a
    // thousand.
    expect(verdictNodeIds([conflict()], [], [edgeChange, nodeChange])).toEqual([SRC_ID, DST_ID]);
  });

  it("asks for a blocker's dependants, which no event on the page names", () => {
    // A dependant is by definition a row the cycle did *not* write, so the event
    // list cannot name one and the dialog printed a truncated id per dependant
    // beside a created row it had named in full.
    const dependants = hexIds(2);
    const ids = verdictNodeIds([], [blocker({ dependants })], [nodeChange]);
    expect(ids).toEqual(dependants);
  });

  it("asks for nothing at all on a clean verdict", () => {
    expect(verdictNodeIds([], [], [edgeChange, nodeChange])).toEqual([]);
  });

  it("asks for each id once, however many rows named it", () => {
    const ids = verdictNodeIds(
      [conflict(), conflict({ row_id: EDGE_ID, conflicting_seq: 58 })],
      [blocker({ dependants: [SRC_ID] })],
      [edgeChange],
    );
    expect(ids).toEqual([SRC_ID, DST_ID]);
  });
});

describe("eventWindow", () => {
  it("cuts a nightly cycle into pages a browser can lay out", () => {
    // A 500-event cycle rendered whole came to 12 066 DOM nodes and 79 055 px,
    // past what Chrome will screenshot — and the server's cap means a real
    // nightly run reaches it by design.
    const first = eventWindow(500, 0, 25);
    expect(first).toMatchObject({ page: 0, pages: 20, from: 0, to: 25 });
    expect(first.label).toBe("Events 1–25 of 500");
  });

  it("ends on a partial page rather than past the list", () => {
    const last = eventWindow(53, 2, 25);
    expect(last).toMatchObject({ page: 2, pages: 3, from: 50, to: 53 });
    expect(last.label).toBe("Events 51–53 of 53");
  });

  it("clamps a page the list has shrunk past", () => {
    // The events reload after a rollback, and leaving the reader on page 12 of a
    // list that now has two would be an empty screen with no way back.
    expect(eventWindow(10, 99, 25)).toMatchObject({ page: 0, pages: 1, from: 0, to: 10 });
    expect(eventWindow(10, -3, 25).page).toBe(0);
  });

  it("has one page and says nothing when there is nothing", () => {
    expect(eventWindow(0, 0, 25)).toMatchObject({ page: 0, pages: 1, from: 0, to: 0 });
    expect(eventWindow(0, 0, 25).label).toBe("No events");
  });

  it("survives a page size of zero rather than dividing by it", () => {
    expect(eventWindow(4, 0, 0)).toMatchObject({ pages: 4, from: 0, to: 1 });
  });
});

describe("eventWindowNote", () => {
  it("says plainly that a filled window may not be all of it", () => {
    const note = eventWindowNote(500, true, 500);
    expect(note).toContain("500-event window filled");
    expect(note).toContain("may not be all of them");
  });

  it("never implies the list is complete when the window bit", () => {
    // The reader is deciding whether to reverse "everything below". The flag is
    // conservative on the server — it says the list *may* be short — so this
    // copy must claim neither completeness nor a definite remainder.
    const note = eventWindowNote(500, true, 500);
    expect(note).not.toMatch(/wrote nothing else|this is all|there are more/i);
    expect(note).toContain("may not be all of them");
  });

  it("says how many there were when the window did not bite", () => {
    expect(eventWindowNote(12, false, 500)).toBe("12 events. This cycle wrote nothing else.");
    expect(eventWindowNote(1, false, 500)).toContain("1 event.");
  });
});

describe("describeConflict", () => {
  it("names the row, what the cycle did, and what has changed it since", () => {
    // Decision C4: a human told which rows are in the way can act; one told
    // "rollback failed" cannot.
    const line = describeConflict(conflict());
    expect(line.kind).toBe("edge");
    expect(line.rowId).toBe(EDGE_ID);
    expect(line.cycleDid).toBe("event #42 (edge.propose)");
    expect(line.sinceDid).toBe("event #57 (edge.accept)");
    expect(line.sentence).toContain("would overwrite that");
  });

  it("calls the row what the page's own event list calls it", () => {
    // The dialog used to name the row by 32-hex id while the event list two
    // inches behind it already knew it as "Meeting 2026-07-01".
    const names = new Map([[EDGE_ID, "note: Meeting 2026-07-01"]]);
    const line = describeConflict(conflict(), (rowId) => names.get(rowId) ?? null);
    expect(line.name).toBe("note: Meeting 2026-07-01");
    expect(line.sentence).toContain("the edge note: Meeting 2026-07-01");
    expect(line.sentence).not.toMatch(BARE_ID);
  });

  it("falls back to the shortened id when nothing on the page named the row", () => {
    const line = describeConflict(conflict());
    expect(line.name).toBeNull();
    expect(line.sentence).toContain("e1a2b3c4…");
  });

  it("names the actor without spelling its kind prefix", () => {
    // The dialog is prose, not the log line beside it: the header greets the
    // same person as "alice", so "human:alice" here is an id printed at someone
    // whose name is already on the screen.
    const line = describeConflict(conflict());
    expect(line.who).toBe("alice");
    expect(line.sentence).toContain("by alice has changed it since");
    expect(describeConflict(conflict({ conflicting_actor: "agent:builtin-gardener" })).who).toBe(
      "the agent builtin-gardener",
    );
  });

  it("names the cycle a conflicting write belonged to, and what that means", () => {
    const line = describeConflict(conflict({ conflicting_cycle_id: ROLLBACK_CYCLE_ID }));
    expect(line.inCycle).toBe(ROLLBACK_CYCLE_ID);
    expect(line.sentence).toContain("belongs to cycle 0abcdef1…");
    expect(line.sentence).toContain("rolling that one back may clear this");
  });

  it("names no cycle when the later write belonged to none", () => {
    const line = describeConflict(conflict());
    expect(line.inCycle).toBeNull();
    expect(line.sentence).not.toContain("belongs to cycle");
  });
});

describe("describeBlocker", () => {
  it("names the row, the create that made it, what depends on it and the refusal", () => {
    // The same argument as a conflict, one refusal along: a human told which
    // rows are held down and by what can go and take those back; one told
    // "rollback failed" can only go and look.
    const line = describeBlocker(blocker());
    expect(line.kind).toBe("node");
    expect(line.rowId).toBe(NODE_ID);
    expect(line.cycleDid).toBe("event #43 (node.create)");
    expect(line.dependants).toHaveLength(2);
    expect(line.dependantCount).toBe(2);
    expect(line.reason).toContain("take those back first");
  });

  it("calls the row what the page's own event list calls it", () => {
    // A blocker's row is always one the cycle *created*, so its event is on the
    // page behind the dialog and that payload carries the title.
    const names = new Map([[NODE_ID, "note: Kafka Streams"]]);
    const line = describeBlocker(blocker(), (rowId) => names.get(rowId) ?? null);
    expect(line.name).toBe("note: Kafka Streams");
    expect(line.sentence).toContain("the node note: Kafka Streams");
    // A dependant is a row outside the cycle, so the page usually cannot name
    // one — but the sentence must never carry 32 hex characters either way.
    expect(line.sentence).not.toMatch(BARE_ID);
  });

  it("falls back to the shortened id when nothing on the page named the row", () => {
    const line = describeBlocker(blocker());
    expect(line.name).toBeNull();
    expect(line.sentence).toContain("a1a2b3c4…");
    expect(line.sentence).not.toMatch(BARE_ID);
  });

  it("names the first few dependants and counts the rest", () => {
    // A refusal naming none of them cannot be acted on; one naming four hundred
    // is not a sentence. The count is stated, so the cap hides nothing.
    const line = describeBlocker(blocker({ dependants: hexIds(8) }));
    expect(line.dependants).toHaveLength(MAX_NAMED_DEPENDANTS);
    expect(line.dependantCount).toBe(8);
    expect(line.sentence).toContain("8 rows depend on it now");
    expect(line.sentence).toContain("and 3 others");
    expect(line.sentence).not.toMatch(BARE_ID);
  });

  it("agrees with itself about a single dependant", () => {
    const line = describeBlocker(blocker({ dependants: hexIds(1) }));
    expect(line.sentence).toContain("1 row depends on it now");
  });

  it("keeps the guard's own wording, with the ids it spells taken out", () => {
    // The reason is the line the rollback actually refuses with, so the wording
    // is shown rather than paraphrased — but `service._delete_blocker` writes
    // "node <32-hex> still has 2 child node(s) (<32-hex>, <32-hex>)", and it was
    // rendered raw, directly beside a sentence that had carefully named the same
    // row. The words survive; the ids do not.
    const line = describeBlocker(blocker());
    expect(line.reason).not.toMatch(BARE_ID);
    expect(line.reason).toContain("still has 2 child node(s)");
    expect(line.reason).toContain("take those back first");
    expect(line.reason).toContain("a1a2b3c4d5e6…");
  });

  it("puts the page's own name into the guard's sentence where it has one", () => {
    const names = new Map([[NODE_ID, "note: Kafka Streams"]]);
    const line = describeBlocker(blocker(), (rowId) => names.get(rowId) ?? null);
    expect(line.reason).toContain("node note: Kafka Streams still has");
    expect(line.reason).not.toMatch(BARE_ID);
  });

  it("names a dependant the dialog has resolved, rather than truncating its id", () => {
    // A dependant is a row outside the cycle, so no event names it — but it is a
    // node, and the dialog resolves its title the way the diff resolves an
    // edge's endpoints. The created row was named and every dependant under it
    // was a truncated id on the same line.
    const dependants = hexIds(2);
    const titles = new Map([
      [NODE_ID, "note: Kafka Streams"],
      [dependants[0]!, "Consumer groups"],
    ]);
    const line = describeBlocker(blocker({ dependants }), (id) => titles.get(id) ?? null);
    expect(line.dependants[0]?.label).toBe("Consumer groups");
    // The second resolved to nothing — a grant's `agent_id` is not a node at all
    // — and the shortened id is the honest answer for it.
    expect(line.dependants[1]?.label).toBe("c1a2b3c4d5e6…");
    expect(line.sentence).toContain("Consumer groups");
    expect(line.sentence).not.toMatch(BARE_ID);
  });

  it("reads as a dependency rather than as a later write", () => {
    // A conflict is *the graph moved on*; a blocker is *something now depends on
    // what this rollback would remove*. Wording that let the two read alike
    // would tell a human the same thing had happened twice, and the two have
    // different answers: go and look at the later work, versus take the
    // dependants back first.
    const held = describeBlocker(blocker()).sentence;
    const moved = describeConflict(conflict()).sentence;
    expect(held).toContain("depend on it now");
    expect(held).toContain("taken back first");
    expect(held).not.toContain("has changed it since");
    expect(held).not.toContain("would overwrite");
    expect(moved).toContain("would overwrite");
    expect(moved).not.toContain("depend on it now");
  });
});

describe("rollbackPlan", () => {
  it("clears a rollback nothing stands in the way of, and counts it", () => {
    const plan = rollbackPlan(rollback({ reversed_events: [44, 43, 42] }));
    expect(plan.blocked).toBe(false);
    expect(plan.headline).toBe("Reversing 3 events.");
    expect(plan.conflicts).toEqual([]);
    expect(plan.blockers).toEqual([]);
  });

  it("says a clean verdict covers both refusals, not just the conflicts", () => {
    expect(rollbackPlan(rollback()).detail).toContain("nothing has been built on");
  });

  it("says which events will be skipped and why", () => {
    const plan = rollbackPlan(rollback({ reversed_events: [44], skipped_events: [40, 41] }));
    expect(plan.detail).toContain("2 audit events will be skipped");
    expect(plan.detail).toContain("no graph effect to reverse");
  });

  it("says a clean rollback is recorded as a cycle that can itself be reversed", () => {
    expect(rollbackPlan(rollback()).detail).toContain("rolled back in turn");
  });

  it("blocks on conflicts and says it is all of it or none", () => {
    const plan = rollbackPlan(
      rollback({ conflicts: [conflict(), conflict({ row_id: NODE_ID, kind: "node" })] }),
    );
    expect(plan.blocked).toBe(true);
    expect(plan.headline).toBe("2 rows moved since this cycle ran.");
    expect(plan.detail).toContain("all of it or none of it");
    expect(plan.conflicts).toHaveLength(2);
  });

  it("passes the page's row names down to every conflict", () => {
    const plan = rollbackPlan(rollback({ conflicts: [conflict()] }), () => "note: Meeting");
    expect(plan.conflicts[0]?.name).toBe("note: Meeting");
  });

  it("blocks on blockers alone, with the conflicts list empty", () => {
    // The defect this closes. A delete guard refuses a rollback exactly as
    // firmly as a conflict does, and a preflight modelling only conflicts
    // answered `conflicts: []` for a rollback that then died at apply time —
    // which is the one answer a confirm dialog must not give.
    const plan = rollbackPlan(rollback({ blockers: [blocker()] }));
    expect(plan.blocked).toBe(true);
    expect(plan.conflicts).toEqual([]);
    expect(plan.blockers).toHaveLength(1);
    expect(plan.headline).toBe("Something now depends on 1 row this cycle created.");
    expect(plan.detail).toContain("all of it or none of it");
    expect(plan.detail).toContain("taken back first");
  });

  it("does not read as clean when blockers stand and nothing has moved", () => {
    // The regression, asserted the way the dialog reads it: `blocked` is what
    // disables the confirm button, and none of the clean copy may survive.
    const clean = rollbackPlan(rollback({ reversed_events: [42] }));
    const held = rollbackPlan(rollback({ reversed_events: [42], blockers: [blocker()] }));
    expect(clean.blocked).toBe(false);
    expect(held.blocked).toBe(true);
    expect(held.headline).not.toBe(clean.headline);
    expect(held.headline).not.toContain("Reversing");
    expect(held.detail).not.toContain("the whole of it can be taken back");
    expect(held.detail).not.toContain("rolled back in turn");
  });

  it("names both when the graph has moved and grown", () => {
    // They compose, and the headline says so rather than adding them up: a
    // reader told "3 rows are in the way" has one number for two problems with
    // two different answers.
    const plan = rollbackPlan(
      rollback({
        conflicts: [conflict()],
        blockers: [blocker(), blocker({ row_id: SRC_ID })],
      }),
    );
    expect(plan.blocked).toBe(true);
    expect(plan.headline).toBe(
      "1 row moved since this cycle ran, and something now depends on 2 rows it created.",
    );
    expect(plan.detail).toContain("overwrite the later work");
    expect(plan.detail).toContain("cascade");
    expect(plan.conflicts).toHaveLength(1);
    expect(plan.blockers).toHaveLength(2);
  });

  it("passes the page's row names down to every blocker", () => {
    const plan = rollbackPlan(rollback({ blockers: [blocker()] }), () => "note: Kafka Streams");
    expect(plan.blockers[0]?.name).toBe("note: Kafka Streams");
  });
});

describe("rollbackRefusal", () => {
  it("tells the 409 race apart from the preflight's own verdict", () => {
    // The dialog checks first, so a 409 means the graph moved between the check
    // and the commit — reading "moved since this cycle ran" directly under a
    // check that had just passed would be confusing rather than informative.
    const plan = rollbackRefusal([conflict()]);
    expect(plan.blocked).toBe(true);
    expect(plan.headline).toBe("1 row moved while you were reading this.");
    expect(plan.detail).toContain("The check above passed");
    expect(plan.detail).toContain("Nothing was rolled back");
    expect(plan.conflicts[0]?.rowId).toBe(EDGE_ID);
  });

  it("names the rows the same way the preflight did", () => {
    const plan = rollbackRefusal([conflict()], () => "note: Meeting");
    expect(plan.conflicts[0]?.name).toBe("note: Meeting");
  });

  it("carries no blockers, because the 409 body has none to carry", () => {
    // Not an omission: only `RollbackConflict` renders a list into its error
    // body. A guard met mid-commit refuses as `UndoNotPossible` — one sentence,
    // no list — and reaches the dialog as an ordinary error toast.
    expect(rollbackRefusal([conflict()]).blockers).toEqual([]);
  });
});

describe("rollbackOutcome", () => {
  it("counts what actually happened and names the cycle that recorded it", () => {
    const said = rollbackOutcome(
      rollback({
        dry_run: false,
        rollback_cycle_id: ROLLBACK_CYCLE_ID,
        reversed_events: [44, 43, 42],
        restored_nodes: ["n1"],
        restored_edges: ["e1", "e2"],
        deleted_nodes: ["n2"],
        redirects_removed: ["t1"],
      }),
    );
    expect(said).toContain("3 events reversed");
    expect(said).toContain("3 rows restored");
    expect(said).toContain("1 row deleted");
    expect(said).toContain("1 merge redirect removed");
    expect(said).toContain("cycle 0abcdef1…");
  });

  it("leaves out the counts that were zero", () => {
    const said = rollbackOutcome(rollback({ dry_run: false, reversed_events: [42] }));
    expect(said).toBe("1 event reversed.");
  });
});

describe("describeRunFailure", () => {
  it("never claims the scope is gone, whichever status the wire carried", () => {
    // `POST /api/cycles` resolves `scope` through the ordinary space rule, so it
    // can refuse one — and `toast.showError` would print the server's own
    // "TypeNotFound: unknown space: research" verbatim.
    for (const wireStatus of [400, 404]) {
      const said = describeRunFailure(
        new UnknownSpaceError(RESEARCH_ID, wireStatus, `unknown space: ${RESEARCH_ID}`),
        RESEARCH,
      ).toLowerCase();
      for (const phrase of FORBIDDEN) expect(said).not.toContain(phrase);
      expect(said).toContain("would not resolve");
      expect(said).toContain("reload");
    }
  });

  it("names the scope rather than spelling the id the picker was holding", () => {
    // The panel holds a `SpaceFilter` value, which is a space **id** — so the
    // refusal used to read `nodum would not resolve the scope
    // "f73944650d5c4255a0aa5421308f62b0"`.
    const said = describeRunFailure(
      new UnknownSpaceError(RESEARCH_ID, 404, `unknown space: ${RESEARCH_ID}`),
      RESEARCH,
    );
    expect(said).toContain('the scope "research"');
    expect(said).not.toMatch(BARE_ID);
  });

  it("rewrites the gardener's ungranted scope, which is the live half of the same event", () => {
    // The refusal a default install meets on the first click of the scope
    // picker. `http_api._failure_message` exempts this package's own exceptions
    // from the storage rewrite, so `ApiError.message` is the gardener's sentence
    // verbatim — and `describeError` renders it as `type: message`, id and all,
    // straight into a toast.
    const live = new ApiError(
      403,
      "GrantNotPermitted",
      `the gardener holds no grant on space '${RESEARCH_ID}', so it cannot consolidate it: ` +
        `migration 0014 seeds builtin-gardener with 'main' and 'meta' only, and every other ` +
        `space is an explicit grant. Run: nodum grant builtin-gardener ${RESEARCH_ID} edit`,
    );
    const said = describeRunFailure(live, RESEARCH);
    expect(said).not.toMatch(BARE_ID);
    expect(said).toContain("holds no grant on the scope \"research\"");
    expect(said).toContain("nodum grant builtin-gardener 'research' edit");
    // The recorded twin says the same thing, because they are one event seen at
    // two moments.
    expect(said).toBe(describeRecordedFailure(UNGRANTED_SCOPE, RESEARCH));
  });

  it("hands every other failure to the shared classifier unchanged", () => {
    const others: unknown[] = [
      new ApiError(503, "DatabaseBusy", "database is locked"),
      new ApiError(403, "GrantNotPermitted", "open a cycle"),
      new TypeError("Failed to fetch"),
    ];
    for (const error of others) {
      expect(describeRunFailure(error, RESEARCH)).toBe(describeError(error));
    }
  });

  it("takes the ids out of a live failure it does not otherwise own", () => {
    // The same fail-closed guard as the recorded path: `describeError` renders
    // `type: message` and a service message names rows.
    const said = describeRunFailure(
      new ApiError(400, "InvalidTransition", `cycle ${CYCLE_ID} is already failed, not running`),
      RESEARCH,
    );
    expect(said).not.toMatch(BARE_ID);
    expect(said).toContain("InvalidTransition");
    expect(said).toContain("6b1f0f2c9a4d…");
  });

  it("answers a refused second cycle with a remedy this reader can carry out", () => {
    // The server's sentence ends `run: nodum cycle-abandon <id>`, which is right
    // on the surface it was written for and wrong on this one twice over: there
    // is no terminal here, and `nameIdsIn` truncates the id the command needs.
    // The journal grew the button that does it, on the running entry.
    const said = describeRunFailure(
      new ApiError(
        409,
        "CycleInProgress",
        `a consolidation cycle is already running: cycle ${CYCLE_ID}, started 2026-07-28 ` +
          `03:00:00 for human:owner. Cycles are serialised across every process that shares ` +
          `this file, so two runs cannot propose the same candidate twice, and this one was ` +
          `refused rather than queued behind it. Try again when it has finished — or, if that ` +
          `run was interrupted and will never close itself, run: nodum cycle-abandon ${CYCLE_ID}`,
      ),
      RESEARCH,
    );
    expect(said).not.toMatch(BARE_ID);
    expect(said).not.toContain("nodum cycle-abandon");
    expect(said).not.toContain("6b1f0f2c9a4d…");
    // It names the control that exists on this screen, and where to find it —
    // through the label the button itself renders, so neither can drift alone.
    expect(said).toContain(ABANDON_ACTION_LABEL);
    expect(said).toContain('"running" badge');
  });
});

describe("shortId", () => {
  it("shortens a uuid and leaves a short id alone", () => {
    expect(shortId(CYCLE_ID)).toBe("6b1f0f2c…");
    expect(shortId("main")).toBe("main");
  });
});
