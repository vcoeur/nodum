/**
 * The dream journal's derivations, kept pure so the harness can cover them —
 * it renders no components, so everything worth asserting lives here.
 *
 * The journal reads a record it does not own and must never embellish. Four
 * rules run through every function below:
 *
 * - **The report is not the diff.** `cycles.report` says what each job examined,
 *   proposed, applied and skipped; what the cycle *changed* is the append-only
 *   event log narrowed to it. {@link cycleWork} writes a sentence from the
 *   report and {@link describeEvent} renders the log, and the two are kept
 *   apart deliberately — a journal that derived one from the other could
 *   disagree with the file.
 * - **A rehearsal is counted in the conditional.** A `dry_run` cycle writes no
 *   graph event, so its `proposed`/`applied` lists are empty by construction
 *   and the candidate counts live in each job's `detail`. Reading the empty
 *   lists would report "found nothing to change" about a run that would have
 *   proposed fourteen links.
 * - **The report is an untyped blob on the wire** (`CycleOut.report` is
 *   `dict | None`), and three different runners write it: the consolidation
 *   runner (`jobs` + `metrics`), a one-op curative cycle (`{"op": …}`), and a
 *   rollback (`{"op": "rollback_cycle", …}`). Every read here is defensive and
 *   every unrecognised shape degrades to a sentence saying so.
 * - **Nothing here judges a metric.** The five coherence metrics are rendered
 *   as before, after and delta with a direction arrow and a note saying what
 *   each measures. Whether a rise is good is a question this view is not in a
 *   position to answer — a cycle that flags duplicates *raises*
 *   `duplicate_candidates` by doing its job — so it does not colour one.
 */

import { isUnknownSpace } from "../../api/client";
import type {
  CycleMetrics,
  CycleOut,
  EventOut,
  JsonObject,
  RollbackConflictOut,
  RollbackOut,
} from "../../api/types";
import { describeError } from "../../lib";

/**
 * How many of a cycle's events the detail view asks for.
 *
 * The server's own default (`http_api.CYCLE_EVENT_LIMIT`), restated so the
 * truncation notice can name the number rather than describing an unnamed cap.
 */
export const CYCLE_EVENT_LIMIT = 500;

/** How many cycles the journal lists. */
export const CYCLE_LIST_LIMIT = 100;

/* ------------------------------------------------------------------ */
/* Small formatters                                                     */
/* ------------------------------------------------------------------ */

/** `3 links` / `1 link`. */
function plural(count: number, noun: string, pluralNoun?: string): string {
  return `${count} ${count === 1 ? noun : (pluralNoun ?? `${noun}s`)}`;
}

