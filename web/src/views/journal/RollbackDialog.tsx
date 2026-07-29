/**
 * The confirm in front of rolling a whole cycle back.
 *
 * **It calls the API before it asks.** `POST /api/cycles/{id}/rollback` with
 * `dry_run: true` opens no cycle, writes nothing, and answers under a **200**
 * rather than raising — which is the "would this succeed?" a confirm dialog
 * exists for. So this dialog runs the rehearsal the moment it opens and shows
 * its verdict: how many events would be reversed, or what stands in the way. A
 * human meeting a refusal here has lost nothing; a human meeting it after
 * pressing the button has already decided.
 *
 * **Two things can stand in the way, and they are shown apart.** A *conflict* is
 * the graph having moved on — a later write changed a row this cycle wrote, so
 * reversing would overwrite that work. A *blocker* is the graph having grown
 * onto a row this cycle created — a child, an occupant, a grant, a type in use —
 * so the delete that reverses the create would cascade past what the reversal
 * was asked to touch. They have different answers (go and look at the later
 * work, versus take the dependants back first), so a single merged list would
 * tell the reader that one thing had happened twice. The verdict is clean only
 * when **both** lists are empty: a confirm that checked conflicts alone offered
 * this button for a rollback that then failed at apply time.
 *
 * A real 409 is still handled, because it is a real race: the preflight and the
 * commit are two requests, and the graph can move between them. Its conflicts
 * come back in the error body verbatim and are rendered exactly as the
 * preflight's are, under wording that says which of the two happened. The 409
 * body carries conflicts only — a guard met mid-commit refuses as
 * `UndoNotPossible`, one sentence and no list, and reaches the error toast.
 *
 * Modelled on `views/spaces/ArchiveDialog.tsx` — busy state on the affirmative
 * button, the dialog left standing on failure, Escape and the backdrop
 * cancelling, nothing confirming on a keypress (the `Modal` contract).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, isRollbackConflict } from "../../api/client";
import { Modal, Spinner, useToast } from "../../components";
import type {
  CycleOut,
  RollbackBlockerOut,
  RollbackConflictOut,
  RollbackOut,
} from "../../api/types";
import { describeError, describeFailure } from "../../lib";
import type { FailureDescription } from "../../lib";
import { rollbackPlan, rollbackRefusal, rowHeadlines, shortId, verdictNodeIds } from "./journal";
import type { EventChange } from "./journal";
import { useNodeTitles } from "./useNodeTitles";

/**
 * Where the dialog is: rehearsing, showing the dry run's verdict, showing the
 * 409 race's, or refusing to plan at all.
 *
 * It holds the server's **raw** answer rather than a rendered plan, because
 * naming the rows takes a lookup that lands after the verdict does — see the
 * titles below. The plan is derived each render from whatever has resolved so
 * far, so a title arriving simply renames a row already on screen.
 */
type PreflightState =
  | { status: "checking" }
  | { status: "ready"; result: RollbackOut }
  | { status: "refused"; conflicts: RollbackConflictOut[] }
  | { status: "failed"; failure: FailureDescription };

/** Stable empties, so the verdict memo below does not change identity per render. */
const NO_CONFLICTS: RollbackConflictOut[] = [];
const NO_BLOCKERS: RollbackBlockerOut[] = [];

interface RollbackDialogProps {
  /** The cycle being taken back. */
  cycle: CycleOut;
  /**
   * The cycle's own events, reduced — the page's record of what it wrote.
   *
   * The server reports a conflict by row id, because it is reporting on the
   * `nodes`/`edges` tables. The page behind this dialog is not: every
   * conflicting row is a row the cycle wrote, so its event is in this list and
   * that payload already carries the title. Showing `e1a2b3c4…` while the list
   * two inches away says *"Meeting 2026-07-01"* makes the reader do a lookup the
   * page had already done.
   *
   * Passed as the events rather than as a namer, because for an **edge** the
   * event carries two endpoint *ids* and the titles have to be fetched — the
   * same lookup the diff behind this dialog is already doing for its own page.
   * Handed a pre-built namer, this dialog printed `relates_to: 19c082d3… →
   * db24d36d…` for an edge the list rendered as *"event sourcing → Event
   * Sourcing"*.
   */
  changes: readonly EventChange[];
  /** Called with the result once a rollback has actually happened. */
  onRolledBack: (result: RollbackOut) => Promise<void> | void;
  /** Cancel handler for every dismissal route. */
  onClose: () => void;
}

