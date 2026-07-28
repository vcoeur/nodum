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
 *
 * **A cycle's scope is an id and is never rendered as one.** `open_cycle` runs
 * `scope` through `_resolve_space` before the row is written, so every scoped
 * entry reports a 32-hex string — *"confined to the space f7394465…"* on every
 * row of the list. It goes through the shared `nameSpace` over two lists, the
 * active one and the lazy archived read, because a cycle can perfectly well have
 * run over a space retired since.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import {
  EmptyState,
  nameSpace,
  Spinner,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useSpaces,
} from "../../components";
import type { SpaceName } from "../../components";
import type { CycleOut } from "../../api/types";
import { describeFailure, formatRelative, formatTimestampLong } from "../../lib";
import type { FailureDescription } from "../../lib";
import { CycleBadges } from "./CycleBadges";
import {
  CYCLE_LIST_LIMIT,
  cycleCaveats,
  cycleFailures,
  cycleProvenance,
  cycleScopeIds,
  cycleWork,
} from "./journal";
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

  // Every scope on screen, resolved through the shared vocabulary. The archived
  // read is lazy and fires only when a cycle names a space no picker lists —
  // `spaceList.spaces` is passed through **null and all**, because reading a
  // list still in flight as empty would report every scope as unresolved and
  // fire that read on a perfectly healthy file.
  const spaceList = useSpaces();
  const scopes = useMemo(
    () => (load.status === "ready" ? cycleScopeIds(load.cycles) : []),
    [load],
  );
  const unresolved = useMemo(
    () => unresolvedSpaceIds(scopes, spaceList.spaces),
    [scopes, spaceList.spaces],
  );
  const archived = useArchivedSpaces(unresolved.length > 0);
  const spaceName = useCallback(
    (scope: string | null): SpaceName | null =>
      scope === null ? null : nameSpace(scope, spaceList.spaces, archived.spaces),
    [spaceList.spaces, archived.spaces],
  );

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

      <RunCyclePanel
        onRan={reload}
        spaces={spaceList.spaces}
        archivedSpaces={archived.spaces}
        spacesFailed={spaceList.failed}
      />

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
                <JournalEntry cycle={cycle} scope={spaceName(cycle.scope)} />
              </li>
            ))}
          </ol>
        )
      ) : null}
    </div>
  );
}

/**
 * One entry in the list: the sentence, who asked, any caveat on reading it —
 * and, when it failed, why.
 *
 * The reason is a line of its own rather than part of the headline, and that is
 * the fix rather than a layout preference: a cycle's recorded failure is a
 * string the *server* wrote, and splicing it into the link text put *"The cycle
 * failed before any job ran: TypeNotFound: unknown space: 909a3060…"* on this
 * row. `cycleFailures` is where the space-safe copy rules reach it.
 */
function JournalEntry({ cycle, scope }: { cycle: CycleOut; scope: SpaceName | null }) {
  const caveats = cycleCaveats(cycle);
  const failures = cycleFailures(cycle, scope);

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
      <p className="nd-meta">{cycleProvenance(cycle, scope)}</p>
      <CycleBadges cycle={cycle} />
      {failures.length > 0 ? (
        <ul className="nd-jn-entry__failures">
          {failures.map((failure) => (
            <li key={failure}>{failure}</li>
          ))}
        </ul>
      ) : null}
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