/** `a`, `a and b`, `a, b and c`. */
function joined(parts: readonly string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/** Sentence case for a clause list that starts mid-verb. */
function sentence(text: string): string {
  return text === "" ? "" : `${text[0]?.toUpperCase() ?? ""}${text.slice(1)}.`;
}

/**
 * Shorten an id for a dense row, keeping the full value for the `title`.
 *
 * Cycle ids and row ids are uuid-shaped; anything already short is untouched.
 */
export function shortId(id: string, keep = 8): string {
  return id.length <= keep + 2 ? id : `${id.slice(0, keep)}…`;
}

/** Read a numeric field out of an untyped detail blob. */
function numberAt(blob: JsonObject, key: string): number | null {
  const value = blob[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Read a string field out of an untyped blob. */
function stringAt(blob: JsonObject, key: string): string | null {
  const value = blob[key];
  return typeof value === "string" ? value : null;
}

/** Read an array-of-strings length out of an untyped blob. */
function lengthAt(blob: JsonObject, key: string): number {
  const value = blob[key];
  return Array.isArray(value) ? value.length : 0;
}

/* ------------------------------------------------------------------ */
/* Reading the report                                                   */
/* ------------------------------------------------------------------ */

/** One consolidation job's outcome, as much of it as the journal reads. */
export interface JobReport {
  /** The job's registered name, e.g. `link_maintenance`. */
  name: string;
  /** Items the job actually looked at. */
  examined: number;
  /** Ids of the writes it proposed — empty on a rehearsal, by construction. */
  proposed: number;
  /** Ids of the writes it applied — empty on a rehearsal. */
  applied: number;
  /** Items it deliberately left alone. */
  skipped: number;
  /** Sentences the reader needs to interpret the numbers. */
  notes: string[];
  /** Whatever else the job is about — candidate counts, the projector run. */
  detail: JsonObject;
  /** True when a scan reached its cap. */
  truncated: boolean;
  /** Set when the job raised; the other jobs still ran and still reported. */
  error: string | null;
}

/**
 * One failure out of the report's `failed` list.
 *
 * `job` is the empty string when the failure happened **outside** any job — a
 * scope the gardener holds no grant on, say. The runner writes that case with
 * no jobs at all, so a reader shown only "1 job failed" would be told nothing
 * about a cycle that never got as far as a job.
 */
export interface JobFailureReport {
  job: string;
  error: string;
}

/** A cycle whose report is the consolidation runner's. */
export interface ConsolidationReport {
  scope: string | null;
  dryRun: boolean;
  jobs: JobReport[];
  /** Jobs that raised, named with the exception that came out of them. */
  failed: JobFailureReport[];
}

/** A cycle whose report is a single operation's — a rollback, or a curative op. */
export interface OperationReport {
  /** The operation's name, e.g. `rollback_cycle`, `merge_nodes`. */
  op: string;
  /** The cycle this one took back, for `rollback_cycle`. */
  rolledBack: string | null;
  /** How many events it reversed, when the report recorded it. */
  reversed: number | null;
  /** Set when the operation raised and the cycle closed `failed`. */
  error: string | null;
}

/**
 * Read a consolidation report, or null when this cycle's report is not one.
 *
 * Tolerant by design: the wire type is `dict | None`, so every field is checked
 * rather than assumed, and a job object missing a key reads as its zero rather
 * than throwing inside a render.
 *
 * @param report `CycleOut.report`.
 */
export function readConsolidationReport(
  report: JsonObject | null | undefined,
): ConsolidationReport | null {
  if (!report || typeof report !== "object") return null;
  const jobs = report.jobs;
  if (!Array.isArray(jobs)) return null;
  const failed: JobFailureReport[] = Array.isArray(report.failed)
    ? report.failed
        .filter((entry): entry is JsonObject => entry !== null && typeof entry === "object")
        .map((entry) => ({
          job: stringAt(entry, "job") ?? "",
          error: stringAt(entry, "error") ?? "no reason recorded",
        }))
    : [];
  return {
    scope: stringAt(report, "scope"),
    dryRun: report.dry_run === true,
    jobs: jobs
      .filter((job): job is JsonObject => job !== null && typeof job === "object")
      .map((job) => ({
        name: stringAt(job, "name") ?? "unnamed job",
        examined: numberAt(job, "examined") ?? 0,
        proposed: lengthAt(job, "proposed"),
        applied: lengthAt(job, "applied"),
        skipped: lengthAt(job, "skipped"),
        notes: Array.isArray(job.notes)
          ? job.notes.filter((note): note is string => typeof note === "string")
          : [],
        detail:
          job.detail !== null && typeof job.detail === "object"
            ? (job.detail as JsonObject)
            : {},
        truncated: job.truncated === true,
        error: stringAt(job, "error"),
      })),
    failed,
  };
}

/**
 * Read a single-operation report, or null when this cycle's report is not one.
 *
 * A curative operation invoked directly opens a one-op cycle of its own
 * (`trigger='curative'`), and a rollback opens one too (`trigger='rollback'`).
 * Both write `{"op": …}` rather than a job list, which is exactly why
 * `CycleDetailOut.metrics` is `{}` for them.
 *
 * @param report `CycleOut.report`.
 */
export function readOperationReport(
  report: JsonObject | null | undefined,
): OperationReport | null {
  if (!report || typeof report !== "object") return null;
  const op = stringAt(report, "op");
  if (op === null) return null;
  return {
    op,
    rolledBack: stringAt(report, "rolled_back"),
    reversed: numberAt(report, "reversed"),
    error: stringAt(report, "error"),
  };
}

/* ------------------------------------------------------------------ */
/* The journal sentence                                                 */
/* ------------------------------------------------------------------ */

/**
 * The clauses one job contributes to the entry's sentence.
 *
 * The vocabulary is per job because the counts mean different things: a
 * `duplicate_of` edge is a candidate a human will merge or reject, a
 * `relates_to` edge is an inferred link, and an archived edge is a pruning the
 * job performed. A job this build does not know about still contributes a
 * clause, in neutral words, rather than vanishing from the sentence.
 *
 * On a rehearsal the counts come from the job's `detail` and the clause is
 * written in the conditional, because a dry run's `proposed` and `applied`
 * lists are empty by construction.
 */
function jobClauses(job: JobReport, dryRun: boolean): string[] {
  const clauses: string[] = [];

  if (job.name === "duplicate_candidates") {
    // On a rehearsal `proposed` is empty by construction and the candidate
    // count is the only record of what the job found.
    const flagged = dryRun ? (numberAt(job.detail, "matched") ?? 0) : job.proposed;
    if (flagged > 0) clauses.push(`flagged ${plural(flagged, "duplicate candidate")}`);
    return clauses;
  }

  if (job.name === "link_maintenance") {
    const inferred = dryRun ? (numberAt(job.detail, "inferred") ?? 0) : job.proposed;
    const pruned = dryRun ? (numberAt(job.detail, "prunable") ?? 0) : job.applied;
    if (inferred > 0) clauses.push(`proposed ${plural(inferred, "link")}`);
    if (pruned > 0) clauses.push(`retired ${plural(pruned, "stale edge")}`);
    return clauses;
  }

  if (job.name === "neglect_report") {
    // Report-only in both modes: deciding a claim is *stale* is judgement, so
    // this job writes nothing and the count is the same either way.
    const neglected = numberAt(job.detail, "neglected_count") ?? 0;
    const days = numberAt(job.detail, "threshold_days");
    if (neglected > 0) {
      clauses.push(
        days === null
          ? `noted ${plural(neglected, "neglected node")}`
          : `noted ${plural(neglected, "node")} untouched for ${plural(days, "day")}`,
      );
    }
    return clauses;
  }

  if (job.proposed > 0) clauses.push(`proposed ${plural(job.proposed, "row")} (${job.name})`);
  if (job.applied > 0) clauses.push(`changed ${plural(job.applied, "row")} (${job.name})`);
  return clauses;
}

/**
 * The failure clause appended to an entry's sentence, or `""` for a clean run.
 *
 * Named failures are counted together; an unnamed one is written out, because a
 * failure outside every job is the *whole* story of that cycle rather than a
 * footnote to what it did.
 */
function failureNote(failures: readonly JobFailureReport[]): string {
  const named = failures.filter((failure) => failure.job !== "");
  const unnamed = failures.filter((failure) => failure.job === "");
  const parts: string[] = [];
  if (named.length > 0) {
    parts.push(`${plural(named.length, "job")} failed: ${joined(named.map((one) => one.job))}.`);
  }
  for (const failure of unnamed) {
    parts.push(`The cycle failed before any job ran: ${failure.error}`);
  }
  return parts.length === 0 ? "" : ` ${parts.join(" ")}`;
}

/**
 * What the gardener did, as a sentence a human reads rather than a row of ids.
 *
 * This is the journal's headline and the reason the view exists: *"flagged 3
 * duplicate candidates, proposed 14 links and retired 2 stale edges"* is what a
 * human wakes to, and a table of job names and counts is not.
 *
 * @param cycle The journal entry.
 */
export function cycleWork(cycle: CycleOut): string {
  const consolidation = readConsolidationReport(cycle.report);
  if (consolidation !== null) {
    const clauses = consolidation.jobs.flatMap((job) => jobClauses(job, consolidation.dryRun));
    const failures = failureNote(consolidation.failed);
    // No jobs at all means the run failed before one could start — a scope the
    // gardener holds no grant on, say. "Ran 0 jobs and found nothing to change"
    // would be a true sentence about the wrong event.
    if (consolidation.jobs.length === 0) {
      return failures === ""
        ? "Nothing ran, and the report says nothing about why."
        : failures.trimStart();
    }
    if (clauses.length === 0) {
      const ran = plural(consolidation.jobs.length, "job");
      return consolidation.dryRun
        ? `Rehearsed ${ran} and found nothing to change.${failures}`
        : `Ran ${ran} and found nothing to change.${failures}`;
    }
    // The rehearsal's conditional is said once, at the front, rather than once
    // per clause — "would have flagged 3, would have proposed 14" is a list of
    // hypotheticals where the reader wants one sentence about one night.
    const said = consolidation.dryRun
      ? `would have ${joined(clauses)}`
      : joined(clauses);
    return `${sentence(said)}${failures}`;
  }

  const operation = readOperationReport(cycle.report);
  if (operation !== null) {
    const failure = operation.error === null ? "" : ` It failed: ${operation.error}`;
    if (operation.op === "rollback_cycle") {
      const target = operation.rolledBack === null ? "another cycle" : shortId(operation.rolledBack);
      const count =
        operation.reversed === null ? "" : ` — ${plural(operation.reversed, "event")} reversed`;
      return `Took ${target} back${count}.${failure}`;
    }
    return `One curative operation: ${operation.op}.${failure}`;
  }

  if (cycle.status === "running") return "Running now — the report lands when it closes.";
  return "No report was recorded for this cycle.";
}

/**
 * Who asked for this cycle, how, and over what.
 *
 * `triggered_by` is who *asked* and is deliberately not the actor on the events
 * inside, which is who *acted* — the gardener. Saying "by alice" about writes
 * attributed to `agent:builtin-gardener` would collapse two questions the
 * journal exists to keep apart, so this sentence names the trigger explicitly.
 *
 * @param cycle The journal entry.
 */
export function cycleProvenance(cycle: CycleOut): string {
  const scope =
    cycle.scope === null ? "across the whole file" : `confined to the space ${cycle.scope}`;
  if (cycle.trigger === "scheduled") return `Run on the nightly schedule, ${scope}.`;
  if (cycle.trigger === "rollback") return `Opened by a rollback, ${scope}.`;
  if (cycle.trigger === "curative") {
    return `Opened by a curative operation ${cycle.triggered_by} asked for, ${scope}.`;
  }
  return `Run on demand by ${cycle.triggered_by}, ${scope}.`;
}

/**
 * Every caveat that changes how this entry should be read, strongest first.
 *
 * A list rather than one line, because they compose: a failed rehearsal is both
 * a rehearsal and a failure, and a reader told only the first would draw the
 * wrong conclusion from the numbers above.
 *
 * @param cycle The journal entry.
 */
export function cycleCaveats(cycle: CycleOut): string[] {
  const caveats: string[] = [];
  if (cycle.status === "rolled_back") {
    caveats.push(
      cycle.rolled_back_by === null
        ? "This cycle has been rolled back. Nothing it wrote is still standing."
        : `This cycle was rolled back by cycle ${shortId(cycle.rolled_back_by)}. Nothing it ` +
          "wrote is still standing.",
    );
  }
  if (cycle.status === "running") {
    caveats.push("Still running: the report and the event list are incomplete until it closes.");
  }
  if (cycle.status === "failed") {
    caveats.push(
      "The cycle closed failed. Whatever it wrote before the failure is real and stays — a " +
        "rollback is what takes it back.",
    );
  }
  if (cycle.dry_run) {
    caveats.push(
      "A rehearsal: every job ran and no graph event was written, so the counts are what it " +
        "would have done.",
    );
  }
  return caveats;
}

/** Whether this cycle can be rolled back, and why not when it cannot. */
export interface RollbackAvailability {
  available: boolean;
  /** The reason it is refused, or null when it is offered. */
  reason: string | null;
}

/**
 * Whether the rollback action is offered for this cycle.
 *
 * Three of the service's own refusals (`_rollback_plan`), stated in front of
 * the button rather than met after clicking it. The fourth — a cycle that wrote
 * no graph events at all — is not decidable from the row, so it is left to the
 * dry-run preflight, which is where an undecidable refusal belongs.
 *
 * @param cycle The journal entry.
 */
export function rollbackAvailability(cycle: CycleOut): RollbackAvailability {
  if (cycle.status === "running") {
    return {
      available: false,
      reason: "A running cycle cannot be rolled back — it has not finished writing.",
    };
  }
  if (cycle.status === "rolled_back") {
    return {
      available: false,
      reason:
        cycle.rolled_back_by === null
          ? "This cycle has already been rolled back."
          : `Already rolled back by cycle ${shortId(cycle.rolled_back_by)}. Roll that one back to ` +
            "re-apply this one.",
    };
  }
  if (cycle.dry_run) {
    return {
      available: false,
      reason: "A rehearsal emitted no graph event, so there is nothing to reverse.",
    };
  }
  return { available: true, reason: null };
}

/**
 * Why a cycle could not be run, in words that never claim a space is missing.
 *
 * A cycle's `scope` names a space, so `POST /api/cycles` can be refused for one
 * — a space archived since the picker was filled is the live case — and the
 * server answers "a space that does not exist" and "a space you cannot read"
 * with the same words on purpose. Handing that refusal to `toast.showError`
 * would print the server's own *"TypeNotFound: unknown space: research"*
 * verbatim, which is the existence oracle the ambiguity exists to close.
 *
 * Everything that is not a refused space is described by the shared classifier
 * unchanged; this module does not re-derive what kind of failure something was.
 *
 * @param error The caught value.
 * @param scopeRef The scope the run named, if it named one.
 */
export function describeRunFailure(error: unknown, scopeRef: string): string {
  if (isUnknownSpace(error)) {
    return (
      `nodum would not resolve the scope "${scopeRef}". A space stops resolving once it is ` +
      "archived or renamed, so the picker may be out of date — reload the screen to see what is " +
      "there now."
    );
  }
  return describeError(error);
}

/* ------------------------------------------------------------------ */
/* The five coherence metrics                                           */
/* ------------------------------------------------------------------ */

/** How a metric's value is rendered, and in what unit its delta is measured. */
type MetricUnit = "ratio" | "count" | "days" | "number";

/** What the journal knows about one metric key. */
interface MetricDefinition {
  label: string;
  unit: MetricUnit;
  /** What it measures, so the number means something without a lookup. */
  note: string;
}

/**
 * The five metrics `nodum.consolidate` computes today, in reading order.
 *
 * Q12 listed six; the two left out need judgement (unresolved contradictions)
 * or syntheses that do not exist yet (stale syntheses), which is why the wire
 * shape is an object keyed by name rather than five columns. A key this build
 * does not know about is rendered under its own name rather than dropped — that
 * is the whole point of the object.
 */
const METRICS: Record<string, MetricDefinition> = {
  orphan_rate: {
    label: "Orphan rate",
    unit: "ratio",
    note: "Active nodes with no active edge, as a share of active nodes.",
  },
  link_density: {
    label: "Link density",
    unit: "number",
    note: "Active edges per active node.",
  },
  duplicate_candidates: {
    label: "Duplicate candidates",
    unit: "count",
    note: "Live duplicate_of edges — pairs waiting for a human to merge or reject.",
  },
  queue_age_days: {
    label: "Review queue age",
    unit: "days",
    note: "Median age of everything waiting in the review queue.",
  },
  neglect_rate: {
    label: "Neglect rate",
    unit: "ratio",
    note: "Active nodes untouched past the neglect threshold, as a share of active nodes.",
  },
};

/** Reading order for the known metrics; unknown keys follow, sorted. */
const METRIC_ORDER = Object.keys(METRICS);

/** Below this a delta is reported as no change — these are rounded floats. */
const DELTA_EPSILON = 1e-9;

/** Render one metric value in its own unit. */
function formatMetric(value: number, unit: MetricUnit): string {
  if (unit === "ratio") return `${(value * 100).toFixed(1)}%`;
  if (unit === "days") return `${value.toFixed(1)} d`;
  if (unit === "count") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  return value.toFixed(2);
}

/** Render a signed delta, in percentage points for a ratio. */
function formatDelta(delta: number, unit: MetricUnit): string {
  if (Math.abs(delta) < DELTA_EPSILON) return "no change";
  const sign = delta > 0 ? "+" : "−";
  const magnitude = Math.abs(delta);
  if (unit === "ratio") return `${sign}${(magnitude * 100).toFixed(1)} pp`;
  if (unit === "days") return `${sign}${magnitude.toFixed(1)} d`;
  if (unit === "count") {
    return `${sign}${Number.isInteger(magnitude) ? magnitude : magnitude.toFixed(2)}`;
  }
  return `${sign}${magnitude.toFixed(2)}`;
}

/** One metric, before and after, as the table renders it. */
export interface MetricRow {
  key: string;
  label: string;
  note: string;
  /** Formatted, or `—` when the snapshot did not carry this metric. */
  before: string;
  after: string;
  /** Signed and in the metric's own unit; `no change` when it did not move. */
  delta: string;
  /**
   * Which way it moved — for an arrow, never for a colour.
   *
   * Whether a rise is an improvement is not something this view can answer: a
   * cycle that flags duplicates raises `duplicate_candidates` *by working*. So
   * the direction is reported and left uninterpreted.
   */
  direction: "up" | "down" | "flat" | "unknown";
}

/**
 * Turn the before/after snapshots into the rows the metric table renders.
 *
 * `{}` — a rollback, or a one-op curative cycle — yields an empty list, which
 * is the caller's cue to say the cycle recorded none rather than to draw a
 * table of dashes.
 *
 * @param metrics `CycleDetailOut.metrics`.
 */
export function metricRows(metrics: CycleMetrics): MetricRow[] {
  const before = metrics.before ?? {};
  const after = metrics.after ?? {};
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])];
  if (keys.length === 0) return [];
  const ordered = [
    ...METRIC_ORDER.filter((key) => keys.includes(key)),
    ...keys.filter((key) => !(key in METRICS)).sort(),
  ];

  return ordered.map((key) => {
    const definition = METRICS[key] ?? { label: key, unit: "number" as MetricUnit, note: "" };
    const fromValue = before[key];
    const toValue = after[key];
    const from = typeof fromValue === "number" ? fromValue : null;
    const to = typeof toValue === "number" ? toValue : null;
    const delta = from === null || to === null ? null : to - from;
    return {
      key,
      label: definition.label,
      note: definition.note,
      before: from === null ? "—" : formatMetric(from, definition.unit),
      after: to === null ? "—" : formatMetric(to, definition.unit),
      delta: delta === null ? "—" : formatDelta(delta, definition.unit),
      direction:
        delta === null
          ? ("unknown" as const)
          : Math.abs(delta) < DELTA_EPSILON
            ? ("flat" as const)
            : delta > 0
              ? ("up" as const)
              : ("down" as const),
    };
  });
}

