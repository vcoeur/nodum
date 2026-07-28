/**
 * The "run one now" control.
 *
 * The nightly schedule is **off unless configured** (design decision J1 — a
 * background process that writes to the graph without being asked is not
 * something to enable by surprise), so without this control a fresh install's
 * journal would be empty forever and the whole view would have nothing to show.
 *
 * Both parameters are the runner's own and this panel invents neither: `scope`
 * confines the cycle to one space, and `dry_run` rehearses it — every job
 * computed, the report written, and no graph event emitted. The rehearsal is
 * worth a control of its own rather than a separate screen, because it is the
 * only way to see what a cycle *would* do on a file whose contents you are not
 * yet willing to let an agent touch.
 *
 * **The scope control is a `SpaceFilter` doing a different job, and it says so.**
 * Everywhere else that component narrows a *read* and promises it "never widens
 * what you can see"; here the choice decides what the gardener will **act on**,
 * so the tooltip is the panel's rather than the component's default. The
 * vocabulary is narrowed too: `meta` is dropped, because
 * `consolidate._is_curatable` excludes every node in it and a cycle scoped there
 * is a guaranteed no-op that reports itself as a clean night.
 */

import { useMemo, useState } from "react";
import { api } from "../../api/client";
import { nameSpace, SpaceFilter, useToast } from "../../components";
import type { NodeOut, SpaceOut } from "../../api/types";
import {
  cycleScopeSpaces,
  cycleWork,
  describeRunFailure,
  SCOPE_CONTROL_HINT,
} from "./journal";

interface RunCyclePanelProps {
  /** Called after a cycle closes, so the journal can pick the new entry up. */
  onRan: () => Promise<void> | void;
  /** Active spaces from the shared read, or null while it is unknown. */
  spaces: SpaceOut[] | null;
  /** Archived space nodes, for naming a scope retired since the picker filled. */
  archivedSpaces: readonly NodeOut[];
  /** True once the space read failed — the picker says so rather than offering "any". */
  spacesFailed: boolean;
}

/**
 * Render the run-now control.
 *
 * @param onRan Refresh hook, awaited before the toast so the new entry is on
 *   screen by the time the notification names it.
 * @param spaces The active space list, or null while it is unknown. Passed in
 *   rather than read here: the view around this panel already holds it for the
 *   entries' own scopes, and a second `GET /api/spaces` on one screen is the
 *   duplication `useSpaces` exists to have collapsed.
 * @param archivedSpaces Archived space nodes, for the one case that matters
 *   here — a scope archived between the picker being filled and the button
 *   being pressed, which is the live way this run gets refused.
 * @param spacesFailed Whether that read failed.
 */
export function RunCyclePanel({
  onRan,
  spaces,
  archivedSpaces,
  spacesFailed,
}: RunCyclePanelProps) {
  const toast = useToast();
  const [scope, setScope] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [running, setRunning] = useState(false);

  const choosable = useMemo(() => cycleScopeSpaces(spaces), [spaces]);

  const run = () => {
    setRunning(true);
    void api
      .runCycle({ ...(scope === "" ? {} : { scope }), dry_run: dryRun })
      .then(
        async (result) => {
          setRunning(false);
          await onRan();
          toast.show(
            result.cycle.status === "failed" ? "error" : "success",
            dryRun ? "Rehearsal finished" : "Cycle finished",
            cycleWork(result.cycle),
          );
        },
        (error: unknown) => {
          setRunning(false);
          // Through the scope-aware wording, never `toast.showError`: that
          // renders an ApiError as `type: message` and would print the server's
          // own "unknown space: research" verbatim — the one phrasing no
          // surface here may say about a space. The scope is *named* rather
          // than quoted, because the picker's value is a space id.
          toast.show(
            "error",
            "The cycle could not be run",
            describeRunFailure(
              error,
              scope === "" ? null : nameSpace(scope, spaces, archivedSpaces),
            ),
          );
        },
      );
  };

  return (
    <section className="nd-jn-run" aria-label="Run a cycle">
      <div className="nd-jn-run__controls">
        <SpaceFilter
          value={scope}
          onChange={setScope}
          spaces={choosable}
          archivedSpaces={archivedSpaces}
          failed={spacesFailed}
          label="Scope"
          name="cycle-scope"
          className="nd-jn-run__scope"
          title={SCOPE_CONTROL_HINT}
        />
        <label className="nd-jn-run__rehearse">
          <input
            type="checkbox"
            name="cycle-dry-run"
            checked={dryRun}
            onChange={(event) => setDryRun(event.target.checked)}
          />
          <span>Rehearse only</span>
        </label>
        <button
          type="button"
          className="nd-button nd-button--primary"
          onClick={run}
          disabled={running}
        >
          {running ? "Running…" : dryRun ? "Rehearse a cycle" : "Run a cycle now"}
        </button>
      </div>
      <p className="nd-meta nd-jn-run__note">
        {dryRun
          ? "A rehearsal computes every job and writes no graph event, so nothing lands in the review queue. It still appears here, marked as a rehearsal."
          : "The gardener proposes duplicates and links and retires the two kinds of edge a machine can be right about. Everything it suggests waits in the review queue. A cycle holds the single database writer while it runs."}
      </p>
      <p className="nd-meta nd-jn-run__note">
        The scope picker leaves out <code>meta</code>: consolidation skips every node there — it
        is the vocabulary and the territory, not knowledge — so a cycle confined to it would
        examine nothing and report a clean night.
      </p>
    </section>
  );
}
