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
 * The space filter (design decision D5) is a fourth commitment of the same
 * kind, and the only control here that is not part of the request: narrowing to
 * a space **dims** what is outside it and removes nothing, so a cross-space
 * edge still runs to a node you can see and click. Hiding the far endpoint
 * would make the picture assert that the connection ended at the boundary,
 * which is exactly the sort of quiet lie the three rules above exist to
 * prevent. It follows from D1: the filter narrows a *reading*, it does not
 * claim the rest of the file is gone, and it is a convenience for the human
 * rather than a boundary — the human is unfiltered by design.
 *
 * Filter state lives in the URL, so a view is linkable and survives a reload,
 * and the query parameters are the API's own — the address bar is the request.
 *
 * Routes: `/graph` (pick a root) and `/graph/:rootId`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArchiveEdgeDialog,
  EmptyState,
  LinkDialog,
  Spinner,
  nameSpace,
  resolveSpaceValue,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useEdgeArchive,
  useSpaces,
} from "../../components";
import type { EdgeArchiveSubject } from "../../components";
import type { NodeOut } from "../../api/types";
import { focusProgrammatically } from "../../lib/programmaticFocus";
import { GraphCanvas } from "./GraphCanvas";
import type { GraphCanvasHandle } from "./GraphCanvas";
import { GraphToolbar } from "./GraphToolbar";
import { NodeDetailPanel } from "./NodeDetailPanel";
import { PathPanel } from "./PathPanel";
import { RootPicker } from "./RootPicker";
import { TruncationNotice } from "./TruncationNotice";
import { classifyFailure } from "./errors";
import { applyFilters, parseFilters, spaceDimming } from "./filters";
import type { GraphFilters } from "./filters";
import { distinctValues, incidentEdges, nodeListItems, toElements } from "./graphElements";
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
  /** Which node a create-link dialog is anchored on, or null while closed. */
  const [linkSource, setLinkSource] = useState<NodeOut | null>(null);
  const [archivingEdge, setArchivingEdge] = useState<EdgeArchiveSubject | null>(null);
  const rootButtonRef = useRef<HTMLButtonElement | null>(null);
  const focusAfterEdgeArchive = useRef(false);
  const refetch = useCallback(() => setReloadToken((token) => token + 1), []);
  const edgeArchive = useEdgeArchive(() => {
    if (archivingEdge !== null) focusAfterEdgeArchive.current = true;
    refetch();
  });

  const subgraph = useSubgraph(rootId, filters, reloadToken);
  const path = usePath(pathA, pathB, reloadToken);
  const catalog = useTypeCatalog();
  const spaceList = useSpaces();

  const data = subgraph.data;

  // A successful relationship archive removes its incident-row opener. The
  // root picker persists across the refresh, so it is the host's real focus
  // destination after Modal declines to restore a detached opener.
  useEffect(() => {
    if (!focusAfterEdgeArchive.current || subgraph.status !== "ready") return;
    focusAfterEdgeArchive.current = false;
    if (rootButtonRef.current !== null) focusProgrammatically(rootButtonRef.current);
  }, [subgraph.status, subgraph.data]);

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
    () =>
      data
        ? toElements(data)
        : { elements: [], danglingEdges: 0, crossingEdges: 0, signature: "" },
    [data],
  );

  /* --- The space filter (D5) ---------------------------------------------- */

  // A space reference is an id or a name; `NodeOut.space_id` is only ever an
  // id, so the URL's value is resolved through the space list before anything
  // is compared to it.
  const knownSpaces = spaceList.spaces ?? [];
  const resolvedSpace = resolveSpaceValue(knownSpaces, filters.space);
  // Only dim once the reference resolves to a space we actually know. Dimming
  // against an unresolvable name would recede *every* node and read as "this
  // space is empty" — a claim the view is in no position to make.
  const spaceResolved =
    filters.space === "" || knownSpaces.some((space) => space.id === resolvedSpace);
  const dimming = useMemo(
    () =>
      spaceDimming(data?.nodes ?? [], data?.edges ?? [], spaceResolved ? resolvedSpace : ""),
    [data, resolvedSpace, spaceResolved],
  );
  // The inspector names the space of the node it is showing, and of the far end
  // of every crossing — including a space archived since those nodes were
  // written, which the shared active-only list cannot name. The **filter's own**
  // reference is in the same set: an archived one is exactly what puts the
  // "not in effect" banner on screen, and it read `18ee0caa…` there and in the
  // picker beside it. One lazy listing covers all of them. It is deliberately
  // **not** also gated on a node being selected: `needed` going false on every
  // deselect and true again on the next select would re-issue the request on
  // each click.
  const unresolvedSpaces = useMemo(
    () =>
      unresolvedSpaceIds(
        [filters.space, ...(data?.nodes ?? []).map((node) => node.space_id ?? "")],
        spaceList.spaces,
      ),
    [filters.space, data, spaceList.spaces],
  );
  const archivedSpaces = useArchivedSpaces(unresolvedSpaces.length > 0);
  // Through `nameSpace`, not `spaceLabel`: the chip row, the banner and the
  // legend all render this, and a filter the list cannot name is the case they
  // exist for.
  const spaceName = nameSpace(filters.space, spaceList.spaces, archivedSpaces.spaces);

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

  // `setSelectedId` is itself a callback over `setSearchParams`, so a stable
  // `selectFromList` would pin the mount-time query string: a selection from
  // the path or detail panel would silently revert every filter change since
  // mount, while a canvas click (which re-reads the callback every render via
  // onSelectRef) would not. The callback changes identity only when the
  // underlying setter does — the canvas already tolerates that.
  const selectFromList = useCallback(
    (nodeId: string) => {
      setSelectedId(nodeId);
      canvasRef.current?.center(nodeId);
    },
    [setSelectedId],
  );

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
        rootButtonRef={rootButtonRef}
        onPickRoot={pickRoot}
        edgeTypeOptions={edgeTypeOptions}
        nodeTypeOptions={nodeTypeOptions}
        actorOptions={actorOptions}
        spaces={spaceList.spaces}
        archivedSpaces={archivedSpaces.spaces}
        spacesFailed={spaceList.failed}
        spaceName={spaceName.label}
        spaceInEffect={spaceResolved}
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

        {filters.space !== "" && !spaceResolved ? (
          <div className="nd-graph__banner nd-graph__banner--warn" role="status">
            <div className="nd-graph__banner-text">
              <strong>The space filter is not in effect.</strong> This view is narrowed to{" "}
              <span className="nd-mono">{spaceName.label}</span>
              {spaceName.kind === "archived" ? (
                <span className="nd-badge nd-badge--archived nd-graph__space-mark">
                  <span className="nd-badge__dot" aria-hidden="true" />
                  archived
                </span>
              ) : null}
              , but the space list
              {spaceList.failed
                ? " could not be loaded, so that reference cannot be resolved"
                : spaceName.kind === "archived"
                  ? " does not carry it, because archiving takes a space out of every picker"
                  : " does not carry that space — it may have been archived or renamed"}
              . Nothing is dimmed; you are looking at the whole neighbourhood.
            </div>
            <div className="nd-graph__banner-actions">
              <button
                type="button"
                className="nd-button nd-button--small"
                onClick={() => updateFilters({ ...filters, space: "" })}
              >
                Clear the space filter
              </button>
            </div>
          </div>
        ) : null}

        {dimming.active && dimming.inside === 0 ? (
          <div className="nd-graph__banner" role="status">
            <div className="nd-graph__banner-text">
              <strong>Nothing in {spaceName.label} is in this neighbourhood.</strong> Every node here
              belongs to another space, so all of them are dimmed. They are still drawn and still
              clickable — the space filter narrows what you are reading, it does not remove the
              rest of the file.
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
              dimmedNodeIds={dimming.nodes}
              dimmedEdgeIds={dimming.edges}
              onSelect={setSelectedId}
            />
          ) : null}

          {/* A focusable twin for every canvas node (review 08-frontend,
              MAJOR 3): the canvas paints into a <canvas> with no text
              alternative, so without this a keyboard or screen-reader user
              could not reach any node data. Hidden with the .nd-sr-only
              recipe; focusing the list reveals it over the canvas. Tab moves
              between nodes, Enter selects — the same state a canvas click
              sets, via selectFromList (safe since M33: it re-reads the
              current query string instead of the mount-time one). */}
          {data ? (
            <ul className="nd-sr-only nd-graph__node-list" aria-label="Graph nodes">
              {nodeListItems(data.nodes).map(({ id, label }) => (
                <li key={id}>
                  <button
                    type="button"
                    className="nd-graph__node-button"
                    aria-label={label}
                    onClick={() => selectFromList(id)}
                  >
                    {label}
                  </button>
                </li>
              ))}
            </ul>
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
            {graph.crossingEdges > 0 ? (
              <>
                <span className="nd-graph__legend-sep" aria-hidden="true" />
                <span className="nd-graph__legend-item nd-graph__legend-item--crossing">
                  {graph.crossingEdges} crossing
                  {graph.crossingEdges === 1 ? "" : "s"}
                </span>
                <span className="nd-meta">outlined edge = spans two spaces</span>
              </>
            ) : null}
            {dimming.active ? (
              <>
                <span className="nd-graph__legend-sep" aria-hidden="true" />
                <span className="nd-meta">
                  dimmed = outside {spaceName.label}, still there and still clickable
                </span>
              </>
            ) : null}
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
              {/* Said as "of", never as a replacement for the node count: the
                  space filter changed nothing about how much was fetched. */}
              {dimming.active ? ` · ${dimming.inside} in ${spaceName.label}` : ""}
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
              spaces={spaceList.spaces}
              archivedSpaces={archivedSpaces.spaces}
              filteredSpace={spaceResolved ? resolvedSpace : ""}
              incident={incidentEdges(data, selectedNode.id)}
              rerootTo={rerootHref(selectedNode.id)}
              onSelect={selectFromList}
              onSetPathEnd={setPathEnd}
              pathRole={pathA === selectedNode.id ? "a" : pathB === selectedNode.id ? "b" : null}
              onCreateEdge={() => setLinkSource(selectedNode)}
              onArchiveEdge={setArchivingEdge}
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

      {linkSource ? (
        <LinkDialog
          source={linkSource}
          onClose={() => setLinkSource(null)}
          onCreated={refetch}
        />
      ) : null}
      {archivingEdge ? (
        <ArchiveEdgeDialog
          subject={archivingEdge}
          onConfirm={() => edgeArchive.archive(archivingEdge)}
          onClose={() => setArchivingEdge(null)}
        />
      ) : null}
    </div>
  );
}

export { GraphView };