/* ------------------------------------------------------------------ */
/* The events, rendered as a diff                                       */
/* ------------------------------------------------------------------ */

/** What kind of row an event is about. */
export type EventSubject = "node" | "edge" | "other";

/** What the event did to that row, read off the payload rather than the op name. */
export type EventShape =
  /** The row did not exist before. */
  | "added"
  /** The row is gone after — a reversed create. */
  | "removed"
  /** It went to `archived`. */
  | "retired"
  /** It came back out of `archived`. */
  | "restored"
  /** Something else about it changed. */
  | "changed"
  /** An audit entry with no graph row at all. */
  | "recorded";

/** One field that differs between the payload's `before` and `after`. */
export interface FieldChange {
  field: string;
  /** Rendered value, or null when the side does not carry the row at all. */
  before: string | null;
  after: string | null;
}

/** One event, reduced to what the diff renders. */
export interface EventChange {
  seq: number;
  op: string;
  actor: string;
  createdAt: string;
  subject: EventSubject;
  shape: EventShape;
  /** The row named in words: `relates_to: 4f2a… → 91bc…`, or a node's title. */
  headline: string;
  /** The row's id, or null for an audit entry that names none. */
  rowId: string | null;
  fields: FieldChange[];
}

/** Node columns the diff compares, in reading order. */
const NODE_FIELDS = ["title", "state", "type_id", "space_id", "parent_id", "content", "props"];

