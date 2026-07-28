/**
 * Route `/journal` — the dream journal (design §9, view 6).
 *
 * What a human wakes to: *"flagged 3 duplicate candidates, proposed 14 links and
 * retired 2 stale edges"* — one legible sentence per night, newest first, each
 * opening onto the cycle's own report, its coherence metrics before and after,
 * the events it wrote, and a one-click rollback of the whole of it.
 *
 * The entries are deliberately **sentences rather than rows of ids**. A table of
 * cycle ids, triggers and timestamps is a record; this view's job is to make a
 * night's work readable at a glance, which is the difference between a journal
 * and a log. The ids are all still here — one click down, where the reviewer who
 * needs them is.
 *
 * The list is the whole screen, so a failed read is the screen's failure rather
 * than a degraded control.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { EmptyState, Spinner } from "../../components";
import type { CycleOut } from "../../api/types";
import { describeFailure, formatRelative, formatTimestampLong } from "../../lib";
import type { FailureDescription } from "../../lib";
import { CycleBadges } from "./CycleBadges";
import { CYCLE_LIST_LIMIT, cycleCaveats, cycleProvenance, cycleWork } from "./journal";
import { RunCyclePanel } from "./RunCyclePanel";
import "./journal.css";

/** Loading / loaded / failed for the journal listing. */
type LoadState =
  | { status: "loading" }
  | { status: "ready"; cycles: CycleOut[] }
  | { status: "failed"; failure: FailureDescription };

/** The journal route. Default-exported because the route is lazily loaded. */
export default function JournalView() {
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  // Bumped by the retry button and by a finished run; the only thing that
  // re-runs the read.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    api
      .listCycles(CYCLE_LIST_LIMIT, controller.signal)
      .then((cycles) => setLoad({ status: "ready", cycles }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ status: "failed", failure: describeFailure(error, "the consolidation journal") });
      });
    return () => controller.abort();
  }, [attempt]);

  const reload = useCallback(() => {
    setAttempt((count) => count + 1);
  }, []);

  return (
    <div className="nd-view nd-jn">
      <header className="nd-view__header">
        <div>
          <h1>Dream journal</h1>
          <p className="nd-meta nd-jn__subtitle">
            Every consolidation cycle the gardener has run, newest first. A cycle proposes and
            prunes; nothing it suggests is live until you accept it in{" "}
            <Link to="/review">Review</Link>. Open an entry to read what it wrote and to take the
            whole of it back in one action.
          </p>
        </div>
      </header>

      <RunCyclePanel onRan={reload} />

      {load.status === "loading" ? (
        <div className="nd-empty">
          <Spinner large label="Loading the journal" />
        </div>
      ) : null}

      {load.status === "failed" ? (
        <EmptyState
          title={load.failure.title}
          body={load.failure.body}
          action={
            <button type="button" className="nd-button" onClick={reload}>
              Try again
            </button>
          }
        />
      ) : null}

      {load.status === "ready" ? (
        load.cycles.length === 0 ? (
          <EmptyState
            title="No cycles yet"
            body="The nightly schedule is off unless it has been configured, so nothing has run on its own. Run one above to see what the gardener would do."
          />
        ) : (
          <ol className="nd-jn__list">
            {load.cycles.map((cycle) => (
              <li key={cycle.id}>
                <JournalEntry cycle={cycle} />
              </li>
            ))}
          </ol>
        )
      ) : null}
    </div>
  );
}

/** One entry in the list: the sentence, who asked, and any caveat on reading it. */
function JournalEntry({ cycle }: { cycle: CycleOut }) {
  const caveats = cycleCaveats(cycle);

  return (
    <article className="nd-card nd-jn-entry">
      <div className="nd-jn-entry__head">
        <h2 className="nd-jn-entry__work">
          <Link to={`/journal/${encodeURIComponent(cycle.id)}`}>{cycleWork(cycle)}</Link>
        </h2>
        <span className="nd-meta nd-jn-entry__when" title={formatTimestampLong(cycle.started_at)}>
          {formatRelative(cycle.started_at)}
        </span>
      </div>
      <p className="nd-meta">{cycleProvenance(cycle)}</p>
      <CycleBadges cycle={cycle} />
      {caveats.length > 0 ? (
        <ul className="nd-jn-entry__caveats">
          {caveats.map((caveat) => (
            <li key={caveat}>{caveat}</li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

export { JournalView };
