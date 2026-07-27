/**
 * The detail panel for a clicked node.
 *
 * It describes the node itself and *its place in the rendered subgraph* — the
 * incident-edge list is scoped to what is on screen, not to the database. That
 * distinction matters here more than anywhere else in the view: this panel is
 * where a filtered graph would most easily be mistaken for a complete one, so
 * the heading says "in this view" and means it.
 *
 * It is also where a dimmed node lands. D5 keeps the far endpoint of a crossing
 * clickable, and a click that opened a panel saying nothing about *why* the
 * node was faint would have been a half-kept promise — so the panel names the
 * node's space, says when that is outside the current filter, and marks each
 * incident edge that leaves the space.
 */

import { Link } from "react-router-dom";
import { NodeBadge, nameSpace, spaceNameNote } from "../../components";
import type { NodeOut } from "../../api/types";
import { formatAbsolute, formatTimestampLong } from "../../lib";
import type { IncidentEdge } from "./graphElements";

interface NodeDetailPanelProps {
  node: NodeOut;
  /** True when this node is the subgraph's root. */
  isRoot: boolean;
  /** True when a node-type filter is on and this root would not have passed it. */
  rootExemptFromTypeFilter: boolean;
  /** Active spaces, for naming this node's; null before the list answers. */
  spaces: readonly NodeOut[] | null;
  /**
   * Archived space nodes, from the lazy read: a subgraph reaches nodes written
   * into a space that has since been retired, and the panel names those rather
   * than printing the 32-hex id it used to.
   */
  archivedSpaces: readonly NodeOut[];
  /** The space the view is narrowed to, resolved to an id; `""` for no filter. */
  filteredSpace: string;
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
  spaces,
  archivedSpaces,
  filteredSpace,
  incident,
  rerootTo,
  onSelect,
  onSetPathEnd,
  pathRole,
  onClose,
}: NodeDetailPanelProps) {
  const preview = excerpt(node.content);
  const outsideFilter = filteredSpace !== "" && node.space_id !== filteredSpace;
  const crossings = incident.filter(({ crossing }) => crossing).length;
  // Through `nameSpace`, not `spaceLabel`: a subgraph reaches nodes in a space
  // archived since they were written, and the picker fallback rendered those as
  // `space  ab30b069e18a42288bb0749c2169251d`.
  const spaceName = node.space_id === null ? null : nameSpace(node.space_id, spaces, archivedSpaces);
  const spaceNote = spaceName === null ? null : spaceNameNote(spaceName);

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

      {outsideFilter ? (
        <p className="nd-graph__hint">
          This node is outside the space you have narrowed to, which is why it is drawn faintly.
          It is not hidden and it is not out of reach — the filter narrows what you are reading,
          not what exists.
        </p>
      ) : null}

      <dl className="nd-graph__facts">
        <dt>id</dt>
        <dd className="nd-mono">{node.id}</dd>
        <dt>space</dt>
        <dd className="nd-mono" title={spaceNote ?? undefined}>
          {spaceName === null ? "—" : spaceName.label}
          {spaceName?.kind === "archived" ? (
            <span className="nd-badge nd-badge--archived nd-graph__space-mark">
              <span className="nd-badge__dot" aria-hidden="true" />
              archived
            </span>
          ) : null}
        </dd>
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
            {incident.map(({ edge, direction, other, crossing }) => (
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
                  {/* Always a cell, empty when there is no crossing: the row is
                      a fixed grid, and a conditional column would shift every
                      other row's alignment. */}
                  <span className="nd-graph__crossing-mark">
                    {crossing ? (
                      <span
                        title={`Crosses into ${
                          other?.space_id
                            ? nameSpace(other.space_id, spaces, archivedSpaces).label
                            : "another space"
                        }`}
                      >
                        crossing
                      </span>
                    ) : null}
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
        {crossings > 0 ? (
          <p className="nd-graph__hint">
            {crossings === 1 ? "One edge leaves" : `${crossings} edges leave`} this node's space.
            The node at the far end is drawn, not hidden, whatever the space filter says.
          </p>
        ) : null}
        {incident.some(({ edge }) => edge.confidence === null) ? (
          <p className="nd-graph__hint">
            “—” means no stated confidence. A confidence floor would drop those edges.
          </p>
        ) : null}
      </div>
    </section>
  );
}