/** Edge columns the diff compares, in reading order. */
const EDGE_FIELDS = ["state", "type_id", "src_id", "dst_id", "confidence", "props"];

/** Longest field value the diff prints before cutting it. */
const FIELD_VALUE_LIMIT = 200;

/** Render one raw column value for the diff, flattened and capped. */
function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  const text: string = typeof value === "string" ? value : JSON.stringify(value);
  const flat = text.replace(/\s+/g, " ").trim();
  // `props` is stored as a JSON *string*, so the commonest value in the file
  // arrives here as the two characters `{}`. Reading that as content puts a
  // "props: {}" row on every created edge, which is noise standing where a real
  // change should be.
  if (flat === "" || flat === "{}" || flat === "[]") return "(empty)";
  return flat.length <= FIELD_VALUE_LIMIT ? flat : `${flat.slice(0, FIELD_VALUE_LIMIT)}…`;
}

/** The `before`/`after` row out of an event payload, when it carries one. */
function payloadSide(payload: JsonObject, key: "before" | "after"): JsonObject | null {
  const side = payload[key];
  return side !== null && typeof side === "object" && !Array.isArray(side)
    ? (side as JsonObject)
    : null;
}

/** Which kind of row this event is about, from the op namespace. */
function eventSubject(op: string): EventSubject {
  if (op.startsWith("node.")) return "node";
  if (op.startsWith("edge.")) return "edge";
  return "other";
}

