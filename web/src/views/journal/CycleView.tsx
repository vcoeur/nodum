/**
 * Route `/journal/:cycleId` — one journal entry in full.
 *
 * Three things, in the order a reviewer needs them: what the cycle did and who
 * asked for it, what it cost or gained in coherence, and exactly what it wrote.
 * Then the one action that undoes all of it.
 *
 * Everything comes from a single `GET /api/cycles/{id}`, which composes the
 * cycle row, its metrics and the events it wrote into one round trip. The
 * events are the append-only log narrowed to this cycle — not a diff the cycle
 * stored — so this page cannot disagree with the file.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import {
  EmptyState,
  nameSpace,
  Spinner,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useSpaces,
  useToast,
} from "../../components";
import type { SpaceName } from "../../components";
import type { CycleDetailOut, CycleOut, RollbackOut } from "../../api/types";
import { describeFailure, formatTimestamp, formatTimestampLong } from "../../lib";
import type { FailureDescription } from "../../lib";
import { AbandonDialog } from "./AbandonDialog";
import { CycleBadges } from "./CycleBadges";
import { EventDiff } from "./EventDiff";
import { MetricTable } from "./MetricTable";
import { RollbackDialog } from "./RollbackDialog";
import { StopDialog } from "./StopDialog";
import {
  ABANDON_ACTION_LABEL,
  abandonAvailability,
  abandonOutcome,
  CYCLE_EVENT_LIMIT,
  cycleCaveats,
  cycleFailures,
  cycleProvenance,
  cycleWork,
  describeEvent,
  describeRecordedFailure,
  emptyEventsNote,
  noMetricsNote,
  readConsolidationReport,
  rollbackAvailability,
  rollbackOutcome,
  RUNNING_ACTIONS_HINT,
  STOP_ACTION_LABEL,
  stopAvailability,
  stopOutcome,
  stopRecord,
} from "./journal";
import type { ConsolidationReport } from "./journal";
import "./journal.css";

/** Loading / loaded / failed for the entry. */
type LoadState =
  | { status: "loading" }
  | { status: "ready"; detail: CycleDetailOut }
  | { status: "failed"; failure: FailureDescription };

