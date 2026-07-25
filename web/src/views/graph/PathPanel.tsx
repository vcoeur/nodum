/**
 * "How are these two connected?"
 *
 * `find_path` is not a filtered read: it walks **every active edge in the
 * graph**, at any depth, ignoring the depth, type, state, author and
 * confidence filters this view applies to the subgraph. The path it returns
 * can therefore run straight out of the rendered neighbourhood — through nodes
 * the walk never reached and edges the filters excluded.
 *
 * Drawing that as a highlighted line would be a lie: the canvas would show a
 * path with pieces missing and no indication that anything was missing. So the
 * canvas highlights only the hops it actually holds, and this panel prints the
 * *whole* path as text, marking every hop the render does not contain.
 */

import { NodeBadge } from "../../components";
import { Spinner } from "../../components";
import type { NodeOut } from "../../api/types";
import { classifyFailure } from "./errors";
import type { PathState } from "./useGraphData";

interface PathPanelProps {
  /** Start node id, or null. */
  a: string | null;
  /** End node id, or null. */
  b: string | null;
  state: PathState;
  /** Node ids present in the rendered subgraph. */
  presentNodeIds: ReadonlySet<string>;
  /** Edge ids present in the rendered subgraph. */
  presentEdgeIds: ReadonlySet<string>;
  /** Select a node from the path breadcrumb. */
  onSelect: (nodeId: string) => void;
  /** Make a path node the new root. */
  onReroot: (nodeId: string) => void;
  onClear: () => void;
}

/** Label for a node in the breadcrumb. */
function label(node: NodeOut): string {
  return node.title?.trim() || node.id.slice(0, 8);
}

/**
 * Render the path panel.
 *
 * @param props See {@link PathPanelProps}.
 */
export function PathPanel({
  a,
  b,
  state,
  presentNodeIds,
  presentEdgeIds,
  onSelect,
  onReroot,
  onClear,
}: PathPanelProps) {
  const path = state.data;
  const missingNodes = path?.found
    ? path.nodes.filter((node) => !presentNodeIds.has(node.id)).length
    : 0;
  const missingEdges = path?.found
    ? path.edges.filter((edge) => !presentEdgeIds.has(edge.id)).length
    : 0;

  return (
    <section className="nd-graph__panel-section" aria-label="Path">
      <header className="nd-graph__panel-header">
        <h2 className="nd-graph__panel-title">Path</h2>
        <button type="button" className="nd-button nd-button--ghost nd-button--small" onClick={onClear}>
          Clear
        </button>
      </header>

      <dl className="nd-graph__facts">
        <dt>from</dt>
        <dd className="nd-mono">{a ?? "— pick a node"}</dd>
        <dt>to</dt>
        <dd className="nd-mono">{b ?? "— pick a node"}</dd>
      </dl>

      {!a || !b ? (
        <p className="nd-graph__hint">
          Click a node, then use “Path from here” and “Path to here”.
        </p>
      ) : null}

      {state.status === "loading" ? (
        <div className="nd-graph__picker-loading">
          <Spinner label="Finding a path" />
        </div>
      ) : null}

      {state.status === "error"
        ? (() => {
            const failure = classifyFailure(state.error);
            return (
              <p className="nd-graph__warn">
                {failure.title}. {failure.detail}
              </p>
            );
          })()
        : null}

      {path && !path.found ? (
        <p className="nd-graph__hint">
          No path over active edges connects these two. `find_path` follows active edges only, so a
          connection made of proposed edges will not show up here.
        </p>
      ) : null}

      {path?.found ? (
        <>
          <p className="nd-graph__meta">
            {path.hops} hop{path.hops === 1 ? "" : "s"}
            {missingNodes + missingEdges > 0
              ? ` · ${missingNodes + missingEdges} step${
                  missingNodes + missingEdges === 1 ? "" : "s"
                } outside this view`
              : " · fully inside this view"}
          </p>

          {missingNodes + missingEdges > 0 ? (
            <p className="nd-graph__warn">
              This path leaves the loaded subgraph. Only the hops present here are highlighted on
              the canvas; the rest are listed below but cannot be drawn. Re-root on one of them, or
              raise the depth, to bring them in.
            </p>
          ) : null}

          <ol className="nd-graph__path">
            {path.nodes.map((node, index) => {
              const edge = index > 0 ? path.edges[index - 1] : undefined;
              const nodeHere = presentNodeIds.has(node.id);
              const edgeHere = edge ? presentEdgeIds.has(edge.id) : true;
              return (
                <li key={node.id} className="nd-graph__path-step">
                  {edge ? (
                    <span
                      className={
                        edgeHere
                          ? "nd-graph__path-edge"
                          : "nd-graph__path-edge nd-graph__path-edge--absent"
                      }
                    >
                      <span className="nd-mono">↓ {edge.type}</span>
                      {edgeHere ? null : <span className="nd-meta">not in this view</span>}
                    </span>
                  ) : null}
                  <span
                    className={
                      nodeHere
                        ? "nd-graph__path-node"
                        : "nd-graph__path-node nd-graph__path-node--absent"
                    }
                  >
                    <button
                      type="button"
                      className="nd-graph__link-button nd-truncate"
                      onClick={() => (nodeHere ? onSelect(node.id) : onReroot(node.id))}
                      title={nodeHere ? "Select in the graph" : "Not rendered — re-root here"}
                    >
                      {label(node)}
                    </button>
                    <NodeBadge type={node.type} state={node.state} />
                    {nodeHere ? null : <span className="nd-meta">not in this view</span>}
                  </span>
                </li>
              );
            })}
          </ol>

          <p className="nd-graph__hint">
            Path finding walks every active edge and ignores the filters above — including the
            depth and node limit.
          </p>
        </>
      ) : null}
    </section>
  );
}
