/**
 * The events a cycle wrote, rendered as a reviewable diff.
 *
 * This is the cycle's diff, and it is the append-only log itself — the report
 * carries no diff of its own precisely so the journal can never become a second
 * record that disagrees with what happened. Every row here is one event.
 *
 * **The window notice is load-bearing.** `events_truncated` means the read hit
 * its limit, and a reader about to approve a rollback of "everything below" has
 * to know they may not be looking at all of it. The flag is conservative on the
 * server — it says the list *may* be short — so the copy says exactly that and
 * no more.
 */

import { useMemo } from "react";
import type { EventOut } from "../../api/types";
import { EmptyState } from "../../components";
import { formatTimestamp, formatTimestampLong } from "../../lib";
import { describeEvent, eventWindowNote } from "./journal";

interface EventDiffProps {
  events: EventOut[];
  /** The server's conservative "the window may have cut this short" flag. */
  truncated: boolean;
  /** The window that was asked for, so the notice can name it. */
  limit: number;
  /** True for a rehearsal, whose empty list is the point rather than a gap. */
  dryRun: boolean;
}

/** Render the cycle's events, newest first. */
export function EventDiff({ events, truncated, limit, dryRun }: EventDiffProps) {
  const changes = useMemo(() => events.map(describeEvent), [events]);

  return (
    <section className="nd-jn-section" aria-label="What the cycle wrote">
      <div className="nd-jn-section__head">
        <h2 className="nd-jn-section__title">What it wrote</h2>
        {changes.length === 0 ? null : (
          <p className="nd-meta nd-jn-section__note">
            {eventWindowNote(changes.length, truncated, limit)}
          </p>
        )}
      </div>

      {changes.length === 0 ? (
        <EmptyState
          title="No events"
          body={
            dryRun
              ? "A rehearsal emits no graph event — an empty list here is the checkable form of “it changed nothing”."
              : "This cycle wrote nothing to the graph. Every job ran and none of them found anything to change."
          }
        />
      ) : (
        <ol className="nd-jn-events">
          {changes.map((change) => (
            <li key={change.seq} className={`nd-jn-event nd-jn-event--${change.shape}`}>
              <div className="nd-jn-event__head">
                <span className={`nd-badge nd-jn-shape nd-jn-shape--${change.shape}`}>
                  {change.shape}
                </span>
                <span className="nd-jn-event__headline">{change.headline}</span>
                <span className="nd-badge nd-badge--type" title={`Event op: ${change.op}`}>
                  {change.op}
                </span>
              </div>
              <p className="nd-meta nd-jn-event__meta">
                <span className="nd-mono">#{change.seq}</span>
                {" · "}
                {change.actor}
                {" · "}
                <span title={formatTimestampLong(change.createdAt)}>
                  {formatTimestamp(change.createdAt)}
                </span>
                {change.rowId === null ? null : (
                  <>
                    {" · "}
                    <span className="nd-mono nd-truncate" title={change.rowId}>
                      {change.rowId}
                    </span>
                  </>
                )}
              </p>

              {change.fields.length === 0 ? null : (
                <div className="nd-jn-scroll">
                  <table className="nd-jn-fields">
                    <caption className="nd-sr-only">
                      Fields event {change.seq} changed, before and after.
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Field</th>
                        <th scope="col">Before</th>
                        <th scope="col">After</th>
                      </tr>
                    </thead>
                    <tbody>
                      {change.fields.map((field) => (
                        <tr key={field.field}>
                          <th scope="row" className="nd-mono">
                            {field.field}
                          </th>
                          <td className="nd-jn-fields__before">
                            {field.before === null ? null : <code>{field.before}</code>}
                          </td>
                          <td className="nd-jn-fields__after">
                            {field.after === null ? null : <code>{field.after}</code>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