/** What the event did, read off the payload rather than trusting the op name. */
function eventShape(before: JsonObject | null, after: JsonObject | null): EventShape {
  if (before === null && after === null) return "recorded";
  if (before === null) return "added";
  if (after === null) return "removed";
  const wasArchived = before.state === "archived";
  const isArchived = after.state === "archived";
  if (!wasArchived && isArchived) return "retired";
  if (wasArchived && !isArchived) return "restored";
  return "changed";
}

/** Name the row an event is about, in the terms the reader is scanning for. */
function eventHeadline(
  subject: EventSubject,
  op: string,
  row: JsonObject | null,
): string {
  if (row === null) return op;
  if (subject === "edge") {
    const type = typeof row.type_id === "string" ? row.type_id : "edge";
    const src = typeof row.src_id === "string" ? shortId(row.src_id) : "?";
    const dst = typeof row.dst_id === "string" ? shortId(row.dst_id) : "?";
    return `${type}: ${src} → ${dst}`;
  }
  if (subject === "node") {
    const title = typeof row.title === "string" && row.title.trim() !== "" ? row.title : null;
    const type = typeof row.type_id === "string" ? row.type_id : "node";
    return title === null ? `${type} (untitled)` : `${type}: ${title}`;
  }
  return op;
}

/**
 * Reduce one event to the diff row the journal renders.
 *
 * The shape is derived from the payload's own `before`/`after` rather than from
 * the op name, because the op namespace is load-bearing elsewhere (the
 * projectors dispatch on it) and is therefore not free to describe what
 * happened: `node.rollback` covers a restore *and* a delete, and `edge.archive`
 * and `edge.reject` are the same thing to a reader. The op is still shown
 * verbatim beside the row, so nothing is hidden by the reduction.
 *
 * Only fields that actually differ are listed. On a create the `before` side is
 * absent, so only the non-empty `after` values are listed rather than every
 * column with a dash beside it.
 *
 * @param event One entry out of `list_events(cycle_id=…)`.
 */
