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
  RollbackConflictOut,
  RollbackOut,
} from "../../api/types";
import {
  actorLabel,
  cycleCaveats,
  cycleFailures,
  cycleProvenance,
  cycleScopeIds,
  cycleScopeSpaces,
  cycleWork,
  describeConflict,
  describeEvent,
  describeRecordedFailure,
  describeRunFailure,
  emptyEventsNote,
  endpointLabel,
  eventWindow,
  eventWindowNote,
  metricRows,
  noMetricsNote,
  readConsolidationReport,
  referencedNodeIds,
  rollbackAvailability,
  rollbackOutcome,
  rollbackPlan,
  rollbackRefusal,
  rowHeadlines,
  SCOPE_CONTROL_HINT,
  shortId,
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
    ...overrides,
  };
}

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

/** A rollback response, clean unless conflicts are given. */
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
 * A recorded failure in the shape `nodum.consolidate` really writes
 * (`f"{type(failure).__name__}: {failure}"`), for the refusal a scoped cycle
 * actually meets first.
 */
const REAL_REFUSAL = `TypeNotFound: unknown space: ${RESEARCH_ID}`;

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
  // Failed outside every job: the shape that produced the blocker.
  cycle({
    status: "failed",
    scope: RESEARCH_ID,
    report: report([], { failed: [{ job: "", error: REAL_REFUSAL }] }),
  }),
  cycle({
    status: "failed",
    scope: RESEARCH_ID,
    report: report([], { failed: [{ job: "", error: HOSTILE }] }),
  }),
  // Failed inside a job, with the same text, and with a hostile job *name*.
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
    // The live case: `open_cycle` resolves `scope` through the ordinary space
    // rule, so a space archived between the picker and the run records the
    // server's own forbidden phrasing in the report.
    const lines = cycleFailures(
      cycle({
        status: "failed",
        scope: RESEARCH_ID,
        report: report([], { failed: [{ job: "", error: REAL_REFUSAL }] }),
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

  it("names a curative operation's own failure", () => {
    expect(cycleFailures(cycle({ report: { op: "merge_nodes", error: "node not found" } }))).toEqual(
      ["The operation merge_nodes failed: node not found"],
    );
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
    const said = describeRecordedFailure(REAL_REFUSAL, RESEARCH).toLowerCase();
    for (const phrase of FORBIDDEN) expect(said).not.toContain(phrase);
    expect(said).toContain("would not resolve");
  });

  it("names the scope generically when nothing has resolved it", () => {
    const said = describeRecordedFailure(REAL_REFUSAL, null);
    expect(said).toContain("the scope it named");
    expect(said).not.toMatch(BARE_ID);
  });

  it("shows any other recorded failure as the server wrote it", () => {
    expect(describeRecordedFailure("GrantNotPermitted: open a cycle", RESEARCH)).toBe(
      "GrantNotPermitted: open a cycle",
    );
    expect(describeRecordedFailure("boom", null)).toBe("boom");
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
    expect(cycleCaveats(cycle({ dry_run: true })).join(" ")).toContain("no graph event was written");
  });

  it("says a failed cycle's writes are real and stay", () => {
    // The dangerous assumption is that "failed" means "nothing happened".
    const lines = cycleCaveats(cycle({ status: "failed" })).join(" ");
    expect(lines).toContain("is real and stays");
    expect(lines).toContain("rollback");
  });

  it("composes, because a failed rehearsal is both", () => {
    expect(cycleCaveats(cycle({ status: "failed", dry_run: true }))).toHaveLength(2);
  });
});

describe("rollbackAvailability", () => {
  it("offers the action on a finished, real cycle", () => {
    expect(rollbackAvailability(cycle())).toEqual({ available: true, reason: null });
    expect(rollbackAvailability(cycle(), true)).toEqual({ available: true, reason: null });
  });

  it("refuses a running cycle, and says it has not finished writing", () => {
    const verdict = rollbackAvailability(cycle({ status: "running", finished_at: null }));
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("has not finished writing");
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
    const verdict = rollbackAvailability(cycle({ trigger: "curative" }), false);
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("no graph event");
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
      report: report([], { failed: [{ job: "", error: REAL_REFUSAL }] }),
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
      report: report([], { failed: [{ job: "", error: REAL_REFUSAL }] }),
    });
    const said = emptyEventsNote(failed);
    expect(said).toContain("No job ran");
    expect(said).not.toContain("Every job ran");
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

describe("rollbackPlan", () => {
  it("clears a rollback nothing stands in the way of, and counts it", () => {
    const plan = rollbackPlan(rollback({ reversed_events: [44, 43, 42] }));
    expect(plan.blocked).toBe(false);
    expect(plan.headline).toBe("Reversing 3 events.");
    expect(plan.conflicts).toEqual([]);
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
});

describe("shortId", () => {
  it("shortens a uuid and leaves a short id alone", () => {
    expect(shortId(CYCLE_ID)).toBe("6b1f0f2c…");
    expect(shortId("main")).toBe("main");
  });
});
