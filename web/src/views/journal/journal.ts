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
 * - **No server text reaches the headline.** {@link cycleWork} is the sentence
 *   the list links by and the detail page titles itself with, and a cycle's
 *   report carries failures as *strings the server wrote* — including the
 *   gardener's own `"GrantNotPermitted: the gardener holds no grant on space
 *   '14b1…'"`, which carries a bare 32-hex id twice. So the headline counts and
 *   names failures and never quotes one; the reason belongs to
 *   {@link cycleFailures}, which routes it through
 *   {@link describeRecordedFailure}.
 * - **The failure guard fails closed.** Two message shapes have now reached a
 *   screen verbatim, so {@link describeRecordedFailure} is not a list of
 *   rewrites to extend per message: the two refusals whose *wording* is a
 *   decision get named copy, and **everything else has its ids taken out**
 *   ({@link nameIdsIn}) rather than being trusted. A third shape can carry an
 *   id, and it cannot put one on the screen.
 * - **A space is named, never spelt.** `cycles.scope` is always the **resolved
 *   id** (`open_cycle` runs it through `_resolve_space`), so every sentence
 *   about a scope takes a {@link SpaceName} the caller resolved through
 *   `nameSpace` rather than the raw reference off the row.
 */

import {
  ApiError,
  isUnknownSpace,
  recordedUngrantedScope,
  recordedUnknownSpace,
} from "../../api/client";
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
import type { SpaceName } from "../../components";
import { describeError, pageWindow } from "../../lib";
import type { PageWindow } from "../../lib";

/**
 * How many of a cycle's events the detail view asks for.
 *
 * The server's own default (`http_api.CYCLE_EVENT_LIMIT`), restated so the
 * truncation notice can name the number rather than describing an unnamed cap.
 */
export const CYCLE_EVENT_LIMIT = 500;

/** How many cycles the journal lists. */
export const CYCLE_LIST_LIMIT = 100;

/**
 * How many events one page of the diff renders.
 *
 * The server caps a cycle's event list at {@link CYCLE_EVENT_LIMIT}; the *page*
 * caps what the browser is asked to lay out. A 500-event cycle rendered whole
 * came to 12 066 DOM nodes and 79 055 px of scroll, which is not a page a
 * reviewer can read and not one Chrome can screenshot — and a nightly cycle on a
 * real graph reaches the cap by design.
 */
export const EVENT_PAGE_SIZE = 25;

/**
 * The meta space, which a cycle may name and may never usefully act on.
 *
 * `consolidate._is_curatable` excludes every node in meta — it is the vocabulary
 * and the territory, not knowledge — so a cycle scoped there examines nothing
 * and closes reporting *"Ran 4 jobs and found nothing to change"*, which is true
 * about a run that could never have found anything.
 */
export const META_SPACE_ID = "meta";

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

/** What a registered job or operation name is allowed to look like. */
const REGISTERED_NAME = /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/;

/**
 * A registered name, or a neutral word when the report carried something else.
 *
 * Job names and curative op names are registry keys (`link_maintenance`,
 * `merge_nodes`), and they are the only server-written strings {@link cycleWork}
 * prints — a job this build does not recognise still earns a clause, because
 * silently dropping one would under-report a night's work. That tolerance is
 * what makes the shape check worth having: *safe to print* is a property of the
 * shape rather than of the source, and this headline has to hold whatever comes
 * back on the wire.
 *
 * @param name The name the report carried.
 * @param fallback What to print when it is not identifier-shaped.
 */
function registeredName(name: string, fallback: string): string {
  return REGISTERED_NAME.test(name) ? name : fallback;
}

/** What an id is allowed to look like: `uuid4().hex`, and nothing with a space in it. */
const ID_SHAPE = /^[A-Za-z0-9_-]{1,64}$/;

/**
 * An id shortened for a sentence, or a neutral phrase for anything that is not
 * one.
 *
 * {@link shortId} leaves a short value whole, by design — `main` is a space id
 * and must not be turned into `main…`. That makes it the one place a *short*
 * server string can reach the headline unshortened, so the shape is checked
 * before it is printed rather than after.
 */
