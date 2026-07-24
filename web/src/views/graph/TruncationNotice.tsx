/**
 * The truncation banner.
 *
 * `subgraph` enforces its node cap *during* the walk: when the cap bites, the
 * server stops admitting nodes and sets `truncated`. What comes back is a
 * slice of the neighbourhood, not the neighbourhood — and a partial graph
 * presented as a whole one is the single most dishonest thing this view could
 * do, because nothing about the picture looks wrong.
 *
 * So truncation gets a persistent banner, not a toast: it is a property of what
 * is on screen, and it stays true until the parameters change. Every route out
 * of it is one click — raise the cap, pull the depth in, or re-root somewhere
 * more specific.
 */

import { MAX_LIMIT } from "./filters";

interface TruncationNoticeProps {
  /** Nodes actually returned — equal to the cap when it bit. */
  nodeCount: number;
  /** The cap that was sent. */
  limit: number;
  /** The depth that was walked. */
  depth: number;
  onRaiseLimit: (next: number) => void;
  onReduceDepth: (next: number) => void;
}

/**
 * Render the truncation banner.
 *
 * @param props See {@link TruncationNoticeProps}.
 */
export function TruncationNotice({
  nodeCount,
  limit,
  depth,
  onRaiseLimit,
  onReduceDepth,
}: TruncationNoticeProps) {
  const raised = Math.min(MAX_LIMIT, limit * 2);
  const canRaise = raised > limit;

  return (
    <div className="nd-graph__banner nd-graph__banner--warn" role="status">
      <div className="nd-graph__banner-text">
        <strong>Partial graph.</strong> The node cap stopped the walk at {nodeCount} node
        {nodeCount === 1 ? "" : "s"} — there is more graph past this boundary at depth {depth}.
        Edges to the nodes that were cut are not shown either.
      </div>
      <div className="nd-graph__banner-actions">
        {canRaise ? (
          <button
            type="button"
            className="nd-button nd-button--small"
            onClick={() => onRaiseLimit(raised)}
          >
            Raise cap to {raised}
          </button>
        ) : (
          <span className="nd-meta">Cap is at the {MAX_LIMIT}-node ceiling.</span>
        )}
        {depth > 1 ? (
          <button
            type="button"
            className="nd-button nd-button--small"
            onClick={() => onReduceDepth(depth - 1)}
          >
            Depth {depth - 1}
          </button>
        ) : null}
        <span className="nd-meta">or re-root on the part you want.</span>
      </div>
    </div>
  );
}
