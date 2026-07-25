/**
 * The truncation banner.
 *
 * `subgraph` enforces **two** caps during the walk and sets `truncated` when
 * either bites. The node cap (`limit`) stops it admitting nodes; the edge cap
 * (`limit * SUBGRAPH_EDGE_FACTOR`, server-side) stops it collecting edges,
 * because a node cap bounds nodes only — one pair of nodes can carry hundreds
 * of edges between them and satisfy a cap of two.
 *
 * What comes back either way is a slice of the neighbourhood, not the
 * neighbourhood — and a partial graph presented as a whole one is the single
 * most dishonest thing this view could do, because nothing about the picture
 * looks wrong.
 *
 * So truncation gets a persistent banner, not a toast: it is a property of what
 * is on screen, and it stays true until the parameters change. It also has to
 * name the *right* cap. `truncated` does not say which one bit, but the node
 * count does: the walk stops admitting at exactly `limit`, so a count below it
 * means the edge cap is what ended the walk. Blaming the node cap there is
 * worse than saying nothing — the reader raises it, sees no change, and learns
 * the banner cannot be trusted.
 */

import { MAX_LIMIT } from "./filters";

/** Which of the server's two caps ended the walk. */
export type TruncationCause = "node-cap" | "edge-cap";

/**
 * Work out which cap bit, from the only evidence the response carries.
 *
 * `SubgraphOut` has one boolean for two caps, so the cause has to be inferred.
 * The walk admits nodes up to `limit` and then stops, so a full house means the
 * node cap ended it; anything short of one means the walk still had node budget
 * left and something else stopped it — the edge cap.
 *
 * `>=` rather than `===` deliberately: the server clamps `limit` to
 * `MAX_SUBGRAPH_LIMIT`, so a URL asking for more gets a node count *below* the
 * limit it sent while the node cap is exactly what bit. Reading that as the edge
 * cap would be the same lie in the other direction.
 *
 * @param nodeCount Nodes actually returned.
 * @param limit The node cap that was sent.
 */
export function truncationCause(nodeCount: number, limit: number): TruncationCause {
  return nodeCount >= Math.min(limit, MAX_LIMIT) ? "node-cap" : "edge-cap";
}

interface TruncationNoticeProps {
  /** Nodes actually returned — equal to the cap when the *node* cap bit. */
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
  const nodeCapBit = truncationCause(nodeCount, limit) === "node-cap";

  return (
    <div className="nd-graph__banner nd-graph__banner--warn" role="status">
      <div className="nd-graph__banner-text">
        <strong>Partial graph.</strong>{" "}
        {nodeCapBit ? (
          <>
            The node cap stopped the walk at {nodeCount} node
            {nodeCount === 1 ? "" : "s"} — there is more graph past this boundary at depth {depth}.
            Edges to the nodes that were cut are not shown either.
          </>
        ) : (
          <>
            The edge cap stopped the walk — this root has more edges than the view will draw at
            once, so some are missing between the {nodeCount} node{nodeCount === 1 ? "" : "s"}{" "}
            shown. Raising the node cap raises the edge cap with it; pulling the depth in or
            re-rooting narrows what has to be drawn.
          </>
        )}
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