function shortIdOr(value: string | null, fallback: string): string {
  return value !== null && ID_SHAPE.test(value) ? shortId(value) : fallback;
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

/** A cycle whose report is a single operation's — a rollback, an abandon, or a curative op. */
export interface OperationReport {
  /**
   * The operation's name, e.g. `rollback_cycle`, `merge_nodes` — `null` for an
   * abandon, which has no operation name and does not want one.
   */
  op: string | null;
  /**
   * Whether this entry was closed by hand rather than by whatever it was doing.
   *
   * `service.abandon_cycle`'s own discriminator, and the reason every reading
   * below branches before it says the word *operation*. `close_cycle` replaces
   * whatever the interrupted run had written, so an abandoned **consolidation**
   * run arrives here wearing a one-op report's shape; read as one it came out as
   * *"One curative operation: abandon_cycle. It failed."* — wrong three ways:
   * abandoning is not a curative operation, the operation did not fail (the run
   * did), and the entry is a night's sweep rather than a single-row write.
   *
   * This used to be `op === "abandon_cycle"`, a magic string the server carried
   * **for this file alone**: the report already said `abandoned: true`, and the
   * `op` key stayed only because this reader returned null without one.
   */
  abandoned: boolean;
  /** The cycle this one took back, for `rollback_cycle`. */
  rolledBack: string | null;
  /** How many events it reversed, when the report recorded it. */
  reversed: number | null;
  /**
   * Who closed an interrupted run's entry, for an abandon.
   *
   * An actor string, named through {@link actorLabel} like every other one on
   * this screen rather than spelt.
   */
  abandonedBy: string | null;
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
 * **Two discriminators, because there are two shapes.** An abandon writes
 * `{"abandoned": true, …}` and **no** `op`: it is not an operation the cycle ran
 * and has no name to give. Keying only on `op` would read that report as no
 * report at all — an abandoned run rendered as *"No report was recorded for this
 * cycle"*, which is worse than the curative-op misreading `abandoned` was
 * introduced to fix.
 *
 * @param report `CycleOut.report`.
 */
export function readOperationReport(
  report: JsonObject | null | undefined,
): OperationReport | null {
  if (!report || typeof report !== "object") return null;
  const op = stringAt(report, "op");
  const abandoned = report.abandoned === true;
  if (op === null && !abandoned) return null;
  return {
    op,
    abandoned,
    rolledBack: stringAt(report, "rolled_back"),
    reversed: numberAt(report, "reversed"),
    abandonedBy: stringAt(report, "abandoned_by"),
    error: stringAt(report, "error"),
  };
}

/** `cycles.report["llm"]` — what a run's model use cost, and what it did not reach. */
export interface LlmReport {
  /** Whether the run was funded — a provider *and* a non-zero budget. */
  enabled: boolean;
  /** Whether a provider was configured at all. */
  available: boolean;
  /** Why not, when no provider was configured — a stable fact about the install. */
  unavailableReason: string | null;
  provider: string | null;
  modelId: string | null;
  budgetTokens: number;
  budgetSeconds: number;
  calls: number;
  /** Calls that produced no usable result — a timeout, a cut-off body. */
  failedCalls: number;
  promptTokens: number;
  outputTokens: number;
  totalTokens: number;
  /** The share of `outputTokens` spent thinking rather than writing. */
  reasoningTokens: number;
  elapsedSeconds: number;
  /** Whether a spending ceiling stopped the work — a fact about this run. */
  exhausted: boolean;
  /** Whether a stop was asked for and the run noticed it. */
  stopped: boolean;
  /** One entry per job the run declared a budget for, including one that never called. */
  perJob: { job: string; calls: number; promptTokens: number; outputTokens: number }[];
}

/**
 * Read `cycles.report["llm"]` — the cost object the abstraction job's run filed
 * under `agent.REPORT_KEY` (A1) — or null when the cycle's report has none.
 *
 * A cycle with no LLM job ran files no `llm` key at all; the wire type is
 * `dict | None`, so every field is checked rather than assumed, exactly as
 * {@link readConsolidationReport} reads its own blob.
 *
 * @param report `CycleOut.report`.
 */
export function readLlmReport(report: JsonObject | null | undefined): LlmReport | null {
  if (!report || typeof report !== "object") return null;
  const raw = report.llm;
  if (raw === null || typeof raw !== "object") return null;
  const llm = raw as JsonObject;
  const perJob = Array.isArray(llm.per_job)
    ? llm.per_job
        .filter((entry): entry is JsonObject => entry !== null && typeof entry === "object")
        .map((entry) => ({
          job: stringAt(entry, "job") ?? "unnamed job",
          calls: numberAt(entry, "calls") ?? 0,
          promptTokens: numberAt(entry, "prompt_tokens") ?? 0,
          outputTokens: numberAt(entry, "output_tokens") ?? 0,
        }))
    : [];
  return {
    enabled: llm.enabled === true,
    available: llm.available === true,
    unavailableReason: stringAt(llm, "unavailable_reason"),
    provider: stringAt(llm, "provider"),
    modelId: stringAt(llm, "model_id"),
    budgetTokens: numberAt(llm, "budget_tokens") ?? 0,
    budgetSeconds: numberAt(llm, "budget_seconds") ?? 0,
    calls: numberAt(llm, "calls") ?? 0,
    failedCalls: numberAt(llm, "failed_calls") ?? 0,
    promptTokens: numberAt(llm, "prompt_tokens") ?? 0,
    outputTokens: numberAt(llm, "output_tokens") ?? 0,
    totalTokens: numberAt(llm, "total_tokens") ?? 0,
    reasoningTokens: numberAt(llm, "reasoning_tokens") ?? 0,
    elapsedSeconds: numberAt(llm, "elapsed_seconds") ?? 0,
    exhausted: llm.exhausted === true,
    stopped: llm.stopped === true,
    perJob,
  };
}

/**
 * One acceptance rate the curation job computed — `detail["acceptance"]`'s
 * row (L4).
 *
 * `kind` is the row state the rate was counted over — `edge` (an edge type),
 * `node` or `version` (a node type, for node and update proposals) — and
 * `rate` is accepted / (accepted + rejected), rounded by the runner to six
 * decimals. Only pairs with history appear: a cold-start proposer has no row.
 */
export interface AcceptanceEntry {
  proposer: string;
  kind: string;
  type: string;
  accepted: number;
  rejected: number;
  rate: number;
}

/**
 * Read the curation job's `detail["acceptance"]` — the per-(proposer, type)
 * rates the cycle computed, as the delta basis the journal renders.
 *
 * Defensive like every read of this untyped-at-the-edges wire: a row missing
 * a number reads as its zero, and a malformed entry is dropped rather than
 * thrown, so a report written by a different runner can never crash the
 * detail page.
 *
 * @param detail The curation job's `detail` blob.
 */
export function readAcceptance(detail: JsonObject | null | undefined): AcceptanceEntry[] {
  if (!detail || typeof detail !== "object") return [];
  const raw = detail.acceptance;
  if (!Array.isArray(raw)) return [];
  const entries: AcceptanceEntry[] = [];
  for (const row of raw.filter(
    (entry): entry is JsonObject => entry !== null && typeof entry === "object",
  )) {
    const proposer = stringAt(row, "proposer");
    const kind = stringAt(row, "kind");
    const type = stringAt(row, "type");
    const accepted = numberAt(row, "accepted");
    const rejected = numberAt(row, "rejected");
    const rate = numberAt(row, "rate");
    if (proposer === null || kind === null || type === null) continue;
    entries.push({
      proposer,
      kind,
      type,
      accepted: accepted ?? 0,
      rejected: rejected ?? 0,
      rate: rate ?? 0,
    });
  }
  return entries;
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

  if (job.name === "curation") {
    // The curation job's two records are not both proposals: convention nodes
    // are (and land in `proposed`), annotations are not — they are rows on the
    // annotations table, so the count is the job's own `detail` in both modes
    // (ids on a run, would-be entries on a rehearsal, same length either way).
    const conventions = dryRun ? lengthAt(job.detail, "conventions") : job.proposed;
    const annotated = lengthAt(job.detail, "annotations");
    if (conventions > 0) {
      clauses.push(`learned ${plural(conventions, "acceptance convention")}`);
    }
    if (annotated > 0) {
      clauses.push(`annotated ${plural(annotated, "queue item")}`);
    }
    return clauses;
  }

  const named = registeredName(job.name, "an unnamed job");
  if (job.proposed > 0) clauses.push(`proposed ${plural(job.proposed, "row")} (${named})`);
  if (job.applied > 0) clauses.push(`changed ${plural(job.applied, "row")} (${named})`);
  return clauses;
}

/**
 * The failure clause appended to an entry's sentence, or `""` for a clean run.
 *
 * **It counts and names; it never quotes.** A failure's `error` is a string the
 * server wrote (`f"{type(failure).__name__}: {failure}"`), and splicing one into
 * the headline put *"The cycle failed before any job ran: TypeNotFound: unknown
 * space: 909a3060…"* into an `<h1>` and into the list's link text — the phrasing
 * the never-say-it-does-not-exist rule names outright, plus a bare 32-hex id.
 * The reason is not dropped: it moves to {@link cycleFailures}, one line below,
 * where the space-safe copy rules apply to it.
 *
 * Named failures are counted together; an unnamed one still gets its own clause,
 * because a failure outside every job is the *whole* story of that cycle rather
 * than a footnote to what it did.
 */
function failureNote(failures: readonly JobFailureReport[]): string {
  const named = failures.filter((failure) => failure.job !== "");
  const unnamed = failures.filter((failure) => failure.job === "");
  const parts: string[] = [];
  if (named.length > 0) {
    const names = named.map((one) => registeredName(one.job, "an unnamed job"));
    parts.push(`${plural(named.length, "job")} failed: ${joined(names)}.`);
  }
  if (unnamed.length > 0) parts.push("The cycle failed before any job ran.");
  return parts.length === 0 ? "" : ` ${parts.join(" ")}`;
}

/**
 * What the gardener did, as a sentence a human reads rather than a row of ids.
 *
 * This is the journal's headline and the reason the view exists: *"flagged 3
 * duplicate candidates, proposed 14 links and retired 2 stale edges"* is what a
 * human wakes to, and a table of job names and counts is not.
 *
 * Nothing the *server* wrote appears in it — see {@link failureNote}. Every
 * string below is this module's own, over counts and registered job names, so
 * the headline is safe whatever comes back on the wire, which is the property
 * `journal.test.ts`'s `FORBIDDEN` guard pins branch by branch.
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
    // An abandoned run is not an operation the way a merge is: the report says
    // how the *entry* was closed, not what the cycle did. Its own writes are the
    // event list below, which is why the sentence points at them.
    if (operation.abandoned) {
      return (
        "Interrupted, and never finished. Its entry was closed by hand so that what it had " +
        "already written could be rolled back."
      );
    }
    // The *reason* belongs to `cycleFailures`, for the reason `failureNote`
    // gives: `report["error"]` is `str(exc)` and this is an `<h1>`.
    const failure = operation.error === null ? "" : " It failed.";
    if (operation.op === "rollback_cycle") {
      const target = shortIdOr(operation.rolledBack, "another cycle");
      const count =
        operation.reversed === null ? "" : ` — ${plural(operation.reversed, "event")} reversed`;
      return `Took ${target} back${count}.${failure}`;
    }
    // `op` is null only for an abandon, which returned above.
    return `One curative operation: ${registeredName(operation.op ?? "", "unnamed")}.${failure}`;
  }

  if (cycle.status === "running") return "Running now — the report lands when it closes.";
  return "No report was recorded for this cycle.";
}

/**
 * Why this cycle failed, one line per failure, said without naming a space.
 *
 * Split out of {@link cycleWork} deliberately, and this is the half where the
 * copy rules live. A cycle's failures are strings the server wrote, and the
 * first one a scoped run actually produces is `open_cycle`'s own refusal of an
 * unresolvable `scope` — *"TypeNotFound: unknown space: 909a…"*, verbatim. So
 * every recorded failure goes through {@link describeRecordedFailure}, and the
 * headline above carries only counts and job names.
 *
 * Rendered beneath the sentence in both the list and the detail page, so nothing
 * is hidden by the split — the reason is one line down, not one click away.
 *
 * @param cycle The journal entry.
 * @param scope The cycle's scope resolved through `nameSpace`, or null when the
 *   cycle named none (or nothing has resolved it yet).
 * @returns One sentence per recorded failure; empty for a cycle that had none.
 */
export function cycleFailures(cycle: CycleOut, scope: SpaceName | null = null): string[] {
  const consolidation = readConsolidationReport(cycle.report);
  if (consolidation !== null) {
    return consolidation.failed.map((failure) =>
      failure.job === ""
        ? `The cycle failed before any job ran: ${describeRecordedFailure(failure.error, scope)}`
        : `The job ${failure.job} raised: ${describeRecordedFailure(failure.error, scope)}`,
    );
  }
  const operation = readOperationReport(cycle.report);
  if (operation !== null && operation.abandoned) {
    // The server's own sentence says the same thing, under a prefix that is
    // false — *"The operation abandon_cycle failed"*. The operation succeeded;
    // the run it closed is what never finished.
    const by =
      operation.abandonedBy === null
        ? ""
        : ` ${actorLabel(operation.abandonedBy)} closed its journal entry.`;
    return [
      `This run was interrupted and never closed itself.${by} Nothing it wrote was undone — ` +
        "rolling the cycle back is what takes those writes back.",
    ];
  }
  // A *named* operation that failed. `op` is null only for an abandon, which
  // returned above with its own line — and narrowing here is what keeps a bare
  // `null` out of a sentence rather than trusting that it did.
  if (operation !== null && operation.op !== null && operation.error !== null) {
    return [`The operation ${operation.op} failed: ${describeRecordedFailure(operation.error, scope)}`];
  }
  return [];
}

/**
 * Name a principal the way the rest of the interface does: without its kind
 * prefix.
 *
 * `triggered_by` is an actor string — `human:owner`, `agent:builtin-gardener` —
 * because that is what the event log stores and what makes a write attributable.
 * The header greets the same person as *owner*, so a sentence reading *"Run on
 * demand by human:owner"* prints an id at someone who has a name on screen two
 * inches above it. The kind is not lost, it is said in words.
 *
 * @param actor An actor string, or anything else (returned unchanged).
 */
export function actorLabel(actor: string): string {
  const separator = actor.indexOf(":");
  if (separator <= 0) return actor;
  const kind = actor.slice(0, separator);
  const id = actor.slice(separator + 1);
  if (id === "") return actor;
  if (kind === "human") return id;
  if (kind === "agent") return `the agent ${id}`;
  return actor;
}

/**
 * How a sentence says what territory a cycle covered.
 *
 * `cycles.scope` is the **resolved space id** and never the reference a human
 * typed — `open_cycle` runs it through `_resolve_space` before the row is
 * written — so this takes the resolution rather than the row's own string. An
 * archived scope is named *and* marked: a cycle can perfectly well have run over
 * a space retired since, and a bare name would say nothing about why no picker
 * offers it now.
 */
function scopeClause(scope: string | null, name: SpaceName | null): string {
  if (scope === null) return "across the whole file";
  if (name === null) return `confined to the space ${scope}`;
  const mark = name.kind === "archived" ? ", archived since" : "";
  return `confined to the space ${name.label}${mark}`;
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
 * @param scope `cycle.scope` resolved through `nameSpace`; null when the cycle
 *   named no space, or while the space list has not answered.
 */
export function cycleProvenance(cycle: CycleOut, scope: SpaceName | null = null): string {
  const where = scopeClause(cycle.scope, scope);
  if (cycle.trigger === "scheduled") return `Run on the nightly schedule, ${where}.`;
  if (cycle.trigger === "rollback") return `Opened by a rollback, ${where}.`;
  if (cycle.trigger === "curative") {
    return `Opened by a curative operation ${actorLabel(cycle.triggered_by)} asked for, ${where}.`;
  }
  return `Run on demand by ${actorLabel(cycle.triggered_by)}, ${where}.`;
}

/**
 * Every space id this set of journal entries names, for the archived-space read.
 *
 * A cycle's scope outlives the listing that names it: `GET /api/spaces` is
 * active-only, and a cycle that ran over `research` goes on reporting that id
 * long after the space was retired. `components/unresolvedSpaceIds` decides
 * whether the lazy archived read fires; this is what it is handed.
 *
 * @param cycles The entries on screen.
 */
export function cycleScopeIds(cycles: readonly CycleOut[]): string[] {
  return [...new Set(cycles.map((cycle) => cycle.scope).filter((scope): scope is string => scope !== null))];
}

/**
 * The spaces a cycle may sensibly be scoped to.
 *
 * `meta` is dropped, and not for tidiness: `consolidate._is_curatable` excludes
 * every node in it, so a cycle scoped there examines nothing whatever the file
 * holds and closes reporting *"Ran 4 jobs and found nothing to change"* — a
 * sentence that reads as a clean night and is really a control that could never
 * have done anything. A picker offering it is offering a guaranteed no-op.
 *
 * `main` stays: it is ordinary territory and the default write target.
 *
 * @param spaces The active space list, or null while it is unknown.
 * @returns The same list without meta, or null when the list is unknown.
 */
export function cycleScopeSpaces<T extends NodeOut>(spaces: readonly T[] | null): T[] | null {
  return spaces === null ? null : spaces.filter((space) => space.id !== META_SPACE_ID);
}

/**
 * What the run panel's scope control says it does.
 *
 * It is a `SpaceFilter`, and everywhere else that component narrows a **read**
 * and promises *"it never widens what you can see"*. Here the same control
 * decides what the gardener **writes to** — the inherited tooltip described the
 * opposite of what the button beside it was about to do. The string lives here
 * rather than inline in the panel so the claim can be asserted: the harness
 * renders no components, so a sentence inside one is a sentence nothing checks.
 */
export const SCOPE_CONTROL_HINT =
  "Confine the cycle to one space. This decides what the gardener acts on, not what you can see.";

/**
 * Every caveat that changes how this entry should be read, strongest first.
 *
 * A list rather than one line, because they compose: a failed rehearsal is both
 * a rehearsal and a failure, and a reader told only the first would draw the
 * wrong conclusion from the numbers above.
 *
 * **Composing is exactly why each one branches**, the way {@link emptyEventsNote}
 * and {@link noMetricsNote} already do. Emitted unconditionally, the two that
 * compose most often contradicted the page around them: *"every job ran"* sat
 * under an `<h1>` reading *"The cycle failed before any job ran"*, and *"whatever
 * it wrote before the failure is real and stays — a rollback is what takes it
 * back"* sat beside a **disabled** rollback button explaining that a rehearsal
 * emitted nothing to reverse. A caveat exists to correct a misreading; one that
 * contradicts the headline creates one.
 *
 * @param cycle The journal entry.
 */
export function cycleCaveats(cycle: CycleOut): string[] {
  const caveats: string[] = [];
  const consolidation = readConsolidationReport(cycle.report);
  // The runner writes `jobs: []` only for a cycle that died outside every job —
  // the same shape `noMetricsNote` and `emptyEventsNote` read. Note it does not
  // prove *nothing was written*: a `BaseException` out of `_run_jobs` (a
  // Ctrl-C mid-run) loses the outcomes of jobs that had already written.
  const jobsRan = consolidation !== null && consolidation.jobs.length > 0;

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
      cycle.dry_run
        ? // A rehearsal emits no graph event whether it finished or not, so
          // there is nothing standing and nothing for a rollback to take back —
          // which is what the disabled button beside this says.
          "The rehearsal failed. It emitted no graph event either way, so nothing is standing " +
            "and there is nothing to reverse — what the report shows is only as far as it got."
        : cycle.stop_requested
          ? // The question the kill switch exists to keep answerable, asked at
            // exactly the moment it is asked for real: a human reading a
            // `failed` cycle the next morning needs to know whether the operator
            // stopped that run or the process died. `failed` is the one status a
            // stopped run has to close into, so the status cannot answer it and
            // the stop record is what does.
            "The cycle closed failed, and a stop was asked for on it — a stopped run closes " +
              "itself failed, so this is what being stopped looks like rather than a fault. " +
              "Whatever it wrote before it stopped is real and stays; a rollback is what takes " +
              "it back."
          : "The cycle closed failed. Whatever it wrote before the failure is real and stays — a " +
              "rollback is what takes it back.",
    );
  }
  if (cycle.stop_requested && cycle.status !== "failed") {
    caveats.push(
      cycle.status === "running"
        ? "A stop has been asked for. The entry stays running until the run notices, and nothing " +
            "about a stop reverses anything it has already written. " +
            STOP_IS_NOTICED_AT_A_MODEL_CALL
        : cycle.status === "completed"
          ? // Reachable today, and the reading it needs is the opposite of
            // alarming: a stop is noticed at the *next* check, and the
            // deterministic jobs make none, so the run finishes normally.
            "A stop was asked for on this run and it completed anyway — a stop is noticed at the " +
              "next check, and a run with none left to make finishes."
          : "A stop was asked for on this run before it closed.",
    );
  }
  if (cycle.dry_run) {
    caveats.push(
      jobsRan
        ? "A rehearsal: every job ran and no graph event was written, so the counts are what it " +
            "would have done."
        : // No job outcome was recorded — it failed outside every job, or it has
          // not closed yet. Claiming "every job ran" here is the contradiction
          // this branch exists to close.
          "A rehearsal: no graph event was written, and no job has reported — so there are no " +
            "counts here saying what it would have done.",
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
 * All four of the service's own refusals (`_rollback_plan`), stated in front of
 * the button rather than met after clicking it. The fourth — a cycle that wrote
 * no graph event at all — is not decidable from the row, which is why it takes
 * an argument: the detail page has read the cycle's events and can answer it,
 * the list has not. A one-op curative cycle that matched nothing (a `bulk_relink`
 * whose selector hit no edge) is exactly that case, and it used to be offered a
 * live button whose preflight then refused with *"InvalidTransition: wrote no
 * graph events"* — a refusal the page already had everything it needed to state.
 *
 * @param cycle The journal entry.
 * @param wroteGraphEvents Whether the cycle emitted any `node.*` / `edge.*`
 *   event. `null` means unknown — no event list has been read, or the window
 *   filled and carried only audit entries — and leaves the action offered, since
 *   an undecidable refusal belongs to the dry-run preflight.
 */
export function rollbackAvailability(
  cycle: CycleOut,
  wroteGraphEvents: boolean | null = null,
): RollbackAvailability {
  if (cycle.status === "running") {
    return {
      available: false,
      // It names the way out. A cycle a crash left `running` is not going to
      // finish, and `undo` refuses every event it stamped — so a reader given
      // this sentence and nothing else was told the writes were unreachable and
      // shown no door. `abandon` is the door, and it is on this screen.
      reason:
        "A running cycle cannot be rolled back — it has not finished writing. If nothing is " +
        "going to finish it, abandon it first: that closes the entry and is what makes what it " +
        "wrote reversible.",
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
  if (wroteGraphEvents === false) {
    const operation = readOperationReport(cycle.report);
    // Why it wrote nothing depends on what it was. A one-op curative cycle
    // matched nothing; an abandoned run died before it got that far — and
    // telling the second reader about a curative operation describes something
    // their cycle is not.
    const because =
      operation === null
        ? ""
        : operation.abandoned
          ? " — it was interrupted before it wrote anything"
          : " — a curative operation that matched nothing writes none";
    return {
      available: false,
      reason: `This cycle wrote no graph event${because}, so there is nothing to reverse.`,
    };
  }
  return { available: true, reason: null };
}

/**
 * Whether the abandon action is offered for this cycle, and why not when it is
 * not.
 *
 * `service.abandon_cycle` refuses anything that is not `running`, in those
 * words: a cycle that has said how it ended is not abandoned, and re-closing it
 * would overwrite the record of what actually happened. That is the *only*
 * refusal, and it is decidable from the row — so it is stated in front of the
 * button rather than met after clicking it, exactly as
 * {@link rollbackAvailability} states its own four.
 *
 * @param cycle The journal entry.
 */
export function abandonAvailability(cycle: CycleOut): RollbackAvailability {
  if (cycle.status === "running") return { available: true, reason: null };
  return {
    available: false,
    // `status` is a closed vocabulary the service writes (`completed`,
    // `failed`, `rolled_back`), not free text off the wire, so it is safe to
    // print — and it is the fact that decides the refusal.
    reason:
      `Only a cycle still running can be abandoned, and this one closed ${cycle.status}. A ` +
      "cycle that has said how it ended is not abandoned; re-closing it would overwrite that.",
  };
}

/**
 * What the abandon confirm says it is about to do — and what it is not.
 *
 * Split out of the dialog because the harness renders no components, so a claim
 * made inside one is a claim nothing checks; and every line here has to be
 * something `service.abandon_cycle` actually delivers.
 *
 * The dangerous misreading is that abandoning *cancels* the run or takes its
 * writes back. It does neither. It closes the journal row as `failed` with a
 * report naming who closed it, and that is the whole of it — which is precisely
 * what unlocks the rollback, since `_rollback_plan` refuses a cycle that has not
 * closed and `undo` refuses every event a cycle stamped.
 */
/**
 * The label on the button that closes an interrupted cycle.
 *
 * Exported and owned here rather than written into the detail view's JSX,
 * because {@link CYCLE_ALREADY_RUNNING} and {@link STOP_CONFIRM} both send a
 * reader to look for it — copy that names a control by a string nothing ties to
 * the control is copy that goes stale the first time somebody rewords a button.
 */
export const ABANDON_ACTION_LABEL = "Abandon this cycle";

export const ABANDON_CONFIRM: readonly string[] = [
  "Abandoning closes this journal entry as failed and records who closed it. It does not " +
    "reverse anything: whatever the run wrote before it stopped is still in the graph.",
  "Closing the entry is what makes those writes reversible — a rollback refuses a cycle that " +
    "has not finished, and undo refuses every event a cycle stamped. Roll the cycle back " +
    "afterwards to take them back.",
  "Only do this for a run nothing is going to finish: a server killed mid-cycle, a power cut, " +
    "or a shutdown that cancelled the nightly task. Nothing here stops a cycle that is genuinely " +
    "still running, and one that is would go on writing into an entry that says it failed.",
];

/**
 * What an abandoned cycle's toast says happened.
 *
 * @param cycle The cycle row as the route answered with it — now `failed`.
 */
export function abandonOutcome(cycle: CycleOut): string {
  return (
    `Cycle ${shortId(cycle.id)} is closed as ${cycle.status}. Nothing it wrote has changed; it ` +
    "can be rolled back now."
  );
}

/**
 * The label on the button that asks a running cycle to stop.
 *
 * Exported and owned here for the reason {@link ABANDON_ACTION_LABEL} is: it
 * sits beside that button on the same entry, and the two are one keystroke apart
 * for very different acts — so the copy that tells them apart has to be able to
 * name each control by the same string the control renders.
 */
export const STOP_ACTION_LABEL = "Stop this cycle";

/**
 * What a stop actually gets, said in one place because four surfaces say it.
 *
 * `AgentRun.chat` checks the switch immediately before a provider call and that
 * is the only check that exists: the five deterministic consolidation jobs make
 * no provider call, so a stop recorded against one of those runs is kept on the
 * entry and the run finishes — to `completed`, if nothing else went wrong. The
 * abstraction job (5b-ii's first) is the exception: it reaches the model
 * through that same `AgentRun.chat`, so a stop recorded against its own run is
 * obeyed at the next call.
 *
 * **Three of the four places a human met this control said otherwise**, and each
 * said it in its own words: the button's tooltip offered to *"ask this run to
 * wind down and close its own entry"*, {@link RUNNING_ACTIONS_HINT} said the run
 * "closes its own entry when it notices", and {@link stopOutcome} promised "the
 * entry closes when the run notices". Only {@link STOP_CONFIRM} — the one screen
 * a human reads *after* deciding — carried the caveat. The code was right and
 * the copy was wrong, which is this whole defect class: the fix is the sentence,
 * not a check wired into the deterministic jobs (that is 5b-ii, and
 * `tests/test_consolidate.py::test_the_deterministic_runner_consults_no_stop_
 * switch_and_the_copy_says_so` is what failed the day the abstraction job
 * landed — its docstring says exactly that, and the test now names the
 * exception instead).
 *
 * One exported constant rather than four wordings, for the reason
 * {@link STOP_ACTION_LABEL} is one: a caveat repeated in four voices is a caveat
 * that stops being true in three of them.
 */
export const STOP_IS_NOTICED_AT_A_MODEL_CALL =
  "What checks the switch today is a model call, and the deterministic jobs make none — so a " +
  "run of those finishes even after you stop it, with the stop kept on the entry.";

/**
 * The stop button's own tooltip, which used to promise a wind-down.
 *
 * Exported rather than written into the button's JSX for the reason
 * {@link STOP_CONFIRM} is an array: the harness renders no components, so a
 * claim made inside one is a claim nothing checks — and this one was wrong for
 * exactly as long as nothing checked it.
 */
export const STOP_ACTION_HINT =
  `Record a stop on this entry. ${STOP_IS_NOTICED_AT_A_MODEL_CALL}`;

/**
 * The line under the two controls a `running` entry offers, telling them apart.
 *
 * They are the only two places in this app where one screen offers two
 * irreversible verbs at once, they look alike, and **nothing on this page can
 * tell a human which one they want** — whether the process behind a `running`
 * row is alive is not a fact the server has. So the screen states the two
 * situations instead of implying a preference by ordering or styling, and says
 * the thing both of them are *not*: neither reverses a write.
 *
 * Named through the two action constants rather than by repeating their words,
 * so a reworded button cannot leave this sentence pointing at nothing.
 *
 * It used to end the first situation on *"the run closes its own entry when it
 * notices"*, which is a wind-down this system does not deliver for the only
 * cycles it ships today — see {@link STOP_IS_NOTICED_AT_A_MODEL_CALL}, which is
 * now the sentence after it.
 */
export const RUNNING_ACTIONS_HINT =
  `Two different situations. "${STOP_ACTION_LABEL}" is for a run that is going right now: it ` +
  "records the instruction, and the run closes its own entry at its next check. " +
  `${STOP_IS_NOTICED_AT_A_MODEL_CALL} ` +
  `"${ABANDON_ACTION_LABEL}" is for one that is never going to finish — a server killed ` +
  "mid-cycle, a power cut — and closes the entry from outside. Neither reverses anything the run " +
  "wrote; rolling the cycle back afterwards is what does.";

/**
 * Whether the stop action is offered for this cycle, and why not when it is not.
 *
 * Two refusals, and only the first is the service's. `service.request_stop`
 * refuses anything that is not `running` — a cycle that has said how it ended
 * has nothing left to obey an instruction — and that is decidable from the row,
 * so it is stated in front of the button exactly as {@link abandonAvailability}
 * states its own.
 *
 * The second is this screen's. Asking twice is deliberately a **no-op** in the
 * service rather than an error, so that a human who presses twice is never left
 * doubting the first press — which is the right server behaviour and the wrong
 * button. Re-offering a control that provably changes nothing would be the
 * screen's own version of the same ambiguity, so once a stop is recorded the
 * action gives way to the record of who asked: `stopRecord` renders it beside
 * this reason, which is more than a second press would have told anybody.
 *
 * @param cycle The journal entry.
 */
export function stopAvailability(cycle: CycleOut): RollbackAvailability {
  if (cycle.status !== "running") {
    return {
      available: false,
      // `status` is the service's closed vocabulary rather than free text off
      // the wire, so it is safe to print — and it is the fact that decides this.
      reason:
        `Only a cycle still running can be told to stop, and this one closed ${cycle.status}. A ` +
        "stop is an instruction to a live run; a cycle that has said how it ended has nothing " +
        "left to obey it.",
    };
  }
  if (cycle.stop_requested) {
    return {
      available: false,
      reason:
        "A stop has already been asked for on this run. The first asker is the one the journal " +
        `records, so asking again would change nothing. ${STOP_IS_NOTICED_AT_A_MODEL_CALL}`,
    };
  }
  return { available: true, reason: null };
}

/**
 * The kill switch's record on one entry: who asked, and when.
 *
 * The *when* is left as the server's raw timestamp for the view to put through
 * `lib/time`, like every other timestamp on this screen — SQLite writes
 * `datetime('now')` with no zone marker, so it must never reach `new Date()`
 * directly. The *who* is named through {@link actorLabel} here, because that is
 * a copy decision rather than a formatting one.
 *
 * It keys on `stop_requested_at` and not on `stop_requested`: the boolean is
 * derived from that column server-side, and a record needs the stamp it is a
 * record *of*. The server's CHECK constraint makes a stamp without a requester
 * unstorable, and the `by === null` branch still exists rather than being
 * asserted away — every read of this untyped-at-the-edges wire is defensive, and
 * a record that renders "somebody" beats one that renders "null".
 *
 * @param cycle The journal entry.
 * @returns The record, or null for a run nobody asked to stop.
 */
export function stopRecord(cycle: CycleOut): { by: string; at: string } | null {
  if (cycle.stop_requested_at === null) return null;
  return {
    by: cycle.stop_requested_by === null ? "Somebody" : actorLabel(cycle.stop_requested_by),
    at: cycle.stop_requested_at,
  };
}

/**
 * What the stop confirm says it is about to do — and the two things it is not.
 *
 * Split out of the dialog for the reason {@link ABANDON_CONFIRM} is: the harness
 * renders no components, so a claim made inside one is a claim nothing checks,
 * and every line here has to be something the system actually delivers.
 *
 * There are **two** dangerous misreadings here rather than one, and they are the
 * two controls this button sits between. That a stop *reverses* what the run has
 * written — it does not; that is the rollback, afterwards. And that a stop is a
 * gentler *abandon* — it is not: abandoning closes a dead process's entry from
 * outside, a stop is obeyed by a run that is still alive, and the journal keeps
 * them apart precisely so a `failed` entry read the next morning says which.
 *
 * The last line is the one that costs something to say and is said anyway.
 * `AgentRun.chat` checks the switch before every provider call, and that is the
 * only check that exists today: the five deterministic consolidation jobs make
 * no provider call, so a stop recorded against one of those runs is kept and the
 * run finishes. Promising a wind-down that would not arrive is exactly the kind
 * of copy this file exists to prevent, and
 * `tests/test_consolidate.py::test_the_deterministic_runner_consults_no_stop_
 * switch_and_the_copy_says_so` is what keeps this sentence answerable to the
 * code.
 */
export const STOP_CONFIRM: readonly string[] = [
  "Stopping records the instruction on this entry and changes nothing else: the cycle stays " +
    "running, and everything it has already written stays in the graph. It does not reverse " +
    "anything.",
  "The run notices at its next check and closes its own entry as failed. Rolling the cycle back " +
    "afterwards is what takes its writes back — stopping and undoing are two decisions, and a " +
    "switch that did both would make it impossible to stop, look at what it did, and then decide.",
  "This is not abandoning. Abandoning closes the entry of a run nothing is going to finish, from " +
    "outside; a stop is an instruction a live run obeys and records itself. The journal keeps the " +
    "two apart, so a failed entry says which of them happened.",
  `${STOP_IS_NOTICED_AT_A_MODEL_CALL} For a run that is never going to finish at all, ` +
    `"${ABANDON_ACTION_LABEL}" is the control.`,
];

/**
 * What a stopped cycle's toast says happened.
 *
 * It reports the *instruction*, never the outcome: the row comes back still
 * `running`, and saying "stopped" about a run that is still writing would be the
 * one claim this screen has no way to make good on.
 *
 * It used to close on *"the entry closes when the run notices"* — a wind-down,
 * promised in the one place a human reads immediately after pressing the button
 * and therefore the one most likely to be believed. What replaces it is
 * {@link STOP_IS_NOTICED_AT_A_MODEL_CALL}.
 *
 * @param cycle The cycle row as the route answered with it — still `running`.
 */
export function stopOutcome(cycle: CycleOut): string {
  return (
    `Cycle ${shortId(cycle.id)} has been asked to stop. It is still ${cycle.status}: nothing it ` +
    `wrote has changed. ${STOP_IS_NOTICED_AT_A_MODEL_CALL}`
  );
}

/**
 * The half of the space-refusal copy that is true whenever a scope stops
 * resolving, shared by the live refusal and the recorded one.
 *
 * It states what *changed* rather than what is missing, which is the whole of
 * the never-say-it-does-not-exist rule: the server answers a space that was
 * never created and a space the caller holds no grant on with word-for-word
 * identical text on purpose, and copy that resolved the ambiguity would be an
 * existence oracle over every space in the file.
 */
const SCOPE_STOPS_RESOLVING =
  "A space stops resolving once it is archived or renamed";

/** How a sentence about a refused scope names it, without printing a bare id. */
function scopeReference(scope: SpaceName | null): string {
  return scope === null ? "the scope it named" : `the scope "${scope.label}"`;
}

/**
 * What a raw id looks like anywhere in a server-written sentence: `uuid4().hex`.
 *
 * Global and case-insensitive because it is used to *replace* every occurrence —
 * the gardener's refusal names its scope twice, once in the sentence and once in
 * the command it suggests.
 */
const BARE_ID_IN_TEXT = /[0-9a-fA-F]{32}/g;

/**
 * A server sentence with every raw id in it replaced by what the page calls it.
 *
 * **This is the guard that fails closed**, and it exists because the alternative
 * has now failed twice. The journal renders three kinds of string the server
 * wrote — a recorded cycle failure, a job's own error, and a delete guard's
 * refusal — and each was handled by recognising the message shapes known at the
 * time and passing everything else through verbatim. Two shapes then arrived
 * that were not on the list, and both put 32 hex characters on the screen: the
 * scope refusal, and the gardener's ungranted-scope refusal, which prints the id
 * twice. A rewrite rule that has to be extended per message is not a rule.
 *
 * So the default is inverted. Anything not covered by named copy still says what
 * the server said — that bargain is unchanged, and it is the only thing that
 * keeps an unfamiliar failure legible — but **no id survives it**. A row the page
 * can name is named; anything else is shortened, which is what every other id on
 * this screen already is.
 *
 * @param text The server's own sentence.
 * @param nameRow Names a row the page knows about; the default names none, which
 *   shortens every id.
 */
export function nameIdsIn(text: string, nameRow: RowNamer = () => null): string {
  return text.replace(BARE_ID_IN_TEXT, (id) => nameRow(id) ?? shortId(id, 12));
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
 * @param scope The scope the run named, resolved through `nameSpace` — the
 *   picker's value is a space **id**, so an unresolved reference here would put
 *   the 32-hex string back on the screen the resolution exists to keep it off.
 */
export function describeRunFailure(error: unknown, scope: SpaceName | null): string {
  if (isUnknownSpace(error)) {
    return (
      `nodum would not resolve ${scopeReference(scope)}. ${SCOPE_STOPS_RESOLVING}, so the picker ` +
      "may be out of date — reload the screen to see what is there now."
    );
  }
  // The live twin of the recorded refusal below, and the one a default install
  // actually meets: the picker offers every space, the gardener is granted two.
  // `describeError` would render it as `type: message`, which is this sentence
  // with a 32-hex id in it twice.
  if (error instanceof ApiError && recordedUngrantedScope(error.message) !== null) {
    return gardenerHoldsNoGrant(scope);
  }
  if (error instanceof ApiError && error.type === CYCLE_IN_PROGRESS) return CYCLE_ALREADY_RUNNING;
  return nameIdsIn(describeError(error));
}

/** The server's class name for a second consolidation refused by `0014`'s index. */
const CYCLE_IN_PROGRESS = "CycleInProgress";

/**
 * A run refused because another one holds the file, said to somebody in a browser.
 *
 * The server's own sentence ends *"run: nodum cycle-abandon <id>"*, and that is
 * the right remedy on the surface it was written for. Here it is not one: this
 * reader has no terminal in front of them, and {@link nameIdsIn} shortens the id
 * it would have to type — so the instruction arrives both unrunnable and
 * truncated. The journal grew the button that does it, and a remedy is only a
 * remedy if the reader can carry it out.
 *
 * No id is spelt, and that is not only the never-print-a-raw-id rule: the
 * blocking cycle is the entry carrying the `running` badge in the list this
 * panel sits above, which is somewhere to look rather than a string to match. It
 * is also the only entry that offers the button — `abandonAvailability` renders
 * it on a `running` cycle and nowhere else.
 */
const CYCLE_ALREADY_RUNNING =
  "A consolidation cycle is already running, and cycles are serialised across every process " +
  "sharing this database — so this one was refused rather than queued behind it. It is the " +
  'entry below carrying the "running" badge: wait for it to finish, or, if it was interrupted ' +
  `and will never close itself, open that entry and use "${ABANDON_ACTION_LABEL}".`;

/**
 * The gardener being ungranted on a scope, said with the space **named**.
 *
 * Shared by the live refusal and the recorded one for the reason the space copy
 * is: they are one event seen at two moments, and two wordings would drift.
 *
 * Nothing here resolves the server's deliberate ambiguity about a space — this
 * refusal is not about whether the space exists (the *caller* has already been
 * shown to see it; `_require_gardener_scope` runs after `open_cycle`), it is
 * about a grant the gardener does not hold. That is a fact about the gardener,
 * so it can be stated outright.
 *
 * The remedy is the server's own (`nodum grant builtin-gardener <space> edit`)
 * with the space named rather than spelt, and quoted, because a space title may
 * contain a space character and `nodum grant` resolves an id **or** a name.
 */
function gardenerHoldsNoGrant(scope: SpaceName | null): string {
  const remedy =
    scope === null
      ? "run nodum grant builtin-gardener <space> edit to give it one."
      : `run nodum grant builtin-gardener '${scope.label}' edit to give it one.`;
  return (
    `the gardener holds no grant on ${scopeReference(scope)}, so it cannot consolidate it. ` +
    `Migration 0014 seeds it with main and meta only and every other space is an explicit ` +
    `grant — ${remedy}`
  );
}

/**
 * A failure a cycle **recorded**, said without a forbidden phrase and without an
 * id.
 *
 * The stored counterpart of {@link describeRunFailure}, asking the same two
 * questions through the same owners — {@link recordedUnknownSpace} and
 * {@link recordedUngrantedScope}, both in `api/client.ts` beside the live
 * discriminator, because a match with two owners is a match that drifts.
 *
 * The two named shapes are the two refusals a scoped cycle can record about a
 * space, and the second is the one a default install meets: migration `0014`
 * grants the gardener `main` and `meta`, the scope picker offers everything, and
 * the refusal echoes *the reference the caller supplied* — which, for the one
 * caller that reaches this path by clicking, is a 32-hex id. It was rendered
 * verbatim in the list **and** in the detail page, twice per line.
 *
 * **Everything else still says what the server said, minus its ids.** That
 * bargain is deliberate — an unfamiliar failure has to stay legible, and this
 * module owns the refusals whose wording is a decision rather than every message
 * the service can produce — but it is no longer a pass-through:
 * {@link nameIdsIn} runs over it, so a *third* shape can arrive carrying an id
 * and still cannot put one on the screen.
 *
 * @param recorded The `error` string out of the report.
 * @param scope The cycle's scope resolved through `nameSpace`, if it had one.
 */
export function describeRecordedFailure(recorded: string, scope: SpaceName | null): string {
  if (recordedUnknownSpace(recorded) !== null) {
    return (
      `nodum would not resolve ${scopeReference(scope)} when this cycle ran. ` +
      `${SCOPE_STOPS_RESOLVING}, so it may have changed between the run being asked for and the ` +
      "gardener reaching it."
    );
  }
  if (recordedUngrantedScope(recorded) !== null) return gardenerHoldsNoGrant(scope);
  return nameIdsIn(recorded);
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
    // The note carries the direction warning because the table deliberately
    // does not colour a movement: this is the one metric a *cycle* can push
    // down by making the queue worse, since every fresh proposal it files is a
    // zero-day item dragging the median with it. A reader who takes a fall for
    // an improvement reads it exactly backwards.
    note:
      "Median age of everything waiting in the review queue. Fresh proposals are the youngest " +
      "items in it, so a cycle that files seven of them pulls this down while making the queue " +
      "longer.",
  },
  neglect_rate: {
    label: "Neglect rate",
    unit: "ratio",
    note: "Active nodes untouched past the neglect threshold, as a share of active nodes.",
  },
};

/** Reading order for the known metrics; unknown keys follow, sorted. */
const METRIC_ORDER = Object.keys(METRICS);

/** Render one metric value in its own unit. */
function formatMetric(value: number, unit: MetricUnit): string {
  if (unit === "ratio") return `${(value * 100).toFixed(1)}%`;
  if (unit === "days") return `${value.toFixed(1)} d`;
  if (unit === "count") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  return value.toFixed(2);
}

/** A delta's magnitude, rounded exactly as this table prints it. */
function deltaMagnitude(magnitude: number, unit: MetricUnit): string {
  if (unit === "ratio") return (magnitude * 100).toFixed(1);
  if (unit === "days") return magnitude.toFixed(1);
  if (unit === "count") {
    return Number.isInteger(magnitude) ? String(magnitude) : magnitude.toFixed(2);
  }
  return magnitude.toFixed(2);
}

/**
 * Whether a delta rounds away to nothing **at the precision this table prints**.
 *
 * A fixed epsilon in raw units is the wrong test, and the wrongness is visible:
 * `queue_age_days` is a median over timestamps, so an unchanged queue re-measured
 * a few seconds later moves by microseconds — far above any epsilon, and far
 * below the tenth of a day the cell renders. That rendered as **"−0.0 d"**, with
 * a down arrow, beside four other unchanged metrics all reading "no change".
 * Rounding first is the only test that cannot disagree with what is on screen.
 */
function isNoChange(delta: number, unit: MetricUnit): boolean {
  return Number(deltaMagnitude(Math.abs(delta), unit)) === 0;
}

/** Render a signed delta, in percentage points for a ratio. */
function formatDelta(delta: number, unit: MetricUnit): string {
  if (isNoChange(delta, unit)) return "no change";
  const sign = delta > 0 ? "+" : "−";
  const magnitude = deltaMagnitude(Math.abs(delta), unit);
  if (unit === "ratio") return `${sign}${magnitude} pp`;
  if (unit === "days") return `${sign}${magnitude} d`;
  return `${sign}${magnitude}`;
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
      // Read through the same rounding the text is, so the arrow can never
      // point at a movement the cell beside it calls "no change".
      direction:
        delta === null
          ? ("unknown" as const)
          : isNoChange(delta, definition.unit)
            ? ("flat" as const)
            : delta > 0
              ? ("up" as const)
              : ("down" as const),
    };
  });
}

/**
 * What to say instead of a metric table when a cycle recorded none.
 *
 * `{}` is a real answer rather than missing data, but it is **three** real
 * answers and they are not interchangeable. A rollback and a one-op curative
 * cycle compute no metrics because they touch specific rows; a consolidation
 * cycle that failed before any job ran computes none because it never got as far
 * as measuring anything — and telling that reader "a rollback and a one-op
 * curative cycle do not compute them" describes two things their cycle is not.
 *
 * @param cycle The journal entry.
 */
export function noMetricsNote(cycle: CycleOut): string {
  const consolidation = readConsolidationReport(cycle.report);
  if (consolidation !== null) {
    // `_metrics` is well defined on an empty graph and runs on both sides of
    // every job list, so the only consolidation report that carries none is the
    // one the runner writes when the cycle died outside a job — which is also
    // the one that carries no jobs.
    return consolidation.jobs.length === 0
      ? "This cycle recorded no coherence metrics: it failed before it could measure anything. " +
          "The reason is above."
      : "This cycle recorded no coherence metrics, and its report does not say why.";
  }
  const operation = readOperationReport(cycle.report);
  if (operation !== null && operation.abandoned) {
    // `close_cycle` replaces the report wholesale, so an abandoned run's metrics
    // are gone whether or not it had measured anything. Saying "a rollback and a
    // one-op curative cycle do not compute them" describes two things this is
    // not.
    return (
      "This cycle recorded no coherence metrics: it was interrupted before it could write any, " +
      "and closing its entry by hand replaced whatever report it had."
    );
  }
  if (operation !== null) {
    return (
      "This cycle recorded no coherence metrics. A rollback and a one-op curative cycle do not " +
      "compute them — they change specific rows rather than sweeping the file."
    );
  }
  if (cycle.status === "running") {
    return "The metrics are written when the cycle closes, and this one is still running.";
  }
  return "This cycle recorded no coherence metrics, and no report saying why.";
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

/**
 * The two nodes an edge event is about, kept as ids rather than as a sentence.
 *
 * The headline used to be built here as `relates_to: cba85bd8… → 9310f1b3…`,
 * and **every event a consolidation cycle writes is an edge**, so the headline
 * feature of this view was a column of truncated ids with nothing to click. A
 * node's title rides in its own payload; an edge's endpoints are ids and the
 * titles have to be fetched — so the ids come out structured and the view names
 * them, exactly as the review queue names both ends of an edge proposal.
 */
export interface EventEdge {
  /** The edge type, e.g. `duplicate_of`. */
  type: string;
  /** The source node's id, or null when the payload did not carry one. */
  srcId: string | null;
  /** The target node's id, or null when the payload did not carry one. */
  dstId: string | null;
}

/** One event, reduced to what the diff renders. */
export interface EventChange {
  seq: number;
  op: string;
  actor: string;
  createdAt: string;
  subject: EventSubject;
  shape: EventShape;
  /**
   * The row named in words, with no lookup: `relates_to: 4f2a… → 91bc…`, or a
   * node's title. The fallback the view falls back *to* — a screen reader label,
   * a `title` attribute, the rollback dialog's name for a row — and what it
   * renders while the titles are still in flight.
   */
  headline: string;
  /** The row's id, or null for an audit entry that names none. */
  rowId: string | null;
  /** Both endpoints, for an edge event; null for anything else. */
  edge: EventEdge | null;
  /** The node's title, for a node event; null when it has none or is not one. */
  nodeTitle: string | null;
  fields: FieldChange[];
}

/** Node columns the diff compares, in reading order. */
const NODE_FIELDS = ["title", "state", "type_id", "space_id", "parent_id", "content", "props"];

/**
 * Edge columns the diff compares, in reading order.
 *
 * `valid_from` and `valid_to` sit next to `state` because that adjacency is the
 * point: `supersede_edge` records **two** facts as two, `valid_to` closed (when
 * the edge stopped being true) *and* `archived` (it is no longer live), and a
 * diff that showed the second without the first showed half of the only write
 * this wave gave that column. `valid_from` still has no writer anywhere in the
 * system and costs nothing to list — {@link describeEvent} drops a row that did
 * not change, so it appears the day something starts writing it and not before.
 */
const EDGE_FIELDS = [
  "state",
  "valid_from",
  "valid_to",
  "type_id",
  "src_id",
  "dst_id",
  "confidence",
  "props",
];

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

  const title = row !== null && typeof row.title === "string" ? row.title.trim() : "";

  return {
    seq: event.seq,
    op: event.op,
    actor: event.actor,
    createdAt: event.created_at,
    subject,
    shape,
    headline: eventHeadline(subject, event.op, row),
    rowId: row !== null && typeof row.id === "string" ? row.id : null,
    edge: subject === "edge" && row !== null ? eventEdge(row) : null,
    nodeTitle: subject === "node" && title !== "" ? title : null,
    fields,
  };
}

/** Both endpoints of an edge event's row, as ids the view can name and link. */
function eventEdge(row: JsonObject): EventEdge {
  return {
    type: typeof row.type_id === "string" ? row.type_id : "edge",
    srcId: typeof row.src_id === "string" ? row.src_id : null,
    dstId: typeof row.dst_id === "string" ? row.dst_id : null,
  };
}

/**
 * What to call one end of an edge on screen.
 *
 * The review queue renders the same edges as *"event sourcing → Event Sourcing"*
 * because the server hands it `{id, title, space_id}` per referenced node; the
 * event log carries the row and nothing else, so the journal resolves the titles
 * itself and falls back to the shortened id for the ones it has not got. An
 * untitled node falls back the same way — its id is genuinely all there is.
 *
 * @param nodeId The endpoint's id, or null when the payload named none.
 * @param title The title if it has been resolved; `undefined` while the lookup
 *   is in flight, `null` once it has answered with nothing.
 */
export function endpointLabel(nodeId: string | null, title: string | null | undefined): string {
  if (nodeId === null) return "?";
  const named = typeof title === "string" ? title.trim() : "";
  return named === "" ? shortId(nodeId) : named;
}

/**
 * Every node id a page of the diff has to name, deduplicated, in reading order.
 *
 * The rule behind the title lookup, pulled out of the component because getting
 * it wrong is invisible until you watch the network panel — the same reason
 * `unresolvedSpaceIds` is a plain function beside `useArchivedSpaces`.
 *
 * It reads a **page**, never the whole cycle: 500 events is 1 000 endpoints and
 * `GET /api/nodes` has no id filter, so the lookup is one request per node and
 * has to be bounded by what is actually on screen. A node event needs no lookup
 * at all — its title is in its own payload.
 *
 * @param changes The events currently rendered.
 */
export function referencedNodeIds(changes: readonly EventChange[]): string[] {
  const ids: string[] = [];
  for (const change of changes) {
    if (change.edge === null) continue;
    for (const id of [change.edge.srcId, change.edge.dstId]) {
      if (id !== null && id !== "") ids.push(id);
    }
  }
  return [...new Set(ids)];
}

/**
 * What one event's row is called on screen, with endpoint titles when they are
 * known.
 *
 * {@link EventChange.headline} is the no-lookup fallback and is pure over ids by
 * construction, because it is computed before any title has been fetched. That
 * made it the wrong thing to *name a row with*: the rollback dialog printed
 * `relates_to: 19c082d3… → db24d36d…` for an edge the event list on the same
 * page was rendering as *"event sourcing → Event Sourcing"*, having resolved
 * both ends. The titles are the same titles; only the dialog was not given them.
 *
 * @param change One reduced event.
 * @param titles Resolved node titles by id; an absent key means the lookup has
 *   not answered, which falls back to the shortened id exactly as the diff does.
 */
export function changeHeadline(
  change: EventChange,
  titles?: ReadonlyMap<string, string | null>,
): string {
  if (change.edge === null || titles === undefined) return change.headline;
  const src = endpointLabel(change.edge.srcId, titles.get(change.edge.srcId ?? ""));
  const dst = endpointLabel(change.edge.dstId, titles.get(change.edge.dstId ?? ""));
  return `${change.edge.type}: ${src} → ${dst}`;
}

/**
 * What this cycle's own events call each row it touched.
 *
 * The rollback confirm lists the rows standing in the way of a reversal, and the
 * server names them by id — it is reporting on the `nodes`/`edges` tables, not
 * on anything with a title. But every conflicting row is by definition a row
 * *this cycle wrote*, so its event is in this list, and that event's payload
 * carries the title.
 *
 * Newest event wins: a row the cycle touched twice is called what it was last
 * called, which is what the graph holds now.
 *
 * @param changes The cycle's events, newest first, as the API returns them.
 * @param titles Resolved node titles, so an **edge** row is named by its
 *   endpoints rather than by their ids — see {@link changeHeadline}. Omitted,
 *   every edge falls back to the two shortened ids.
 */
export function rowHeadlines(
  changes: readonly EventChange[],
  titles?: ReadonlyMap<string, string | null>,
): Map<string, string> {
  const names = new Map<string, string>();
  for (const change of changes) {
    if (change.rowId === null || names.has(change.rowId)) continue;
    names.set(change.rowId, changeHeadline(change, titles));
  }
  return names;
}

/**
 * Every node id the rollback verdict has to look up before it can name its rows.
 *
 * The rule behind the dialog's own title lookup, kept out of the component for
 * the reason {@link referencedNodeIds} is: getting it wrong is invisible until
 * you watch the network panel, and the harness renders nothing.
 *
 * Two sources, and they are different in kind. A **conflict** or **blocker**
 * names a row *this cycle wrote*, so its event is in the list behind the dialog
 * — for an edge, what has to be resolved is that event's two endpoints. A
 * **dependant** is by definition a row the cycle did *not* write, so no event
 * names it and the id is all there is; it is looked up directly, and answering
 * with nothing is fine — `GET /api/nodes/{id}` 404s for the one dependant that
 * is not a node at all (a grant's `agent_id`), and the shortened id stands.
 *
 * @param conflicts The verdict's conflicts.
 * @param blockers The verdict's blockers.
 * @param changes The cycle's reduced events.
 */
export function verdictNodeIds(
  conflicts: readonly RollbackConflictOut[],
  blockers: readonly RollbackBlockerOut[],
  changes: readonly EventChange[],
): string[] {
  const rows = new Set<string>([
    ...conflicts.map((conflict) => conflict.row_id),
    ...blockers.map((blocker) => blocker.row_id),
  ]);
  const named = changes.filter((change) => change.rowId !== null && rows.has(change.rowId));
  const ids = [
    ...referencedNodeIds(named),
    ...blockers.flatMap((blocker) => blocker.dependants),
  ];
  return [...new Set(ids.filter((id) => id !== ""))];
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

/** One page of the event list, and what to call it — `lib/paging`'s shape. */
export type EventWindow = PageWindow;

/**
 * Which slice of a cycle's events to render, and what the pager says.
 *
 * The server's cap works and the presentation had none: a 500-event cycle
 * rendered every diff table expanded, 12 066 DOM nodes and 79 055 px tall, which
 * Chrome could not screenshot and a reviewer could not read — and a nightly
 * cycle on a real graph reaches that cap by design rather than by accident.
 * Paging rather than collapsing, because it bounds the *lookups* too: the
 * endpoint titles are one request per node, so what is rendered is what is
 * fetched.
 *
 * The arithmetic itself is `lib/paging.pageWindow`, shared with the review
 * queue, which meets the same shape one screen away — a cycle that files
 * hundreds of proposals in one run puts that list exactly here. This wrapper is
 * the noun and nothing else.
 *
 * @param total How many events there are.
 * @param page The page asked for, zero-based.
 * @param size Events per page; anything below 1 is read as 1.
 */
export function eventWindow(total: number, page: number, size: number): EventWindow {
  return pageWindow(total, page, size, "event");
}

/**
 * What an empty event list means for *this* cycle.
 *
 * Three different facts wear the same empty list, and the copy used to state one
 * of them for all three: *"This cycle wrote nothing to the graph. Every job ran
 * and none of them found anything to change."* is false about a cycle that
 * failed before a job started — the page's own headline says so two inches above
 * — and false about a curative operation, where no job exists to have run.
 * {@link cycleWork} already tells the three apart, so the empty state does too.
 *
 * @param cycle The journal entry.
 */
export function emptyEventsNote(cycle: CycleOut): string {
  if (cycle.dry_run) {
    return (
      "A rehearsal emits no graph event — an empty list here is the checkable form of " +
      "“it changed nothing”."
    );
  }
  const consolidation = readConsolidationReport(cycle.report);
  if (consolidation !== null) {
    return consolidation.jobs.length === 0
      ? "No job ran, so nothing was written. The cycle failed before it could start one — the " +
          "reason is at the top of this page."
      : "This cycle wrote nothing to the graph. Every job ran and none of them found anything " +
          "to change.";
  }
  const operation = readOperationReport(cycle.report);
  if (operation !== null && operation.abandoned) {
    // An abandoned run usually *did* write — that is why closing its entry
    // matters — so an empty list here means it died before it got that far.
    return (
      "This run was interrupted before it wrote anything to the graph, so there is nothing here " +
      "to reverse."
    );
  }
  if (operation !== null) {
    return (
      "One curative operation ran and matched nothing, so it wrote no graph event. There is " +
      "nothing here to reverse."
    );
  }
  if (cycle.status === "running") {
    return "Still running: what it has written so far appears here as it goes.";
  }
  return "This cycle wrote nothing to the graph, and recorded no report saying what it tried.";
}

/* ------------------------------------------------------------------ */
/* Rollback: the preflight verdict, the conflicts and the blockers      */
/* ------------------------------------------------------------------ */

/** Names a row this cycle touched, for a surface holding only its id. */
export type RowNamer = (rowId: string) => string | null;

/**
 * What to call a row on screen: the page's own name for it, or its shortened id.
 *
 * The one rule shared by both refusal shapes, so neither can drift into printing
 * 32 hex characters where the other prints a title.
 *
 * @param rowId The row's id.
 * @param nameRow Names a row this cycle touched — {@link rowHeadlines}.
 * @param keep How much of an unnamed id to show; see {@link shortId}.
 */
function rowLabel(rowId: string, nameRow: RowNamer, keep?: number): string {
  return nameRow(rowId) ?? shortId(rowId, keep);
}

/** One conflict, in the four facts a human needs to act on it. */
export interface ConflictLine {
  /** `node` or `edge`. */
  kind: string;
  /** The row that has moved. */
  rowId: string;
  /**
   * What this cycle's own events call that row, or null when nothing does.
   *
   * The server names a conflict by id because it is reporting on a table. The
   * page around this dialog is not: the row is one the cycle wrote, so its event
   * is in the list behind the dialog and already knows it as *"Meeting
   * 2026-07-01"*. Showing the id alone while the page behind it shows the title
   * makes the reader do a lookup the page had already done.
   */
  name: string | null;
  /** What this cycle did to it. */
  cycleDid: string;
  /** What has happened to it since. */
  sinceDid: string;
  /**
   * Who made that later write, named rather than spelt ({@link actorLabel}).
   *
   * This is a confirm dialog rather than the event log — the log line keeps the
   * raw actor string, because that is the record — and *"human:owner"* beside a
   * header that greets the same person as *owner* is an id printed at someone
   * whose name is already on the screen.
   */
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
 * @param nameRow Names a row this cycle touched — {@link rowHeadlines} over the
 *   page's own event list. Omitted, the line falls back to the shortened id.
 */
export function describeConflict(
  conflict: RollbackConflictOut,
  nameRow: RowNamer = () => null,
): ConflictLine {
  const cycleDid = `event #${conflict.cycle_event_seq} (${conflict.cycle_event_op})`;
  const sinceDid = `event #${conflict.conflicting_seq} (${conflict.conflicting_op})`;
  const inCycle = conflict.conflicting_cycle_id;
  const name = nameRow(conflict.row_id);
  const called = rowLabel(conflict.row_id, nameRow);
  const provenance =
    inCycle === null
      ? ""
      : ` That write belongs to cycle ${shortId(inCycle)}, so rolling that one back may clear this.`;
  return {
    kind: conflict.kind,
    rowId: conflict.row_id,
    name,
    cycleDid,
    sinceDid,
    who: actorLabel(conflict.conflicting_actor),
    inCycle,
    sentence:
      `This cycle's ${cycleDid} touched the ${conflict.kind} ${called}; ` +
      `${sinceDid} by ${actorLabel(conflict.conflicting_actor)} has changed it since, so ` +
      `reversing the cycle would overwrite that.${provenance}`,
  };
}

/**
 * How many dependants a blocker line names before it counts the rest.
 *
 * The same bargain `service._named_rows` strikes on the server, made again here
 * because this list is the **whole** one rather than the capped handful the
 * `reason` sentence spells out: a refusal naming none of them cannot be acted
 * on, and one naming four hundred is not a sentence. The count is always stated,
 * so nothing is hidden by the cap.
 */
export const MAX_NAMED_DEPENDANTS = 5;

/** One row standing on a blocker, named where the page can name it. */
export interface DependantLine {
  /** The dependant's id — a node id, or an agent id for a grant. */
  id: string;
  /** What the page calls it, or the shortened id when nothing does. */
  label: string;
}

/**
 * One blocker: a row this cycle created that the graph has since grown onto.
 *
 * Deliberately **not** a {@link ConflictLine}, because it is not the same event
 * and does not have the same answer. A conflict is *the graph moved on* — a
 * later write changed a row this cycle wrote, and reversing would overwrite that
 * work. A blocker is *something now depends on what this rollback would remove*
 * — the cycle created a row, something outside the cycle has since pointed at
 * it, and the delete that reverses the create refuses to cascade onto rows the
 * reversal was never asked to touch. Rendering the two as one list would tell a
 * human that the same thing had happened twice.
 */
export interface BlockerLine {
  /** `node` — the only kind the delete guards cover. */
  kind: string;
  /** The row the cycle created and the rollback would have to delete. */
  rowId: string;
  /**
   * What this cycle's own events call that row, or null when nothing does.
   *
   * A blocker's row is *always* one the cycle created, so its create is in the
   * event list behind the dialog and that payload carries the title — the same
   * lookup {@link describeConflict} does, and a stronger guarantee than the
   * conflict case, where the row merely has to have been touched.
   */
  name: string | null;
  /** The create this rollback would reverse: `event #42 (node.create)`. */
  cycleDid: string;
  /** What now depends on it, in the order the server listed them. */
  dependants: DependantLine[];
  /** How many there are in total — `dependants` is capped for reading, this is not. */
  dependantCount: number;
  /**
   * The guard's own refusal, in the server's words with its ids named.
   *
   * The wording is the server's, which is the same bargain
   * {@link describeRecordedFailure} strikes: what this module owns is the
   * sentence *about* the refusal, not every message the service can produce, and
   * this is the one line that says what the run will actually fail with if it is
   * attempted anyway.
   *
   * What it may not carry is a bare id. `service._delete_blocker` writes *"space
   * &lt;32-hex&gt; still holds 3 node(s) (&lt;32-hex&gt;, …)"* — the row and the
   * dependants, spelt — and it sat directly beside a sentence that had carefully
   * named both. {@link nameIdsIn} runs over it, so every id in it is the page's
   * own name for that row or its shortened form.
   */
  reason: string;
  /** The whole of it as one sentence, for a screen reader and a copy-paste. */
  sentence: string;
}

/**
 * Render one blocker legibly: which row, what the cycle did, what depends on it.
 *
 * Same argument as {@link describeConflict}, one refusal along: a human told
 * *which* rows are held down and by what can go and take those back, and one
 * told "rollback failed" can only go and look. So every field the server sends
 * is used, and the ids are named through the page's own event list rather than
 * printed.
 *
 * The sentence names the row and its dependants and stops there; the `reason` is
 * rendered beside it rather than spliced into it, and goes through
 * {@link nameIdsIn} for the same reason the sentence never spells an id.
 *
 * @param blocker One row out of `RollbackOut.blockers`.
 * @param nameRow Names a row on this page — {@link rowHeadlines} over the
 *   cycle's own event list, and the resolved node titles for the dependants,
 *   which no event names. Omitted, every line falls back to shortened ids.
 */
export function describeBlocker(
  blocker: RollbackBlockerOut,
  nameRow: RowNamer = () => null,
): BlockerLine {
  const cycleDid = `event #${blocker.cycle_event_seq} (${blocker.cycle_event_op})`;
  const name = nameRow(blocker.row_id);
  const called = rowLabel(blocker.row_id, nameRow);
  // A dependant is by definition a row this cycle did *not* write, so no event
  // on this page names it — but it is a node, and the dialog resolves its title
  // the way the diff resolves an edge's endpoints. Where that answers nothing
  // (a grant's `agent_id` is not a node at all) the shortened id stands, which
  // is still not the 32 hex characters the server sent.
  const dependants = blocker.dependants
    .slice(0, MAX_NAMED_DEPENDANTS)
    .map((id) => ({ id, label: rowLabel(id, nameRow, 12) }));
  const total = blocker.dependants.length;
  const rest = total - dependants.length;
  const listed = dependants.map((dependant) => dependant.label);
  // Capped, the tail is a count rather than a final "and" — the same shape
  // `service._named_rows` writes, so the two readings of one list agree.
  const named = rest > 0 ? `${listed.join(", ")} and ${plural(rest, "other")}` : joined(listed);
  const depend = total === 1 ? "depends" : "depend";
  return {
    kind: blocker.kind,
    rowId: blocker.row_id,
    name,
    cycleDid,
    dependants,
    dependantCount: total,
    reason: nameIdsIn(blocker.reason, nameRow),
    sentence:
      `This cycle's ${cycleDid} created the ${blocker.kind} ${called}, and ` +
      `${plural(total, "row")} ${depend} on it now (${named}). Reversing a create deletes the ` +
      "row, and the deletion will not cascade onto rows this cycle never made — so those have " +
      "to be taken back first.",
  };
}

/**
 * The dry run's verdict, as the confirm dialog reads it.
 *
 * Two lists, and **`blocked` is true when either is non-empty**. They are not
 * interchangeable and neither implies the other: a rollback can be perfectly
 * free of conflicts and still fail on a guard, which is precisely the shape that
 * made the preflight disagree with the run.
 */
export interface RollbackPlan {
  /** True when anything at all stands between the cycle and its reversal. */
  blocked: boolean;
  headline: string;
  detail: string;
  /** Rows the graph has *moved* since the cycle wrote them. */
  conflicts: ConflictLine[];
  /** Rows the cycle *created* that something outside it now depends on. */
  blockers: BlockerLine[];
}

/**
 * The headline over a refused verdict, naming which of the two shapes bit.
 *
 * They compose, so all three cases are written out rather than concatenated
 * from a clause each: a reader told "5 rows are in the way" about two conflicts
 * and three blockers has been told one number about two different problems with
 * two different answers.
 */
function blockedHeadline(conflicts: number, blockers: number): string {
  if (blockers === 0) return `${plural(conflicts, "row")} moved since this cycle ran.`;
  if (conflicts === 0) {
    return `Something now depends on ${plural(blockers, "row")} this cycle created.`;
  }
  return (
    `${plural(conflicts, "row")} moved since this cycle ran, and something now depends on ` +
    `${plural(blockers, "row")} it created.`
  );
}

/** What to do about it, per shape, under {@link blockedHeadline}. */
function blockedDetail(conflicts: number, blockers: number): string {
  const opener =
    "A rollback is all of it or none of it, so nothing can be reversed while these stand.";
  const moved = "Rolling back would overwrite the later work listed below.";
  const grown =
    "Reversing a create deletes the row it made, and that deletion refuses to cascade onto " +
    "rows this cycle never wrote — so each dependant below has to be taken back first.";
  if (blockers === 0) return `${opener} ${moved}`;
  if (conflicts === 0) return `${opener} ${grown}`;
  return `${opener} ${moved} ${grown}`;
}

/**
 * Turn a dry-run rollback into the verdict a confirm dialog shows.
 *
 * The dry run is the whole reason the confirm calls the API before the human
 * commits: it opens no cycle, writes nothing, and answers under a **200**, so a
 * blocked rollback is something the reader meets *before* acting rather than as
 * a failure afterwards.
 *
 * **The verdict is clean only when `conflicts` and `blockers` are both empty.**
 * Reading one of them was the defect this closes: the guards refuse a rollback
 * exactly as firmly as a conflict does, so a dialog checking conflicts alone
 * said "clean" and offered the button for a rollback that then died at apply
 * time — the one answer a preflight must not give.
 *
 * @param result The `dry_run: true` response.
 * @param nameRow Names a row this cycle touched; see {@link describeConflict}.
 */
export function rollbackPlan(result: RollbackOut, nameRow?: RowNamer): RollbackPlan {
  const conflicts = result.conflicts.map((conflict) => describeConflict(conflict, nameRow));
  const blockers = result.blockers.map((blocker) => describeBlocker(blocker, nameRow));
  if (conflicts.length > 0 || blockers.length > 0) {
    return {
      blocked: true,
      headline: blockedHeadline(conflicts.length, blockers.length),
      detail: blockedDetail(conflicts.length, blockers.length),
      conflicts,
      blockers,
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
      `Nothing outside this cycle has touched the rows it wrote, and nothing has been built on ` +
      `the rows it created, so the whole of it can be taken back.${skipped} The reversal is ` +
      "recorded as a new cycle of its own, which can be rolled back in turn to re-apply this one.",
    conflicts,
    blockers,
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
 * `blockers` is empty here and that is the wire's shape rather than an omission:
 * the 409 body carries a `conflicts` list and nothing else, because a rollback
 * that meets a *guard* mid-commit refuses as `UndoNotPossible` — one sentence,
 * no list — and reaches the dialog as an ordinary error toast.
 *
 * @param conflicts The `conflicts` list out of the 409 error body.
 * @param nameRow Names a row this cycle touched; see {@link describeConflict}.
 */
export function rollbackRefusal(
  conflicts: readonly RollbackConflictOut[],
  nameRow?: RowNamer,
): RollbackPlan {
  return {
    blocked: true,
    headline: `${plural(conflicts.length, "row")} moved while you were reading this.`,
    detail:
      "The check above passed, and then something wrote to a row this cycle had touched. " +
      "Nothing was rolled back — a rollback is all of it or none of it.",
    conflicts: conflicts.map((conflict) => describeConflict(conflict, nameRow)),
    blockers: [],
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