/**
 * Confirm a rollback, having first asked the server whether it would work.
 *
 * @param cycle The cycle being taken back.
 * @param changes The cycle's reduced events; see the prop's own note.
 * @param onRolledBack Called with the outcome; the dialog closes after it.
 * @param onClose Cancel handler.
 */
export function RollbackDialog({ cycle, changes, onRolledBack, onClose }: RollbackDialogProps) {
  const toast = useToast();
  const [preflight, setPreflight] = useState<PreflightState>({ status: "checking" });
  const [committing, setCommitting] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api
      .rollbackCycle(cycle.id, { dryRun: true }, controller.signal)
      .then((result) => setPreflight({ status: "ready", result }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        // Every refusal other than a conflict is raised on the dry run too — a
        // cycle still running, one already rolled back, one that wrote no graph
        // event. Those are refusals to *plan*, so they belong here rather than
        // as a toast over a dialog still offering the button.
        setPreflight({ status: "failed", failure: describeFailure(error, "this cycle") });
      });
    return () => controller.abort();
  }, [cycle.id]);

  // The rows the verdict names, whichever of the two verdicts is on screen.
  const verdict = useMemo(() => {
    if (preflight.status === "ready") {
      return { conflicts: preflight.result.conflicts, blockers: preflight.result.blockers };
    }
    if (preflight.status === "refused") {
      return { conflicts: preflight.conflicts, blockers: NO_BLOCKERS };
    }
    return { conflicts: NO_CONFLICTS, blockers: NO_BLOCKERS };
  }, [preflight]);

  // One lookup, bounded by the verdict rather than by the cycle: a clean
  // rollback names no rows and fetches nothing at all.
  const wanted = useMemo(
    () => verdictNodeIds(verdict.conflicts, verdict.blockers, changes),
    [verdict, changes],
  );
  const titles = useNodeTitles(wanted);
  const rowNames = useMemo(() => rowHeadlines(changes, titles), [changes, titles]);
  const nameRow = useCallback(
    // Two sources, in order: a row this cycle wrote is called what its own event
    // calls it; a dependant is a row outside the cycle, so only the direct
    // title lookup can name one.
    (rowId: string) => rowNames.get(rowId) ?? titles.get(rowId) ?? null,
    [rowNames, titles],
  );

  const commit = () => {
    setCommitting(true);
    void api.rollbackCycle(cycle.id, { dryRun: false }).then(
      async (result) => {
        setCommitting(false);
        await onRolledBack(result);
        onClose();
      },
      (error: unknown) => {
        setCommitting(false);
        if (isRollbackConflict(error)) {
          // The race: the preflight passed and the graph moved before the
          // commit. Nothing was written, so the dialog stays up with the new
          // verdict rather than reporting a failure and closing.
          setPreflight({ status: "refused", conflicts: error.conflicts });
          return;
        }
        toast.show("error", "The rollback did not happen", describeError(error));
      },
    );
  };

  const plan =
    preflight.status === "ready"
      ? rollbackPlan(preflight.result, nameRow)
      : preflight.status === "refused"
        ? rollbackRefusal(preflight.conflicts, nameRow)
        : null;
  const canConfirm = plan !== null && !plan.blocked && !committing;

  return (
    <Modal
      title={`Roll back cycle ${shortId(cycle.id)}?`}
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="nd-button nd-button--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="nd-button nd-button--danger"
            onClick={commit}
            disabled={!canConfirm}
          >
            {committing ? "Rolling back…" : "Roll the cycle back"}
          </button>
        </>
      }
    >
      {preflight.status === "checking" ? (
        <div className="nd-empty">
          <Spinner large label="Checking whether this can be rolled back" />
        </div>
      ) : null}

      {preflight.status === "failed" ? (
        <div className="nd-jn-verdict nd-jn-verdict--blocked">
          <p className="nd-jn-verdict__headline">{preflight.failure.title}</p>
          <p>{preflight.failure.body}</p>
        </div>
      ) : null}

      {plan === null ? null : (
        <>
          <div
            className={
              plan.blocked
                ? "nd-jn-verdict nd-jn-verdict--blocked"
                : "nd-jn-verdict nd-jn-verdict--clear"
            }
          >
            <p className="nd-jn-verdict__headline">{plan.headline}</p>
            <p>{plan.detail}</p>
          </div>

          {plan.conflicts.length === 0 && plan.blockers.length === 0 ? (
            <p className="nd-meta">
              Rolling back writes the recorded payloads back verbatim, across every space the cycle
              touched. It is not undone by undo — a rollback is itself a cycle, and rolling{" "}
              <em>that</em> back re-applies this one.
            </p>
          ) : null}

          {plan.conflicts.length === 0 ? null : (
            <section className="nd-jn-standoff">
              <h3 className="nd-jn-standoff__title">Moved since this cycle ran</h3>
              <p className="nd-meta nd-jn-standoff__note">
                Something outside this cycle has written to a row it wrote. Reversing would put the
                cycle&rsquo;s own payload back over that work — go and look at it first.
              </p>
              <ul className="nd-jn-conflicts">
                {plan.conflicts.map((conflict) => (
                  <li key={`${conflict.rowId}:${conflict.sinceDid}`} className="nd-jn-conflict">
                    <p className="nd-jn-conflict__row">
                      <span className="nd-badge nd-badge--type">{conflict.kind}</span>
                      {conflict.name === null ? null : (
                        <span className="nd-jn-conflict__name">{conflict.name}</span>
                      )}
                      <span className="nd-mono nd-truncate" title={conflict.rowId}>
                        {conflict.name === null ? conflict.rowId : shortId(conflict.rowId, 12)}
                      </span>
                    </p>
                    <dl className="nd-jn-conflict__facts">
                      <dt>This cycle</dt>
                      <dd>{conflict.cycleDid}</dd>
                      <dt>Changed since by</dt>
                      <dd>
                        {conflict.sinceDid} — {conflict.who}
                        {conflict.inCycle === null
                          ? ", outside any cycle"
                          : `, in cycle ${shortId(conflict.inCycle)}`}
                      </dd>
                    </dl>
                    {conflict.inCycle === null ? null : (
                      <p className="nd-meta">
                        Rolling that cycle back may clear this one out of the way.
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {plan.blockers.length === 0 ? null : (
            <section className="nd-jn-standoff">
              <h3 className="nd-jn-standoff__title">Depended on since this cycle ran</h3>
              <p className="nd-meta nd-jn-standoff__note">
                This cycle created these rows and something outside it points at them now. Reversing
                a create deletes the row, and the deletion will not cascade onto rows this cycle
                never wrote — so take the dependants back first, then roll this cycle back.
              </p>
              <ul className="nd-jn-conflicts">
                {plan.blockers.map((blocker) => (
                  <li key={`${blocker.rowId}:${blocker.cycleDid}`} className="nd-jn-conflict">
                    <p className="nd-jn-conflict__row">
                      <span className="nd-badge nd-badge--type">{blocker.kind}</span>
                      {blocker.name === null ? null : (
                        <span className="nd-jn-conflict__name">{blocker.name}</span>
                      )}
                      <span className="nd-mono nd-truncate" title={blocker.rowId}>
                        {blocker.name === null ? blocker.rowId : shortId(blocker.rowId, 12)}
                      </span>
                    </p>
                    <dl className="nd-jn-conflict__facts">
                      <dt>This cycle</dt>
                      <dd>{blocker.cycleDid} — it created this row</dd>
                      <dt>Depended on by</dt>
                      <dd>
                        <ul className="nd-jn-dependants">
                          {blocker.dependants.map((dependant) => (
                            <li key={dependant.id} className="nd-truncate" title={dependant.id}>
                              {dependant.label}
                            </li>
                          ))}
                        </ul>
                        {blocker.dependantCount > blocker.dependants.length ? (
                          <p className="nd-meta">
                            …and {blocker.dependantCount - blocker.dependants.length} more,{" "}
                            {blocker.dependantCount} in all.
                          </p>
                        ) : null}
                      </dd>
                      <dt>The run refuses with</dt>
                      <dd>{blocker.reason}</dd>
                    </dl>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </Modal>
  );
}
