/**
 * The graph view — a bounded, filtered subgraph rendered with Cytoscape.
 *
 * The rendering is the easy half. The half worth reading is the boundary:
 * `subgraph` is the one nodum read that is capped *server-side, during the
 * walk*, so this view can never provoke an unbounded result — and in exchange
 * it inherits the duty of never pretending a bounded result is a complete one.
 * Three commitments follow, and each is implemented rather than documented:
 *
 * 1. **A `limit` is always sent** (`filters.ts` → `toSubgraphParams`). The
 *    server's default is never leaned on; the cap is the caller's decision.
 * 2. **`truncated` is a banner, not a footnote** ({@link TruncationNotice}).
 *    When the cap bit, the user is looking at a slice and is told so, with the
 *    three ways out one click away.
 * 3. **The confidence floor is opt-in and self-describing**
 *    ({@link ConfidenceFilter}). `min_confidence` drops every edge whose
 *    confidence is NULL, and hand-made edges rarely have one, so a default-on
 *    slider would quietly hide most of a personal graph.
 *
 * Filter state lives in the URL, so a view is linkable and survives a reload,
 * and the query parameters are the API's own — the address bar is the request.
 *
 * Routes: `/graph` (pick a root) and `/graph/:rootId`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { EmptyState, Spinner } from "../../components";
import type { NodeOut } from "../../api/types";
import { GraphCanvas } from "./GraphCanvas";
import type { GraphCanvasHandle } from "./GraphCanvas";
import { GraphToolbar } from "./GraphToolbar";
import { NodeDetailPanel } from "./NodeDetailPanel";
import { PathPanel } from "./PathPanel";
import { RootPicker } from "./RootPicker";
import { TruncationNotice } from "./TruncationNotice";
import { classifyFailure } from "./errors";
import { applyFilters, parseFilters } from "./filters";
import type { GraphFilters } from "./filters";
import { distinctValues, incidentEdges, toElements } from "./graphElements";
import { shapeForType } from "./graphStyle";
import { useSubgraph, usePath } from "./useGraphData";
import { offeredTypes, useTypeCatalog } from "./useTypeCatalog";
import "./graph.css";

/** Actors always worth offering as a `created_by` completion. */
const BASE_ACTORS = ["human"];

