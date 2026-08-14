import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import type { NodeFilters, NodeOut, NodeState } from "../../api/types";
import {
  EmptyState,
  NodeBadge,
  SpaceFilter,
  Spinner,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useNodeTypes,
  useSpaces,
} from "../../components";
import { formatTimestamp } from "../../lib";
import {
  NODE_BROWSE_LIMIT,
  describeNodeBrowseFailure,
  nodeTypeOptions,
  readNodeBrowseState,
  sortNodes,
  toNodeBrowseParams,
  type NodeBrowseState,
  type NodeSort,
} from "./nodes";
import "./nodes.css";

/** Browse nodes through the existing bounded listing endpoint. */
export default function NodesView() {
  const [params, setParams] = useSearchParams();
  const browse = readNodeBrowseState(params);
  const [nodes, setNodes] = useState<NodeOut[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const nodeTypes = useNodeTypes();
  const spaceList = useSpaces();
  const unresolved = unresolvedSpaceIds(browse.space ? [browse.space] : [], spaceList.spaces);
  const archived = useArchivedSpaces(unresolved.length > 0);

  const patch = (change: Partial<NodeBrowseState>) => {
    setParams(toNodeBrowseParams({ ...browse, ...change }));
  };

  useEffect(() => {
    const controller = new AbortController();
    setNodes(null);
    setError(null);
    const filters: NodeFilters = { limit: NODE_BROWSE_LIMIT };
    if (browse.type) filters.type = browse.type;
    if (browse.state) filters.state = browse.state;
    if (browse.space) filters.space = browse.space;
    void api
      .listNodes(filters, controller.signal)
      .then((listed) => {
        setNodes(listed);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError(caught);
      });
    return () => controller.abort();
  }, [browse.type, browse.state, browse.space]);

  const ordered = useMemo(() => sortNodes(nodes ?? [], browse.sort), [nodes, browse.sort]);
  const typeOptions = nodeTypeOptions(nodeTypes.types ?? [], browse.type);
  const failure = error
    ? describeNodeBrowseFailure(error, browse.type, spaceList.spaces, archived.spaces)
    : null;

  return (
    <div className="nd-view nd-nodes">
      <header className="nd-nodes__header">
        <div>
          <h1>Nodes</h1>
          <p className="nd-meta">
            Browsing up to {NODE_BROWSE_LIMIT} oldest matching nodes. Sorting rearranges only this
            returned set, not the whole graph.
          </p>
        </div>
        <span className="nd-meta" aria-live="polite">
          {error ? "Could not load nodes" : nodes === null ? "Loading…" : `${nodes.length} returned`}
        </span>
      </header>

      <div className="nd-card nd-nodes__filters">
        <label className="nd-field">
          <span className="nd-label">Node type</span>
          <select
            name="node-type"
            className="nd-select"
            value={browse.type}
            disabled={nodeTypes.types === null}
            onChange={(event) => patch({ type: event.target.value })}
          >
            <option value="">{nodeTypes.failed ? "Types unavailable" : "Any type"}</option>
            {typeOptions.map((type) => (
              <option key={type.value} value={type.value}>
                {type.unlisted ? `${type.label} (unavailable)` : type.label}
              </option>
            ))}
          </select>
        </label>
        <SpaceFilter
          value={browse.space}
          onChange={(space) => patch({ space })}
          spaces={spaceList.spaces}
          archivedSpaces={archived.spaces}
          failed={spaceList.failed}
          name="node-space"
        />
        <label className="nd-field">
          <span className="nd-label">State</span>
          <select name="node-state" className="nd-select" value={browse.state} onChange={(event) => patch({ state: event.target.value as "" | NodeState })}>
            <option value="">Any state</option>
            <option value="active">Active</option>
            <option value="proposed">Proposed</option>
            <option value="archived">Archived</option>
          </select>
        </label>
        <label className="nd-field">
          <span className="nd-label">Sort</span>
          <select name="node-sort" className="nd-select" value={browse.sort} onChange={(event) => patch({ sort: event.target.value as NodeSort })}>
            <option value="created-asc">Created, oldest first</option>
            <option value="created-desc">Created, newest in set first</option>
            <option value="title-asc">Title A–Z in set</option>
            <option value="title-desc">Title Z–A in set</option>
          </select>
        </label>
      </div>

      {failure ? (
        <EmptyState
          title={failure.title}
          body={failure.detail}
          action={
            failure.clear ? (
              <button
                type="button"
                className="nd-button nd-button--primary"
                onClick={() =>
                  failure.clear === "space" ? patch({ space: "" }) : patch({ type: "" })
                }
              >
                Clear {failure.clear} filter
              </button>
            ) : undefined
          }
        />
      ) : null}
      {nodes === null && !error ? <Spinner large label="Loading nodes" /> : null}
      {nodes?.length === 0 ? <EmptyState title="No matching nodes" body="Change or clear a filter to broaden this bounded listing." /> : null}
      {ordered.length > 0 ? (
        <div className="nd-nodes__list">
          {ordered.map((node) => (
            <article className="nd-card nd-nodes-row" key={node.id}>
              <div>
                <Link className="nd-nodes-row__title" to={`/node/${node.id}`}>{node.title ?? "Untitled node"}</Link>
                <p className="nd-meta">created {formatTimestamp(node.created_at)}</p>
              </div>
              <NodeBadge type={node.type} state={node.state} />
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}