export function describeEvent(event: EventOut): EventChange {
  const payload = event.payload ?? {};
  const before = payloadSide(payload, "before");
  const after = payloadSide(payload, "after");
  const subject = eventSubject(event.op);
  const shape = eventShape(before, after);
  const row = after ?? before;
  const columns = subject === "edge" ? EDGE_FIELDS : subject === "node" ? NODE_FIELDS : [];

  const fields: FieldChange[] = [];
  for (const field of columns) {
    const left = before === null ? null : renderValue(before[field]);
    const right = after === null ? null : renderValue(after[field]);
    if (left === right) continue;
    // A side that does not exist contributes nothing to read: on a create the
    // whole point is what landed, not that every column was previously absent.
    if (left === null && (right === null || right === "—" || right === "(empty)")) continue;
    if (right === null && (left === "—" || left === "(empty)")) continue;
    fields.push({ field, before: left, after: right });
  }

  return {
    seq: event.seq,
    op: event.op,
    actor: event.actor,
    createdAt: event.created_at,
    subject,
    shape,
    headline: eventHeadline(subject, event.op, row),
    rowId: row !== null && typeof row.id === "string" ? row.id : null,
    fields,
  };
}

/**
 * What to say above the event list about how complete it is.
 *
 * `events_truncated` is deliberately conservative on the server — it says the
 * list *may* be short, not that it provably is (the same rule `SubgraphOut`
 * follows) — so this copy must not claim there is definitely more. Implying the
 * list is complete when the window filled is the failure this exists to
 * prevent: a reviewer approving a rollback needs to know they may not be
 * looking at all of it.
 *
 * @param count Events actually returned.
 * @param truncated The server's flag.
 * @param limit The window that was asked for.
 */
