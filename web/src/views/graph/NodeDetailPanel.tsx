/**
 * The detail panel for a clicked node.
 *
 * It describes the node itself and *its place in the rendered subgraph* — the
 * incident-edge list is scoped to what is on screen, not to the database. That
 * distinction matters here more than anywhere else in the view: this panel is
 * where a filtered graph would most easily be mistaken for a complete one, so
 * the heading says "in this view" and means it.
 */

import { Link } from "react-router-dom";
import { NodeBadge } from "../../components";
import type { NodeOut } from "../../api/types";
import { formatAbsolute, formatTimestampLong } from "../../lib";
import type { IncidentEdge } from "./graphElements";

interface NodeDetailPanelProps {
  node: NodeOut;
  /** True when this node is the subgraph's root. */
  isRoot: boolean;
  /** True when a node-type filter is on and this root would not have passed it. */
  rootExemptFromTypeFilter: boolean;
  incident: IncidentEdge[];
  /** Link target for "re-root here", carrying the current filters. */
  rerootTo: string;
  /** Select another node (from the incident-edge list). */
  onSelect: (nodeId: string) => void;
  /** Put this node at one end of the path query. */
  onSetPathEnd: (end: "a" | "b", nodeId: string) => void;
  /** Which path role this node already fills, if any. */
  pathRole: "a" | "b" | null;
  onClose: () => void;
}

/** How much node content the panel previews. */
const EXCERPT_LIMIT = 260;

/** Collapse the content to a single-paragraph preview. */
function excerpt(content: string): string | null {
  const flattened = content.replace(/\s+/g, " ").trim();
  if (!flattened) return null;
  return flattened.length > EXCERPT_LIMIT
    ? `${flattened.slice(0, EXCERPT_LIMIT - 1)}…`
    : flattened;
}

/**
 * Render the node detail panel.
 *
 * @param props See {@link NodeDetailPanelProps}.
 */
export function NodeDetailPanel({
  node,
  isRoot,
  rootExemptFromTypeFilter,
  incident,
  rerootTo,
  onSelect,
  onSetPathEnd,
  pathRole,
  onClose,
}: NodeDetailPanelProps) {
  const preview = excerpt(node.content);

  return (
    <section className="nd-graph__panel-section" aria-label="Selected node">
      <header className="nd-graph__panel-header">
        <div className="nd-stack" style={{ ["--nd-stack-gap" as string]: "var(--nd-space-2)" }}>
          <h2 className="nd-graph__panel-title">{node.title ?? "(untitled)"}</h2>
          <NodeBadge type={node.type} state={node.state} />
        </div>
        <button
          type="button"
          className="nd-button nd-button--ghost nd-button--small"
          onClick={onClose}
          aria-label="Close the detail panel"
        >
          ×
        </button>
      </header>

      {isRoot ? (
        <p className="nd-graph__hint">
          This is the root of the walk, and always the first node returned.
          {rootExemptFromTypeFilter
            ? " Its type is outside the node-type filter — the root is exempt, so it is shown anyway."
            : ""}
        </p>
      ) : null}

      <dl className="nd-graph__facts">
        <dt>id</dt>
        <dd className="nd-mono">{node.id}</dd>
        <dt>created by</dt>
        <dd className="nd-mono">{node.created_by}</dd>
        {/* Through `lib/time.ts`, like every other timestamp: the server sends
            SQLite's zone-less UTC, which a raw render leaves the reader to
            misread as their own clock. */}
        <dt>created</dt>
        <dd className="nd-mono" title={formatTimestampLong(node.created_at)}>
          {formatAbsolute(node.created_at)}
        </dd>
        <dt>updated</dt>
        <dd className="nd-mono" title={formatTimestampLong(node.updated_at)}>
          {formatAbsolute(node.updated_at)}
        </dd>
        {node.parent_id ? (
          <>
            <dt>parent</dt>
            <dd>
              <button
                type="button"
                className="nd-graph__link-button nd-mono"
                onClick={() => onSelect(node.parent_id as string)}
              >
                {node.parent_id}
              </button>
            </dd>
          </>
        ) : null}
      </dl>

      {preview ? <p className="nd-graph__excerpt">{preview}</p> : null}

      <div className="nd-graph__panel-actions">
        <Link className="nd-button nd-button--small" to={`/editor/${encodeURIComponent(node.id)}`}>
          Open in editor
        </Link>
        {isRoot ? null : (
          <Link className="nd-button nd-button--small nd-button--primary" to={rerootTo}>
            Re-root here
          </Link>
        )}
        <button
          type="button"
          className="nd-button nd-button--small"
          onClick={() => onSetPathEnd("a", node.id)}
          disabled={pathRole === "a"}
        >
          Path from here
        </button>
        <button
          type="button"
          className="nd-button nd-button--small"
          onClick={() => onSetPathEnd("b", node.id)}
          disabled={pathRole === "b"}
        >
          Path to here
        </button>
      </div>

      <div className="nd-graph__edges">
        <h3 className="nd-label">
          {incident.length} edge{incident.length === 1 ? "" : "s"} in this view
        </h3>
        {incident.length === 0 ? (
          <p className="nd-graph__hint">
            Nothing connects to it under the current filters. Widen the depth, add edge states, or
            clear a type filter.
          </p>
        ) : (
          <ul className="nd-graph__edge-list">
            {incident.map(({ edge, direction, other }) => (
              <li key={edge.id}>
                <button
                  type="button"
                  className="nd-graph__edge-row"
                  onClick={() => other && onSelect(other.id)}
                  disabled={other === null}
                >
                  <span className="nd-mono nd-graph__edge-dir">
                    {direction === "out" ? "→" : "←"}
                  </span>
                  <span className="nd-mono nd-graph__edge-type">{edge.type}</span>
                  <span className="nd-truncate nd-graph__edge-other">
                    {other?.title ?? other?.id ?? "(missing)"}
                  </span>
                  <NodeBadge state={edge.state} stateOnly />
                  <span className="nd-mono nd-graph__edge-confidence">
                    {edge.confidence === null ? "—" : edge.confidence.toFixed(2)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {incident.some(({ edge }) => edge.confidence === null) ? (
          <p className="nd-graph__hint">
            “—” means no stated confidence. A confidence floor would drop those edges.
          </p>
        ) : null}
      </div>
    </section>
  );
}
