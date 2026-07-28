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
 *   missing data to paper over with dashes;
 * - a **conflict** has to name which row, what the cycle did to it and what has
 *   changed it since — decision C4's whole argument is that "rollback failed"
 *   leaves a human with nothing to do.
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError } from "../../api/client";
import { describeError } from "../../lib";
import type {
  CycleMetrics,
  CycleOut,
  EventOut,
  JsonObject,
  RollbackConflictOut,
  RollbackOut,
} from "../../api/types";
import {
  cycleCaveats,
  cycleProvenance,
  cycleWork,
  describeConflict,
  describeEvent,
  describeRunFailure,
  eventWindowNote,
  metricRows,
  readConsolidationReport,
  rollbackAvailability,
  rollbackOutcome,
  rollbackPlan,
  rollbackRefusal,
  shortId,
} from "./journal";

/** A cycle row as `GET /api/cycles` sends it. */
function cycle(overrides: Partial<CycleOut> = {}): CycleOut {
  return {
    id: "6b1f0f2c9a4d4f0e8c7b5a3d2e1f0a9b",
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
    cycle_id: "cy-1",
    created_at: "2026-07-27 02:00:03",
    ...overrides,
  };
}

/** An edge row as an event payload carries it — the raw table row. */
function edgeRow(overrides: JsonObject = {}): JsonObject {
  return {
    id: "e1a2b3c4d5e6f708192a3b4c5d6e7f80",
    src_id: "4f2ad9c1b0e34a75",
    dst_id: "91bc7e2d5a084c31",
    type_id: "relates_to",
    props: '{"job": "link_maintenance"}',
    confidence: 0.84,
    created_by: "agent:builtin-gardener",
    state: "proposed",
    created_at: "2026-07-27 02:00:03",
    ...overrides,
  };
}

/** A node row as an event payload carries it. */
function nodeRow(overrides: JsonObject = {}): JsonObject {
  return {
    id: "n1a2b3c4d5e6f708192a3b4c5d6e7f80",
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
    cycle_id: "cy-1",
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
    row_id: "e1a2b3c4d5e6f708192a3b4c5d6e7f80",
    cycle_event_seq: 42,
    cycle_event_op: "edge.propose",
    conflicting_seq: 57,
    conflicting_op: "edge.accept",
    conflicting_actor: "human:alice",
    conflicting_cycle_id: null,
    ...overrides,
  };
}