export function eventWindowNote(count: number, truncated: boolean, limit: number): string {
  if (truncated) {
    return (
      `Showing ${plural(count, "event")} — the ${limit}-event window filled, so this may not be ` +
      "all of them. The complete record is the event log itself."
    );
  }
  return `${sentence(plural(count, "event"))} This cycle wrote nothing else.`;
}

/* ------------------------------------------------------------------ */
/* Rollback: the preflight verdict and the conflicts                    */
/* ------------------------------------------------------------------ */

/** One conflict, in the four facts a human needs to act on it. */
export interface ConflictLine {
  /** `node` or `edge`. */
  kind: string;
  /** The row that has moved. */
  rowId: string;
  /** What this cycle did to it. */
  cycleDid: string;
  /** What has happened to it since. */
  sinceDid: string;
  /** Who made that later write. */
  who: string;
  /** The cycle that later write belonged to, or null when it belonged to none. */
  inCycle: string | null;
  /** The whole of it as one sentence, for a screen reader and a copy-paste. */
  sentence: string;
}

/**
 * Render one conflict legibly: which row, what the cycle did, what changed it.
 *
 * Decision C4's whole argument is that a human told *which* rows are in the way
 * can act and one told "rollback failed" cannot — so every field the server
 * sends is used, `conflicting_cycle_id` included. A later write that belonged
 * to another cycle is still outside this one and still a conflict, and naming
 * that cycle is what tells the reader that rolling *it* back may clear the way.
 *
 * @param conflict One row out of `RollbackOut.conflicts` or the 409 body.
 */
