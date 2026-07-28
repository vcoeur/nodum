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
 *   report carries failures as *strings the server wrote* — including
 *   `open_cycle`'s own `"TypeNotFound: unknown space: 909a…"`, which is the one
 *   phrasing nothing user-facing may render, plus a bare 32-hex id. So the
 *   headline counts and names failures and never quotes one; the reason belongs
 *   to {@link cycleFailures}, which routes it through
 *   {@link recordedUnknownSpace} — the *same* discriminator {@link
 *   describeRunFailure} uses on a live refusal, reading a string that never came
 *   back through `fetch`.
 * - **A space is named, never spelt.** `cycles.scope` is always the **resolved
 *   id** (`open_cycle` runs it through `_resolve_space`), so every sentence
 *   about a scope takes a {@link SpaceName} the caller resolved through
 *   `nameSpace` rather than the raw reference off the row.
 */

import { isUnknownSpace, recordedUnknownSpace } from "../../api/client";
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
    // The *reason* belongs to `cycleFailures`, for the reason `failureNote`
    // gives: `report["error"]` is `str(exc)` and this is an `<h1>`.
    const failure = operation.error === null ? "" : " It failed.";
    if (operation.op === "rollback_cycle") {
      const target = shortIdOr(operation.rolledBack, "another cycle");
      const count =
        operation.reversed === null ? "" : ` — ${plural(operation.reversed, "event")} reversed`;
      return `Took ${target} back${count}.${failure}`;
    }
    return `One curative operation: ${registeredName(operation.op, "unnamed")}.${failure}`;
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
  if (operation !== null && operation.error !== null) {
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
  if (wroteGraphEvents === false) {
    return {
      available: false,
      reason:
        "This cycle wrote no graph event — a curative operation that matched nothing writes " +
        "none — so there is nothing to reverse.",
    };
  }
  return { available: true, reason: null };
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
  return describeError(error);
}

/**
 * A failure a cycle **recorded**, said without claiming a space is missing.
 *
 * The stored counterpart of {@link describeRunFailure}, and it asks the same
 * question through the same owner: {@link recordedUnknownSpace} is the client's
 * one unknown-space match, reading a string rather than a caught response. A
 * scoped cycle whose space stopped resolving records *"TypeNotFound: unknown
 * space: 909a3060…"* — forbidden phrasing and a bare id in one line — and it is
 * the *first* failure a real scoped run produces, so this is not a hypothetical
 * branch.
 *
 * Anything else is the server's own line, shown as it was written. That is the
 * same bargain `describeError` strikes for a live failure: what this module
 * owns is the one refusal whose wording is a decision, not every message the
 * server can produce.
 *
 * @param recorded The `error` string out of the report.
 * @param scope The cycle's scope resolved through `nameSpace`, if it had one.
 */
export function describeRecordedFailure(recorded: string, scope: SpaceName | null): string {
  if (recordedUnknownSpace(recorded) === null) return recorded;
  return (
    `nodum would not resolve ${scopeReference(scope)} when this cycle ran. ` +
    `${SCOPE_STOPS_RESOLVING}, so it may have changed between the run being asked for and the ` +
    "gardener reaching it."
  );
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
  if (readOperationReport(cycle.report) !== null) {
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
 * What this cycle's own events call each row it touched.
 *
 * The rollback confirm lists the rows standing in the way of a reversal, and the
 * server names them by id — it is reporting on the `nodes`/`edges` tables, not
 * on anything with a title. But every conflicting row is by definition a row
 * *this cycle wrote*, so its event is on the same page, and that event's payload
 * carries the title. Naming the row *"Meeting 2026-07-01"* costs one lookup in a
 * map the page already built.
 *
 * Newest event wins: a row the cycle touched twice is called what it was last
 * called, which is what the graph holds now.
 *
 * @param changes The cycle's events, newest first, as the API returns them.
 */
export function rowHeadlines(changes: readonly EventChange[]): Map<string, string> {
  const names = new Map<string, string>();
  for (const change of changes) {
    if (change.rowId === null || names.has(change.rowId)) continue;
    names.set(change.rowId, change.headline);
  }
  return names;
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

/** One page of the event list, and what to call it. */
export interface EventWindow {
  /** Zero-based page index, clamped into range. */
  page: number;
  /** How many pages there are; at least 1, even for an empty list. */
  pages: number;
  /** Slice bounds into the event list — `[from, to)`. */
  from: number;
  to: number;
  /** `Events 1–25 of 500`, one-based and inclusive, for a human. */
  label: string;
}

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
 * The page is clamped rather than trusted: a list that shrinks under a pager
 * (the events reload after a rollback) would otherwise leave the reader on an
 * empty page with no way back that is not the browser's.
 *
 * @param total How many events there are.
 * @param page The page asked for, zero-based.
 * @param size Events per page; anything below 1 is read as 1.
 */
export function eventWindow(total: number, page: number, size: number): EventWindow {
  const perPage = Math.max(1, Math.floor(size));
  const pages = Math.max(1, Math.ceil(total / perPage));
  const current = Math.min(Math.max(0, Math.floor(page)), pages - 1);
  const from = current * perPage;
  const to = Math.min(total, from + perPage);
  const label =
    total === 0 ? "No events" : `Events ${from + 1}–${to} of ${total}`;
  return { page: current, pages, from, to, label };
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
  if (readOperationReport(cycle.report) !== null) {
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
   * The guard's own refusal, as the server wrote it.
   *
   * Shown verbatim, which is the same bargain {@link describeRecordedFailure}
   * strikes: what this module owns is the sentence *about* the refusal, not
   * every message the service can produce. It is also the one line that says
   * what the run will actually fail with if it is attempted anyway.
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
 * The sentence names the row and its dependants and stops there — the `reason`
 * carries the server's own wording, ids and all, and is rendered beside the
 * sentence rather than spliced into it, so a page that could name every row does
 * not put 32 hex characters into the line a screen reader announces.
 *
 * @param blocker One row out of `RollbackOut.blockers`.
 * @param nameRow Names a row this cycle touched — {@link rowHeadlines} over the
 *   page's own event list. Omitted, the line falls back to shortened ids.
 */
export function describeBlocker(
  blocker: RollbackBlockerOut,
  nameRow: RowNamer = () => null,
): BlockerLine {
  const cycleDid = `event #${blocker.cycle_event_seq} (${blocker.cycle_event_op})`;
  const name = nameRow(blocker.row_id);
  const called = rowLabel(blocker.row_id, nameRow);
  // A dependant is by definition a row this cycle did *not* write, so the page's
  // event list usually cannot name it — the shortened id is the honest answer,
  // and it is still not the 32 hex characters the server sent.
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
    reason: blocker.reason,
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