/** The cycle-detail route. Default-exported because the route is lazily loaded. */
export default function CycleView() {
  const { cycleId } = useParams<{ cycleId: string }>();
  const toast = useToast();
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [confirming, setConfirming] = useState(false);
  const [abandoning, setAbandoning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [attempt, setAttempt] = useState(0);
  /**
   * Where the keyboard goes once the rollback has happened.
   *
   * `Modal` restores focus to its opener only when the opener is still usable,
   * and a confirmed rollback leaves that button *disabled* — the cycle is now
   * `rolled_back`, so there is nothing left to roll back. Focusing a disabled
   * button drops the user on `<body>`, so the view places focus on the heading,
   * which is also the sentence that has just changed.
   */
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (!cycleId) return;
    const controller = new AbortController();
    setLoad({ status: "loading" });
    api
      .getCycle(cycleId, { limit: CYCLE_EVENT_LIMIT }, controller.signal)
      .then((detail) => setLoad({ status: "ready", detail }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ status: "failed", failure: describeFailure(error, "this cycle") });
      });
    return () => controller.abort();
  }, [cycleId, attempt]);

  const reload = useCallback(() => setAttempt((count) => count + 1), []);

  const onRolledBack = useCallback(
    (result: RollbackOut) => {
      toast.show("success", "Cycle rolled back", rollbackOutcome(result));
      heading.current?.focus();
      reload();
    },
    [reload, toast],
  );

  // Same landing as a rollback, and for the same reason: the button that opened
  // the dialog is about to be disabled (the cycle is no longer `running`), and
  // `Modal` deliberately will not restore focus onto a control that cannot be
  // used. The heading is also the sentence that has just changed.
  const onAbandoned = useCallback(
    (closed: CycleOut) => {
      toast.show("info", "Cycle abandoned", abandonOutcome(closed));
      heading.current?.focus();
      reload();
    },
    [reload, toast],
  );

  // The same landing again, for the same reason: the stop button gives way to
  // the record once a stop is on the entry, so the control that opened the
  // dialog is gone by the time it closes. The toast reports the *instruction*
  // and never an outcome — the row comes back still `running`.
  const onStopRequested = useCallback(
    (stopped: CycleOut) => {
      toast.show("info", "Stop requested", stopOutcome(stopped));
      heading.current?.focus();
      reload();
    },
    [reload, toast],
  );

  // Everything below has to be computed before the early returns: hooks do not
  // get to be conditional. `detail` is null until the read lands, and every
  // derivation tolerates that.
  const detail = load.status === "ready" ? load.detail : null;

  // The cycle's scope is the **resolved space id** (`open_cycle` runs it through
  // `_resolve_space`), so the header names it through the shared vocabulary
  // rather than printing 32 hex characters. The archived read is lazy: a cycle
  // may have run over a space retired since, and nothing else on this page needs
  // it.
  const spaceList = useSpaces();
  const scopes = useMemo(
    () => (detail?.cycle.scope == null ? [] : [detail.cycle.scope]),
    [detail?.cycle.scope],
  );
  const unresolved = useMemo(
    () => unresolvedSpaceIds(scopes, spaceList.spaces),
    [scopes, spaceList.spaces],
  );
  const archived = useArchivedSpaces(unresolved.length > 0);
  const scope: SpaceName | null =
    detail === null || detail.cycle.scope === null
      ? null
      : nameSpace(detail.cycle.scope, spaceList.spaces, archived.spaces);

  // One reduction of the event list, shared by the diff, the rollback
  // availability check and the confirm dialog's row names — three readings of
  // the same log that must not disagree.
  const changes = useMemo(() => (detail?.events ?? []).map(describeEvent), [detail?.events]);

  if (!cycleId) {
    return (
      <div className="nd-view">
        <EmptyState
          title="No cycle given"
          body="A journal entry is per cycle. Pick one from the journal."
          action={
            <Link to="/journal" className="nd-button">
              Open the journal
            </Link>
          }
        />
      </div>
    );
  }

  if (load.status === "loading") {
    return (
      <div className="nd-view nd-jn">
        <div className="nd-empty">
          <Spinner large label="Loading the cycle" />
        </div>
      </div>
    );
  }

  if (load.status === "failed") {
    return (
      <div className="nd-view nd-jn">
        <EmptyState
          title={load.failure.title}
          body={load.failure.body}
          action={
            load.failure.kind === "not-found" ? (
              <Link to="/journal" className="nd-button">
                Back to the journal
              </Link>
            ) : (
              <button type="button" className="nd-button" onClick={reload}>
                Try again
              </button>
            )
          }
        />
      </div>
    );
  }

  const { cycle, metrics, events_truncated: truncated } = load.detail;
  const caveats = cycleCaveats(cycle);
  const failures = cycleFailures(cycle, scope);
  const report = readConsolidationReport(cycle.report);
  // The service's fourth rollback refusal — "wrote no graph events" — is not
  // decidable from the cycle row, but it *is* decidable from this page's own
  // event list: `undo`'s own rule is that a `node.*`/`edge.*` event has a graph
  // effect and anything else is an audit entry. A filled window that carried
  // only audit entries proves nothing either way, so that answers `null` and
  // leaves the refusal to the preflight where it belongs.
  const wroteGraphEvents = changes.some((change) => change.subject !== "other")
    ? true
    : truncated
      ? null
      : false;
  const rollback = rollbackAvailability(cycle, wroteGraphEvents);
  // The door out of an interrupted run. `POST /api/cycles/{id}/abandon` and
  // `nodum cycle-abandon` both shipped and no surface here offered either, so a
  // cycle a crash left `running` had its writes irreversible on every surface —
  // on the one screen that displays the stuck entry.
  const abandon = abandonAvailability(cycle);
  // The kill switch. `service.request_stop` and migration `0015` shipped with no
  // CLI verb, no route and no button — and this is the screen that displays the
  // running cycle a human would want to stop. The record beside it is the other
  // half: `status` says `failed` for a run the operator stopped and for one
  // whose process died, so who asked and when is the only thing that tells the
  // two apart the next morning.
  const stop = stopAvailability(cycle);
  const stopped = stopRecord(cycle);

  return (
    <div className="nd-view nd-jn">
      <header className="nd-view__header">
        <div className="nd-jn-detail__heading">
          <p className="nd-jn-detail__back">
            <Link to="/journal">← All cycles</Link>
          </p>
          <h1 ref={heading} tabIndex={-1}>
            {cycleWork(cycle)}
          </h1>
          <p className="nd-meta">{cycleProvenance(cycle, scope)}</p>
          <p className="nd-meta">
            Started <span title={formatTimestampLong(cycle.started_at)}>{formatTimestamp(cycle.started_at)}</span>
            {cycle.finished_at === null ? (
              ", still running"
            ) : (
              <>
                {", finished "}
                <span title={formatTimestampLong(cycle.finished_at)}>
                  {formatTimestamp(cycle.finished_at)}
                </span>
              </>
            )}
            .
          </p>
          {stopped === null ? null : (
            <p className="nd-meta">
              {stopped.by} asked this run to stop{" "}
              <span title={formatTimestampLong(stopped.at)}>{formatTimestamp(stopped.at)}</span>.
              Nothing about a stop reverses what it had already written.
            </p>
          )}
          <CycleBadges cycle={cycle} />
        </div>
        <div className="nd-jn-detail__actions">
          {/* Offered only while it is possible, rather than always and mostly
              disabled — the rule the abandon control below follows too. A stop
              is for a run that is still alive; once one is recorded the control
              gives way to the record above, because a second press is a no-op in
              the service and a button that provably changes nothing is the
              screen's own version of the ambiguity that no-op exists to avoid. */}
          {stop.available ? (
            <button
              type="button"
              className="nd-button nd-button--danger"
              onClick={() => setStopping(true)}
              title="Ask this run to wind down and close its own entry"
            >
              {STOP_ACTION_LABEL}
            </button>
          ) : null}
          {/* Offered only while it is possible, rather than always and mostly
              disabled: abandoning is for one situation — a run nothing is going
              to finish — and a permanently greyed control beside the rollback
              would read as a second way to undo a cycle. */}
          {abandon.available ? (
            <button
              type="button"
              className="nd-button nd-button--danger"
              onClick={() => setAbandoning(true)}
              title="Close this interrupted entry so what it wrote can be rolled back"
            >
              {ABANDON_ACTION_LABEL}
            </button>
          ) : null}
          {/* Only while *both* buttons are on screen: the sentence names each
              one by its own label, so showing it beside a control that is no
              longer offered would point a reader at nothing. Nothing here can
              tell them which of the two they want — whether the process behind a
              `running` row is alive is not a fact the server has — so the screen
              states both situations rather than implying a preference by
              ordering or styling. */}
          {stop.available && abandon.available ? (
            <p className="nd-meta nd-jn-detail__reason">{RUNNING_ACTIONS_HINT}</p>
          ) : null}
          {/* The one refusal that is not already said by the caveats or the
              record: a running entry whose stop is in. It is rendered exactly
              where the button it replaced was, because "why is that control
              gone" is the question a reader has here and nowhere else. */}
          {abandon.available && !stop.available ? (
            <p className="nd-meta nd-jn-detail__reason">{stop.reason}</p>
          ) : null}
          <button
            type="button"
            className="nd-button nd-button--danger"
            onClick={() => setConfirming(true)}
            disabled={!rollback.available}
            title={rollback.reason ?? "Reverse every event this cycle wrote, in one action"}
          >
            Roll this cycle back
          </button>
          {rollback.reason === null ? null : (
            <p className="nd-meta nd-jn-detail__reason">{rollback.reason}</p>
          )}
        </div>
      </header>

      {failures.length > 0 ? (
        <ul className="nd-jn-failures">
          {failures.map((failure) => (
            <li key={failure}>{failure}</li>
          ))}
        </ul>
      ) : null}

      {caveats.length > 0 ? (
        <ul className="nd-jn-caveats">
          {caveats.map((caveat) => (
            <li key={caveat}>{caveat}</li>
          ))}
        </ul>
      ) : null}

      {report === null ? null : <JobReports report={report} scope={scope} />}

      <MetricTable metrics={metrics} noneNote={noMetricsNote(cycle)} />

      <EventDiff
        changes={changes}
        truncated={truncated}
        limit={CYCLE_EVENT_LIMIT}
        emptyNote={emptyEventsNote(cycle)}
      />

      {confirming ? (
        <RollbackDialog
          cycle={cycle}
          changes={changes}
          onRolledBack={onRolledBack}
          onClose={() => setConfirming(false)}
        />
      ) : null}

      {abandoning ? (
        <AbandonDialog
          cycle={cycle}
          onAbandoned={onAbandoned}
          onClose={() => setAbandoning(false)}
        />
      ) : null}

      {stopping ? (
        <StopDialog
          cycle={cycle}
          onStopRequested={onStopRequested}
          onClose={() => setStopping(false)}
        />
      ) : null}
    </div>
  );
}