export default function GraphView() {
  const { rootId } = useParams<{ rootId: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const filters = useMemo(() => parseFilters(searchParams), [searchParams]);
  const pathA = searchParams.get("path_a");
  const pathB = searchParams.get("path_b");
  // Selection is URL state for the same reason the filters are: "look at this
  // node in this neighbourhood" is a thing worth linking to, and a reload
  // should not drop the panel you were reading.
  const selectedId = searchParams.get("selected");

  const [reloadToken, setReloadToken] = useState(0);
  const canvasRef = useRef<GraphCanvasHandle | null>(null);

  const subgraph = useSubgraph(rootId, filters, reloadToken);
  const path = usePath(pathA, pathB);
  const catalog = useTypeCatalog();

  const data = subgraph.data;

  /* --- URL writers ------------------------------------------------------- */

  const updateFilters = useCallback(
    (next: GraphFilters) => {
      // Replace rather than push: dragging a slider should not fill the back
      // stack with intermediate graphs.
      setSearchParams((current) => applyFilters(current, next), { replace: true });
    },
    [setSearchParams],
  );

  const setSelectedId = useCallback(
    (nodeId: string | null) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (nodeId) next.set("selected", nodeId);
          else next.delete("selected");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const setPathEnd = useCallback(
    (end: "a" | "b", nodeId: string) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set(`path_${end}`, nodeId);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const clearPath = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("path_a");
        next.delete("path_b");
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  const rerootHref = useCallback(
    (nodeId: string) => {
      // Filters and the path selection survive a re-root; the panel selection
      // does not, because a new root is a new question.
      const carried = new URLSearchParams(searchParams);
      carried.delete("selected");
      const query = carried.toString();
      return `/graph/${encodeURIComponent(nodeId)}${query ? `?${query}` : ""}`;
    },
    [searchParams],
  );

  const pickRoot = useCallback(
    (nodeId: string) => navigate(rerootHref(nodeId)),
    [navigate, rerootHref],
  );

  /* --- Derived data ------------------------------------------------------ */

  const graph = useMemo(
    () => (data ? toElements(data) : { elements: [], danglingEdges: 0, signature: "" }),
    [data],
  );

  const nodesById = useMemo(() => {
    const index = new Map<string, NodeOut>();
    for (const node of data?.nodes ?? []) index.set(node.id, node);
    return index;
  }, [data]);

  const presentNodeIds = useMemo(
    () => new Set((data?.nodes ?? []).map((node) => node.id)),
    [data],
  );
  const presentEdgeIds = useMemo(
    () => new Set((data?.edges ?? []).map((edge) => edge.id)),
    [data],
  );

  const rootNode = data ? (nodesById.get(data.root) ?? null) : null;
  const unratedEdges = (data?.edges ?? []).filter((edge) => edge.confidence === null).length;

  const edgeTypeOptions = offeredTypes(
    catalog.edgeTypes,
    distinctValues((data?.edges ?? []).map((edge) => edge.type)),
    filters.edgeTypes,
  );
  const nodeTypeOptions = offeredTypes(
    catalog.nodeTypes,
    distinctValues((data?.nodes ?? []).map((node) => node.type)),
    filters.nodeTypes,
  );
  const actorOptions = distinctValues([
    ...BASE_ACTORS,
    ...(data?.nodes ?? []).map((node) => node.created_by),
    ...(data?.edges ?? []).map((edge) => edge.created_by),
  ]);

  const pathFound = path.data?.found ? path.data : null;
  const pathNodeIds = (pathFound?.nodes ?? [])
    .map((node) => node.id)
    .filter((id) => presentNodeIds.has(id));
  const pathEdgeIds = (pathFound?.edges ?? [])
    .map((edge) => edge.id)
    .filter((id) => presentEdgeIds.has(id));
  const pathEndIds = [pathA, pathB].filter(
    (id): id is string => id !== null && presentNodeIds.has(id),
  );

  const selectedNode = selectedId ? (nodesById.get(selectedId) ?? null) : null;
  const shapesInUse = useMemo(
    () => distinctValues((data?.nodes ?? []).map((node) => node.type)),
    [data],
  );

  /* --- Selection housekeeping -------------------------------------------- */

  // A filter change (or an arrival on a link naming a node this walk does not
  // reach) can leave the selection pointing outside the render.
  useEffect(() => {
    if (selectedId && data && !presentNodeIds.has(selectedId)) setSelectedId(null);
  }, [selectedId, data, presentNodeIds, setSelectedId]);

  const selectFromList = useCallback((nodeId: string) => {
    setSelectedId(nodeId);
    canvasRef.current?.center(nodeId);
  }, []);

  /* --- No root yet -------------------------------------------------------- */

  if (!rootId) {
    return (
      <div className="nd-view nd-graph nd-graph--picking">
        <header className="nd-view__header">
          <h1>Graph</h1>
        </header>
        <div className="nd-card nd-graph__picker-card">
          <div>
            <h2>Start somewhere</h2>
            <p className="nd-meta">
              The graph is read as a bounded neighbourhood around one node — pick that node and the
              walk goes out from there, capped at a node limit the server enforces while it walks.
            </p>
          </div>
          <RootPicker onPick={pickRoot} />
        </div>
      </div>
    );
  }

  /* --- Failure with nothing to show --------------------------------------- */

  const failure = subgraph.status === "error" ? classifyFailure(subgraph.error, rootId) : null;

  if (failure && !data) {
    return (
      <div className="nd-view nd-graph nd-graph--picking">
        <header className="nd-view__header">
          <h1>Graph</h1>
          <span className="nd-mono">{rootId}</span>
        </header>
        {failure.kind === "root-missing" ? (
          <div className="nd-card nd-graph__picker-card">
            <div>
              <h2>{failure.title}</h2>
              <p className="nd-meta">{failure.detail}</p>
            </div>
            <RootPicker onPick={pickRoot} />
          </div>
        ) : (
          <EmptyState
            title={failure.title}
            body={failure.detail}
            action={
              <div className="nd-row">
                <button
                  type="button"
                  className="nd-button nd-button--primary"
                  onClick={() => setReloadToken((token) => token + 1)}
                >
                  Retry
                </button>
                <Link className="nd-button" to="/graph">
                  Pick another root
                </Link>
              </div>
            }
          />
        )}
      </div>
    );
  }

  /* --- The graph ---------------------------------------------------------- */

  const emptyNeighbourhood = data !== null && data.nodes.length <= 1 && data.edges.length === 0;
  const rootExempt =
    filters.nodeTypes.length > 0 &&
    rootNode !== null &&
    !filters.nodeTypes.includes(rootNode.type);

  return (
    <div className="nd-view nd-view--wide nd-graph">
      <GraphToolbar
        filters={filters}
        onFiltersChange={updateFilters}
        rootNode={rootNode}
        rootId={rootId}
        onPickRoot={pickRoot}
        edgeTypeOptions={edgeTypeOptions}
        nodeTypeOptions={nodeTypeOptions}
        actorOptions={actorOptions}
        unratedEdges={unratedEdges}
        totalEdges={data?.edges.length ?? 0}
        loading={subgraph.status === "loading"}
        onReload={() => setReloadToken((token) => token + 1)}
        onFit={() => canvasRef.current?.fit()}
        onRelayout={() => canvasRef.current?.relayout()}
      />

      <div className="nd-graph__banners">
        {failure ? (
          <div className="nd-graph__banner nd-graph__banner--error" role="alert">
            <div className="nd-graph__banner-text">
              <strong>{failure.title}.</strong> {failure.detail} The graph below is the last result
              that loaded.
            </div>
            <div className="nd-graph__banner-actions">
              <button
                type="button"
                className="nd-button nd-button--small"
                onClick={() => setReloadToken((token) => token + 1)}
              >
                Retry
              </button>
            </div>
          </div>
        ) : null}

        {data?.truncated ? (
          <TruncationNotice
            nodeCount={data.nodes.length}
            limit={filters.limit}
            depth={filters.depth}
            onRaiseLimit={(limit) => updateFilters({ ...filters, limit })}
            onReduceDepth={(depth) => updateFilters({ ...filters, depth })}
          />
        ) : null}

        {graph.danglingEdges > 0 ? (
          <div className="nd-graph__banner nd-graph__banner--warn" role="status">
            <div className="nd-graph__banner-text">
              {graph.danglingEdges} edge{graph.danglingEdges === 1 ? "" : "s"} named an endpoint the
              response did not carry and {graph.danglingEdges === 1 ? "was" : "were"} dropped. That
              should not happen — the subgraph read is supposed to drop an edge with its node.
            </div>
          </div>
        ) : null}

        {emptyNeighbourhood && !failure ? (
          <div className="nd-graph__banner" role="status">
            <div className="nd-graph__banner-text">
              <strong>The root stands alone here.</strong> Nothing else passed the current filters
              {filters.depth === 0 ? " at depth 0" : ""}.
            </div>
            <div className="nd-graph__banner-actions">
              {filters.depth < 4 ? (
                <button
                  type="button"
                  className="nd-button nd-button--small"
                  onClick={() => updateFilters({ ...filters, depth: filters.depth + 1 })}
                >
                  Depth {filters.depth + 1}
                </button>
              ) : null}
              {!filters.edgeStates.includes("proposed") ? (
                <button
                  type="button"
                  className="nd-button nd-button--small"
                  onClick={() =>
                    updateFilters({ ...filters, edgeStates: ["active", "proposed"] })
                  }
                >
                  Include proposed edges
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>

      <div className="nd-graph__body">
        <div className="nd-graph__stage">
          {data ? (
            <GraphCanvas
              ref={canvasRef}
              elements={graph.elements}
              signature={graph.signature}
              selectedId={selectedId}
              pathNodeIds={pathNodeIds}
              pathEdgeIds={pathEdgeIds}
              pathEndIds={pathEndIds}
              pathActive={pathFound !== null && pathNodeIds.length > 0}
              onSelect={setSelectedId}
            />
          ) : null}

          {!data && subgraph.status === "loading" ? (
            <div className="nd-graph__stage-overlay">
              <Spinner large label="Loading the subgraph" />
            </div>
          ) : null}

          {subgraph.stale ? (
            <div className="nd-graph__stage-badge">
              <Spinner label="Refreshing" />
              <span className="nd-meta">refreshing</span>
            </div>
          ) : null}

          <div className="nd-graph__legend">
            <span className="nd-label">State</span>
            <span className="nd-graph__legend-item nd-graph__legend-item--proposed">proposed</span>
            <span className="nd-graph__legend-item nd-graph__legend-item--active">active</span>
            <span className="nd-graph__legend-item nd-graph__legend-item--archived">archived</span>
            <span className="nd-graph__legend-sep" aria-hidden="true" />
            <span className="nd-meta">dashed edge = proposed</span>
            {shapesInUse.length > 0 ? (
              <>
                <span className="nd-graph__legend-sep" aria-hidden="true" />
                <span className="nd-label">Shape</span>
                {shapesInUse.map((type) => (
                  <span key={type} className="nd-graph__legend-item">
                    <span className="nd-mono">{type}</span>
                    <span className="nd-meta">{shapeForType(type)}</span>
                  </span>
                ))}
              </>
            ) : null}
          </div>

          <div className="nd-graph__status">
            <span className="nd-mono">
              {data?.nodes.length ?? 0} nodes · {data?.edges.length ?? 0} edges
            </span>
            <span className="nd-meta">
              depth {filters.depth} · cap {filters.limit}
              {data?.truncated ? " · truncated" : ""}
            </span>
          </div>
        </div>

        <aside className="nd-graph__panel">
          {pathA || pathB ? (
            <PathPanel
              a={pathA}
              b={pathB}
              state={path}
              presentNodeIds={presentNodeIds}
              presentEdgeIds={presentEdgeIds}
              onSelect={selectFromList}
              onReroot={pickRoot}
              onClear={clearPath}
            />
          ) : null}

          {selectedNode && data ? (
            <NodeDetailPanel
              node={selectedNode}
              isRoot={selectedNode.id === data.root}
              rootExemptFromTypeFilter={rootExempt && selectedNode.id === data.root}
              incident={incidentEdges(data, selectedNode.id)}
              rerootTo={rerootHref(selectedNode.id)}
              onSelect={selectFromList}
              onSetPathEnd={setPathEnd}
              pathRole={pathA === selectedNode.id ? "a" : pathB === selectedNode.id ? "b" : null}
              onClose={() => setSelectedId(null)}
            />
          ) : (
            <section className="nd-graph__panel-section">
              <h2 className="nd-graph__panel-title">Nothing selected</h2>
              <p className="nd-graph__hint">
                Click a node to see what it is, open it in the editor, re-root the walk on it, or
                use it as one end of a path.
              </p>
              {rootExempt ? (
                <p className="nd-graph__hint">
                  The root's type is outside the node-type filter. The root is exempt from that
                  filter — it is what you asked for — so it is drawn regardless.
                </p>
              ) : null}
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}

export { GraphView };
