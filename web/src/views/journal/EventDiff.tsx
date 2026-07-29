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
 *
 * **It pages, and the page is what gets named.** The server's cap works; the
 * presentation had none, and a 500-event cycle came out as 500 expanded diff
 * tables — 12 066 DOM nodes, 79 055 px, past what Chrome will screenshot. A
 * nightly cycle on a real graph reaches that cap by design. Paging rather than
 * collapsing, because the endpoint titles below are one request per node: what
 * is rendered is what is fetched.
 *
 * **An edge is named by its endpoints, not by their ids.** Every event a
 * consolidation cycle writes is an edge, so a diff that printed
 * `duplicate_of: cba85bd8… → 9310f1b3…` made the headline feature of this view
 * unreadable and unclickable — while the review queue, on the same build, showed
 * the same two rows as *"event sourcing → Event Sourcing"*. The titles are
 * reachable; `useNodeTitles` fetches them for the page on screen, and until they
 * land the shortened id stands in.
 */

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "../../components";
import { formatTimestamp, formatTimestampLong } from "../../lib";
import {
  endpointLabel,
  EVENT_PAGE_SIZE,
  eventWindow,
  eventWindowNote,
  referencedNodeIds,
} from "./journal";
import type { EventChange, EventWindow } from "./journal";
import { useNodeTitles } from "./useNodeTitles";

interface EventDiffProps {
  /**
   * Every event reduced, newest first.
   *
   * Passed in rather than derived here: the page above reads the same reduction
   * to decide whether a rollback is offered at all and to name the rows in the
   * confirm dialog, and three readings of one log must not disagree.
   */
  changes: EventChange[];
  /** The server's conservative "the window may have cut this short" flag. */
  truncated: boolean;
  /** The window that was asked for, so the notice can name it. */
  limit: number;
  /** The empty-list sentence for this cycle — three cases, `journal.emptyEventsNote`. */
  emptyNote: string;
}

/** Render the cycle's events, newest first, one page at a time. */
export function EventDiff({ changes, truncated, limit, emptyNote }: EventDiffProps) {
  const [page, setPage] = useState(0);
  // A rollback reloads the entry, and the list it comes back with is a
  // different list; leaving the reader on page 12 of it would be arbitrary.
  useEffect(() => setPage(0), [changes]);

  const view = eventWindow(changes.length, page, EVENT_PAGE_SIZE);
  const shown = useMemo(
    () => changes.slice(view.from, view.to),
    [changes, view.from, view.to],
  );
  const wanted = useMemo(() => referencedNodeIds(shown), [shown]);
  const titles = useNodeTitles(wanted);

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
        <EmptyState title="No events" body={emptyNote} />
      ) : (
        <>
          {view.pages > 1 ? <EventPager view={view} onPage={setPage} /> : null}

          <ol className="nd-jn-events">
            {shown.map((change) => (
              <li key={change.seq} className={`nd-jn-event nd-jn-event--${change.shape}`}>
                <div className="nd-jn-event__head">
                  <span className={`nd-badge nd-jn-shape nd-jn-shape--${change.shape}`}>
                    {change.shape}
                  </span>
                  <span className="nd-jn-event__headline" title={change.headline}>
                    <EventSubjectLabel change={change} titles={titles} />
                  </span>
                  <span className="nd-badge nd-badge--type" title={`Event op: ${change.op}`}>
                    {change.op}
                  </span>
                </div>
                <p className="nd-meta nd-jn-event__meta">
                  <span className="nd-mono">#{change.seq}</span>
                  {" · "}
                  {/* The actor string verbatim, deliberately: this line is the
                      log entry, and `agent:builtin-gardener` is the answer to
                      "who is answerable for this write". The prose around it —
                      the provenance sentence, the rollback confirm — names the
                      principal instead. */}
                  <span className="nd-mono">{change.actor}</span>
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

          {view.pages > 1 ? <EventPager view={view} onPage={setPage} /> : null}
        </>
      )}
    </section>
  );
}

/**
 * What one event is about, named rather than spelt.
 *
 * An edge becomes `duplicate_of: <source> → <target>`, both ends linked into the
 * editor — which is the whole difference between a diff a reviewer can act on
 * and a column of hex. A node links to itself. An audit entry has no row and
 * stays the op it was.
 */
function EventSubjectLabel({
  change,
  titles,
}: {
  change: EventChange;
  /** Resolved node titles; an absent key means the lookup has not answered. */
  titles: ReadonlyMap<string, string | null>;
}) {
  if (change.edge !== null) {
    return (
      <>
        <span className="nd-mono nd-jn-event__type">{change.edge.type}</span>
        {": "}
        <NodeRef nodeId={change.edge.srcId} titles={titles} />
        {" → "}
        <NodeRef nodeId={change.edge.dstId} titles={titles} />
      </>
    );
  }
  if (change.subject === "node" && change.rowId !== null) {
    return (
      <Link to={`/editor/${encodeURIComponent(change.rowId)}`} title={change.rowId}>
        {change.headline}
      </Link>
    );
  }
  return <>{change.headline}</>;
}

/** One endpoint: its title once it is known, its shortened id until then. */
function NodeRef({
  nodeId,
  titles,
}: {
  nodeId: string | null;
  titles: ReadonlyMap<string, string | null>;
}) {
  if (nodeId === null) return <span className="nd-meta">unknown</span>;
  const label = endpointLabel(nodeId, titles.get(nodeId));
  return (
    <Link to={`/editor/${encodeURIComponent(nodeId)}`} title={nodeId}>
      {label}
    </Link>
  );
}

/** Previous / next over the event pages, with the range it is showing. */
function EventPager({
  view,
  onPage,
}: {
  /** The slice currently rendered, from `journal.eventWindow`. */
  view: EventWindow;
  onPage: (page: number) => void;
}) {
  return (
    <div className="nd-row nd-jn-pager">
      <button
        type="button"
        className="nd-button nd-button--small"
        onClick={() => onPage(view.page - 1)}
        disabled={view.page === 0}
      >
        ← Newer
      </button>
      <span className="nd-meta nd-jn-pager__range">
        {view.label} · page {view.page + 1} of {view.pages}
      </span>
      <button
        type="button"
        className="nd-button nd-button--small"
        onClick={() => onPage(view.page + 1)}
        disabled={view.page >= view.pages - 1}
      >
        Older →
      </button>
    </div>
  );
}