/**
 * The runner's own report, job by job.
 *
 * Shown beside the event list rather than instead of it: the report says what
 * each job *examined and decided*, which the log cannot tell you — a job that
 * looked at 400 nodes and wrote nothing leaves no event at all — while the log
 * says what actually changed. The `notes` are the sentences that make the
 * numbers readable (a degraded signal, a rehearsal, a no-op that is correct),
 * so they are rendered rather than summarised.
 *
 * A job's `error` is a string the server wrote, so it goes through the same
 * space-safe reading the headline's failures do — a job that tripped over a
 * space archived mid-cycle would otherwise print the server's own *"unknown
 * space: …"* here instead.
 */
function JobReports({ report, scope }: { report: ConsolidationReport; scope: SpaceName | null }) {
  if (report.jobs.length === 0) return null;

  return (
    <section className="nd-jn-section" aria-label="The runner's report">
      <h2 className="nd-jn-section__title">Jobs</h2>
      <ul className="nd-jn-jobs">
        {report.jobs.map((job) => (
          <li key={job.name} className="nd-jn-job">
            <div className="nd-jn-job__head">
              <span className="nd-mono nd-jn-job__name">{job.name}</span>
              <span className="nd-meta">
                examined {job.examined} · proposed {job.proposed} · applied {job.applied} · skipped{" "}
                {job.skipped}
              </span>
            </div>
            {job.truncated ? (
              <p className="nd-meta nd-jn-job__warn">
                A scan reached its cap, so this job did not see everything in scope.
              </p>
            ) : null}
            {job.error === null ? null : (
              <p className="nd-jn-job__error">
                This job raised: {describeRecordedFailure(job.error, scope)}
              </p>
            )}
            {job.notes.length === 0 ? null : (
              <ul className="nd-jn-job__notes">
                {job.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export { CycleView };
