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
 * A cycle is a long synchronous write against SQLite's single writer, so the
 * button stays busy for its whole duration rather than returning optimistically.
 */

import { useState } from "react";
import { api } from "../../api/client";
import { SpaceFilter, useSpaces, useToast } from "../../components";
import { cycleWork, describeRunFailure } from "./journal";

interface RunCyclePanelProps {
  /** Called after a cycle closes, so the journal can pick the new entry up. */
  onRan: () => Promise<void> | void;
}

/**
 * Render the run-now control.
 *
 * @param onRan Refresh hook, awaited before the toast so the new entry is on
 *   screen by the time the notification names it.
 */
export function RunCyclePanel({ onRan }: RunCyclePanelProps) {
  const toast = useToast();
  const { spaces, failed } = useSpaces();
  const [scope, setScope] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [running, setRunning] = useState(false);

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
          // surface here may say about a space.
          toast.show("error", "The cycle could not be run", describeRunFailure(error, scope));
        },
      );
  };

  return (
    <section className="nd-jn-run" aria-label="Run a cycle">
      <div className="nd-jn-run__controls">
        <SpaceFilter
          value={scope}
          onChange={setScope}
          spaces={spaces}
          failed={failed}
          label="Scope"
          name="cycle-scope"
          className="nd-jn-run__scope"
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
    </section>
  );
}
