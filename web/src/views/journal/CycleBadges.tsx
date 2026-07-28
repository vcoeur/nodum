/**
 * The pills that classify one journal entry at a glance.
 *
 * Deliberately **not** `<NodeBadge>`: a cycle's `status` is not the service
 * layer's `proposed → active → archived` state machine, and borrowing that
 * ramp's colours would say something false about a row that has no lifecycle
 * state at all. These carry the feedback hues instead — running is in progress,
 * failed is a failure, a rehearsal changed nothing — and the reverted pill takes
 * the lowest-contrast colour in the system, because a cycle that has been taken
 * back should recede exactly as an archived row does.
 */

import type { CycleOut } from "../../api/types";
import { shortId } from "./journal";

/** Render the trigger, the status, and the two facts that qualify both. */
export function CycleBadges({ cycle }: { cycle: CycleOut }) {
  return (
    <div className="nd-row nd-jn-badges">
      <span className="nd-badge nd-badge--type" title={`Cycle ${cycle.id}`}>
        {cycle.trigger}
      </span>
      <span className={`nd-badge ${statusClass(cycle.status)}`} title={`Status: ${cycle.status}`}>
        <span className="nd-badge__dot" aria-hidden="true" />
        {cycle.status.replace("_", " ")}
      </span>
      {cycle.dry_run ? (
        <span
          className="nd-badge nd-jn-badge--rehearsal"
          title="Every job ran and no graph event was written"
        >
          rehearsal
        </span>
      ) : null}
      {cycle.rolled_back_by === null ? null : (
        <span
          className="nd-badge nd-jn-badge--reverted"
          title={`Reversed by cycle ${cycle.rolled_back_by}`}
        >
          reversed by {shortId(cycle.rolled_back_by)}
        </span>
      )}
      <span className="nd-mono nd-jn-badges__id" title={cycle.id}>
        {shortId(cycle.id)}
      </span>
    </div>
  );
}

/** The pill class for one cycle status. */
function statusClass(status: string): string {
  if (status === "running") return "nd-jn-badge--running";
  if (status === "failed") return "nd-jn-badge--failed";
  if (status === "rolled_back") return "nd-jn-badge--reverted";
  return "nd-jn-badge--done";
}