describe("readConsolidationReport", () => {
  it("reads the runner's report", () => {
    const parsed = readConsolidationReport(report(A_NIGHT, { scope: "research" }));
    expect(parsed?.scope).toBe("research");
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
    expect(readConsolidationReport({ op: "rollback_cycle", rolled_back: "cy-0" })).toBeNull();
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

  it("says a cycle that failed before any job ran did exactly that", () => {
    // The runner writes this case with `jobs: []` and one failure whose `job`
    // is the empty string — a scope the gardener holds no grant on, say. "Ran 0
    // jobs and found nothing to change" would be true about the wrong event.
    const said = cycleWork(
      cycle({
        status: "failed",
        report: report([], {
          failed: [{ job: "", error: "GrantNotPermitted: open a cycle on research" }],
        }),
      }),
    );
    expect(said).toBe(
      "The cycle failed before any job ran: GrantNotPermitted: open a cycle on research",
    );
    expect(said).not.toContain("0 jobs");
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
        report: { op: "rollback_cycle", rolled_back: "cy-0abcdef123456", reversed: 12 },
      }),
    );
    expect(said).toBe("Took cy-0abcd… back — 12 events reversed.");
  });

  it("reads a one-op curative cycle as one operation", () => {
    expect(cycleWork(cycle({ trigger: "curative", report: { op: "merge_nodes" } }))).toBe(
      "One curative operation: merge_nodes.",
    );
  });

  it("carries an operation's own failure", () => {
    const said = cycleWork(
      cycle({ status: "failed", report: { op: "merge_nodes", error: "node not found" } }),
    );
    expect(said).toContain("It failed: node not found");
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

describe("cycleProvenance", () => {
  it("keeps who asked apart from what ran", () => {
    // `triggered_by` is who *asked*; the events inside are the gardener's. The
    // sentence names the trigger so the two are never collapsed.
    expect(cycleProvenance(cycle())).toBe("Run on the nightly schedule, across the whole file.");
    expect(
      cycleProvenance(cycle({ trigger: "manual", triggered_by: "human:alice", scope: "research" })),
    ).toBe("Run on demand by human:alice, confined to the space research.");
  });

  it("says which space a scoped cycle covered, and says so when it covered all of them", () => {
    expect(cycleProvenance(cycle({ scope: "research" }))).toContain("confined to the space research");
    expect(cycleProvenance(cycle({ scope: null }))).toContain("across the whole file");
  });

  it("names the two cycles nobody asked for directly", () => {
    expect(cycleProvenance(cycle({ trigger: "rollback" }))).toContain("Opened by a rollback");
    expect(
      cycleProvenance(cycle({ trigger: "curative", triggered_by: "human:alice" })),
    ).toContain("curative operation human:alice asked for");
  });
});

describe("cycleCaveats", () => {
  it("has nothing to say about an ordinary completed cycle", () => {
    expect(cycleCaveats(cycle())).toEqual([]);
  });

  it("says a reversed cycle is not standing, and names what reversed it", () => {
    const lines = cycleCaveats(
      cycle({ status: "rolled_back", rolled_back_by: "cy-9f8e7d6c5b4a" }),
    ).join(" ");
    expect(lines).toContain("rolled back by cycle cy-9f8e7…");
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
  });

  it("refuses a running cycle, and says it has not finished writing", () => {
    const verdict = rollbackAvailability(cycle({ status: "running", finished_at: null }));
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("has not finished writing");
  });

  it("refuses one already rolled back, and points at the way to re-apply it", () => {
    const verdict = rollbackAvailability(
      cycle({ status: "rolled_back", rolled_back_by: "cy-9f8e7d6c5b4a" }),
    );
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("Roll that one back to re-apply this one");
  });

  it("refuses a rehearsal, because it emitted nothing to reverse", () => {
    const verdict = rollbackAvailability(cycle({ dry_run: true }));
    expect(verdict.available).toBe(false);
    expect(verdict.reason).toContain("nothing to reverse");
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

describe("describeEvent", () => {
  it("reads a proposed edge as something added, and names both ends", () => {
    const change = describeEvent(
      event({ op: "edge.propose", payload: { before: null, after: edgeRow() } }),
    );
    expect(change.shape).toBe("added");
    expect(change.subject).toBe("edge");
    expect(change.headline).toBe("relates_to: 4f2ad9c1… → 91bc7e2d…");
    expect(change.op).toBe("edge.propose");
    expect(change.rowId).toBe("e1a2b3c4d5e6f708192a3b4c5d6e7f80");
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
    expect(change.fields.every((field) => field.before === null)).toBe(true);
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
    expect(change.fields).toEqual([]);
  });

  it("keeps the seq, the actor and the timestamp the log recorded", () => {
    const change = describeEvent(event({ seq: 99, actor: "agent:builtin-gardener" }));
    expect(change.seq).toBe(99);
    expect(change.actor).toBe("agent:builtin-gardener");
    expect(change.createdAt).toBe("2026-07-27 02:00:03");
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
    expect(line.rowId).toBe("e1a2b3c4d5e6f708192a3b4c5d6e7f80");
    expect(line.cycleDid).toBe("event #42 (edge.propose)");
    expect(line.sinceDid).toBe("event #57 (edge.accept)");
    expect(line.who).toBe("human:alice");
    expect(line.sentence).toContain("would overwrite that");
  });

  it("names the cycle a conflicting write belonged to, and what that means", () => {
    const line = describeConflict(conflict({ conflicting_cycle_id: "cy-77ab99cc1234" }));
    expect(line.inCycle).toBe("cy-77ab99cc1234");
    expect(line.sentence).toContain("belongs to cycle cy-77ab9…");
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
      rollback({ conflicts: [conflict(), conflict({ row_id: "e2", kind: "node" })] }),
    );
    expect(plan.blocked).toBe(true);
    expect(plan.headline).toBe("2 rows moved since this cycle ran.");
    expect(plan.detail).toContain("all of it or none of it");
    expect(plan.conflicts).toHaveLength(2);
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
    expect(plan.conflicts[0]?.rowId).toBe("e1a2b3c4d5e6f708192a3b4c5d6e7f80");
  });
});

describe("rollbackOutcome", () => {
  it("counts what actually happened and names the cycle that recorded it", () => {
    const said = rollbackOutcome(
      rollback({
        dry_run: false,
        rollback_cycle_id: "cy-33cc44dd5566",
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
    expect(said).toContain("cycle cy-33cc4…");
  });

  it("leaves out the counts that were zero", () => {
    const said = rollbackOutcome(rollback({ dry_run: false, reversed_events: [42] }));
    expect(said).toBe("1 event reversed.");
  });
});

describe("describeRunFailure", () => {
  /** Copy that would resolve the server's deliberate ambiguity about a space. */
  const FORBIDDEN = ["no such space", "does not exist", "no record of", "unknown space"];

  it("never claims the scope is gone, whichever status the wire carried", () => {
    // `POST /api/cycles` resolves `scope` through the ordinary space rule, so it
    // can refuse one — and `toast.showError` would print the server's own
    // "TypeNotFound: unknown space: research" verbatim.
    for (const wireStatus of [400, 404]) {
      const said = describeRunFailure(
        new UnknownSpaceError("research", wireStatus, "unknown space: research"),
        "research",
      ).toLowerCase();
      for (const phrase of FORBIDDEN) expect(said).not.toContain(phrase);
      expect(said).toContain("would not resolve");
      expect(said).toContain("reload");
    }
  });

  it("hands every other failure to the shared classifier unchanged", () => {
    const others: unknown[] = [
      new ApiError(503, "DatabaseBusy", "database is locked"),
      new ApiError(403, "GrantNotPermitted", "open a cycle"),
      new TypeError("Failed to fetch"),
    ];
    for (const error of others) {
      expect(describeRunFailure(error, "research")).toBe(describeError(error));
    }
  });
});

describe("shortId", () => {
  it("shortens a uuid and leaves a short id alone", () => {
    expect(shortId("6b1f0f2c9a4d4f0e8c7b5a3d2e1f0a9b")).toBe("6b1f0f2c…");
    expect(shortId("main")).toBe("main");
  });
});