export function describeConflict(conflict: RollbackConflictOut): ConflictLine {
  const cycleDid = `event #${conflict.cycle_event_seq} (${conflict.cycle_event_op})`;
  const sinceDid = `event #${conflict.conflicting_seq} (${conflict.conflicting_op})`;
  const inCycle = conflict.conflicting_cycle_id;
  const provenance =
    inCycle === null
      ? ""
      : ` That write belongs to cycle ${shortId(inCycle)}, so rolling that one back may clear this.`;
  return {
    kind: conflict.kind,
    rowId: conflict.row_id,
    cycleDid,
    sinceDid,
    who: conflict.conflicting_actor,
    inCycle,
    sentence:
      `This cycle's ${cycleDid} touched the ${conflict.kind} ${shortId(conflict.row_id)}; ` +
      `${sinceDid} by ${conflict.conflicting_actor} has changed it since, so reversing the ` +
      `cycle would overwrite that.${provenance}`,
  };
}

/** The dry run's verdict, as the confirm dialog reads it. */
export interface RollbackPlan {
  /** True when conflicts stand between the cycle and its reversal. */
  blocked: boolean;
  headline: string;
  detail: string;
  conflicts: ConflictLine[];
}

/**
 * Turn a dry-run rollback into the verdict a confirm dialog shows.
 *
 * The dry run is the whole reason the confirm calls the API before the human
 * commits: it opens no cycle, writes nothing, and answers the conflicts under a
 * **200**, so a blocked rollback is something the reader meets *before* acting
 * rather than as a 409 afterwards.
 *
 * @param result The `dry_run: true` response.
 */
export function rollbackPlan(result: RollbackOut): RollbackPlan {
  const conflicts = result.conflicts.map(describeConflict);
  if (conflicts.length > 0) {
    return {
      blocked: true,
      headline: `${plural(conflicts.length, "row")} moved since this cycle ran.`,
      detail:
        "A rollback is all of it or none of it, so nothing can be reversed while these stand. " +
        "Rolling back would overwrite the later work listed below.",
      conflicts,
    };
  }
  const skipped =
    result.skipped_events.length === 0
      ? ""
      : ` ${plural(result.skipped_events.length, "audit event")} will be skipped — they have no ` +
        "graph effect to reverse.";
  return {
    blocked: false,
    headline: `${sentence(`reversing ${plural(result.reversed_events.length, "event")}`)}`,
    detail:
      `Nothing outside this cycle has touched the rows it wrote, so the whole of it can be ` +
      `taken back.${skipped} The reversal is recorded as a new cycle of its own, which can be ` +
      "rolled back in turn to re-apply this one.",
    conflicts,
  };
}

/**
 * The verdict when a **real** rollback met a 409 — the graph moved mid-confirm.
 *
 * The dialog checks first, so this is the race and not the ordinary path: the
 * preflight passed and then something wrote to a row the cycle had touched
 * before the human pressed the button. Worth its own wording, because "3 rows
 * moved since this cycle ran" would be a confusing thing to read directly under
 * a check that had just said none had.
 *
 * @param conflicts The `conflicts` list out of the 409 error body.
 */
export function rollbackRefusal(conflicts: readonly RollbackConflictOut[]): RollbackPlan {
  return {
    blocked: true,
    headline: `${plural(conflicts.length, "row")} moved while you were reading this.`,
    detail:
      "The check above passed, and then something wrote to a row this cycle had touched. " +
      "Nothing was rolled back — a rollback is all of it or none of it.",
    conflicts: conflicts.map(describeConflict),
  };
}

/**
 * What a rollback that happened actually did, for the toast that reports it.
 *
 * @param result The `dry_run: false` response.
 */
export function rollbackOutcome(result: RollbackOut): string {
  const restored = result.restored_nodes.length + result.restored_edges.length;
  const deleted = result.deleted_nodes.length + result.deleted_edges.length;
  const parts: string[] = [`${plural(result.reversed_events.length, "event")} reversed`];
  if (restored > 0) parts.push(`${plural(restored, "row")} restored`);
  if (deleted > 0) parts.push(`${plural(deleted, "row")} deleted`);
  if (result.redirects_removed.length > 0) {
    parts.push(`${plural(result.redirects_removed.length, "merge redirect")} removed`);
  }
  const recorded =
    result.rollback_cycle_id === null
      ? ""
      : ` Recorded as cycle ${shortId(result.rollback_cycle_id)}, which can be rolled back in turn.`;
  return `${sentence(joined(parts))}${recorded}`;
}
