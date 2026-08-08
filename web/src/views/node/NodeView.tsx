/**
 * The reading view — route `/node/:nodeId`.
 *
 * Where the graph panel shows a 260-character excerpt, this is the whole
 * note: rendered Markdown with mermaid diagrams drawn in, exactly as the
 * editor's preview renders it (the renderer is the same pure module, so the
 * reading view gets its sanitisation policy with it). Wikilinks inside the
 * content are clickable: a click resolves the title to a node id and
 * navigates here — never to a guess.
 *
 * The right rail is the node's own neighbourhood from `getNode(id, { depth:
 * 1 })` narrowed to the incident edges, in the graph panel's vocabulary:
 * direction arrow, mono edge type, the far endpoint's title, a crossing mark
 * when the edge leaves the node's space, and the edge's state badge. Clicking
 * a row travels to the far node.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, getNode } from "../../api/client";
import {
  EmptyState,
  LinkDialog,
  NodeBadge,
  NodePeekScope,
  Spinner,
  nameSpace,
  spaceNameNote,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useSpaces,
  useToast,
} from "../../components";
import type { NodeOut, SubgraphOut } from "../../api/types";
import { actionForResolution, attachWikilinkClicks, describeFailure } from "../../lib";
import type { FailureDescription } from "../../lib";
import { formatAbsolute, formatTimestampLong } from "../../lib/time";
import { DIAGRAM_PLACEHOLDER_CLASS, renderMarkdown } from "../editor/markdownRender";
import { peekDiagram, renderDiagram } from "../editor/mermaidRender";
import type { DiagramResult } from "../editor/mermaidRender";
import { incidentRows } from "./nodeEdges";
import type { IncidentRow } from "./nodeEdges";
import "./node.css";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; subgraph: SubgraphOut }
  | { status: "failed"; failure: FailureDescription };

const EMPTY_ROWS: IncidentRow[] = [];

export default function NodeView() {
  const { nodeId } = useParams<{ nodeId: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  // Bumped by the retry button; the only thing that re-runs the load for an
  // unchanged node id.
  const [attempt, setAttempt] = useState(0);
  // Bumped when an edge is created from this view, so the rail refetches in
  // the background: the current subgraph stays on screen until the new one
  // lands.
  const [refresh, setRefresh] = useState(0);
  /** Which node a create-link dialog is anchored on, or null while closed. */
  const [linkSource, setLinkSource] = useState<NodeOut | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  /** Bumped per render pass, so a slow diagram cannot land in a newer document. */
  const generation = useRef(0);

  useEffect(() => {
    if (!nodeId) return;
    const controller = new AbortController();
    // A retry and a navigation to another node show the spinner; a refresh of
    // the same node keeps the current subgraph on screen until the new one
    // lands, so creating an edge never flashes the whole view away.
    setLoad((current) =>
      current.status === "ready" && current.subgraph.root === nodeId
        ? current
        : { status: "loading" },
    );
    getNode(nodeId, { depth: 1 }, controller.signal)
      .then((subgraph) => setLoad({ status: "ready", subgraph }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ status: "failed", failure: describeFailure(error, "this node") });
      });
    return () => controller.abort();
  }, [nodeId, attempt, refresh]);

  const root = load.status === "ready" ? load.subgraph.nodes[0] : null;

  // Naming the node's space, and the archived read that lets it name a space
  // the active listing does not carry — the shared vocabulary, verbatim.
  const spaces = useSpaces();
  const spaceId = root?.space_id ?? null;
  const archivedNeeded = unresolvedSpaceIds(spaceId === null ? [] : [spaceId], spaces.spaces);
  const archivedSpaces = useArchivedSpaces(archivedNeeded.length > 0);
  const spaceName = spaceId === null ? null : nameSpace(spaceId, spaces.spaces, archivedSpaces.spaces);

  const rows = useMemo(
    () => (load.status === "ready" ? incidentRows(load.subgraph) : EMPTY_ROWS),
    [load],
  );

  // One pass per document: fill the container with the sanitised render, draw
  // its diagrams, and intercept wikilink clicks. The listener is attached here
  // rather than on mount because the container only exists in the ready state.
  useEffect(() => {
    const container = contentRef.current;
    if (!container || load.status !== "ready") return;
    const pass = ++generation.current;

    const { html, diagrams } = renderMarkdown(load.subgraph.nodes[0]?.content ?? "");
    container.innerHTML = html;

    for (const placeholder of container.querySelectorAll<HTMLElement>(
      `.${DIAGRAM_PLACEHOLDER_CLASS}`,
    )) {
      const index = Number(placeholder.dataset["diagram"]);
      const diagram = diagrams[index];
      if (diagram === undefined) continue;
      const known = peekDiagram(diagram);
      if (known) {
        fill(placeholder, known);
        continue;
      }
      markPending(placeholder);
      void renderDiagram(diagram).then((result) => {
        if (generation.current !== pass || !placeholder.isConnected) return;
        fill(placeholder, result);
      });
    }

    const detach = attachWikilinkClicks(container, (title) => {
      void (async () => {
        try {
          // The node's own space breaks ties: reading a note in `research`
          // and following a title that exists in several spaces means the
          // `research` copy, which is what the header's space pill says too.
          const [resolution] = await api.resolveTitles([title], {
            space: load.subgraph.nodes[0]?.space_id ?? undefined,
          });
          if (resolution === undefined) return;
          const action = actionForResolution(resolution);
          if (action.kind === "navigate") {
            navigate(`/node/${action.nodeId}`);
          } else {
            toast.show("info", action.toastTitle, action.toastDetail);
          }
        } catch (error) {
          toast.showError(error);
        }
      })();
    });
    return () => {
      generation.current += 1;
      detach();
    };
  }, [load, navigate, toast]);

  if (!nodeId) {
    return (
      <div className="nd-view">
        <EmptyState
          title="No node given"
          body="The reading view is per node. Follow a wikilink, a search result, or an edge here."
        />
      </div>
    );
  }

  return (
    <div className="nd-view nd-node">
      <header className="nd-view__header">
        <div className="nd-node__heading">
          <h1>{root?.title ?? "(untitled)"}</h1>
          <p className="nd-row" style={{ ["--nd-row-gap" as string]: "var(--nd-space-3)" }}>
            {root ? <NodeBadge type={root.type} state={root.state} /> : null}
            {spaceName ? (
              <span
                className={spaceName.kind === "archived" ? "nd-badge nd-badge--archived" : "nd-badge"}
                title={spaceNameNote(spaceName) ?? undefined}
              >
                <span className="nd-badge__dot" aria-hidden="true" />
                {spaceName.label}
                {spaceName.kind === "archived" ? " · archived" : ""}
              </span>
            ) : null}
          </p>
          {root ? (
            <p className="nd-meta nd-node__timestamps">
              created{" "}
              <span title={formatTimestampLong(root.created_at)}>
                {formatAbsolute(root.created_at)}
              </span>{" "}
              · updated{" "}
              <span title={formatTimestampLong(root.updated_at)}>
                {formatAbsolute(root.updated_at)}
              </span>
            </p>
          ) : null}
        </div>
        {root ? (
          <div className="nd-node__actions">
            <button
              type="button"
              className="nd-button nd-button--small"
              onClick={() => setLinkSource(root)}
            >
              Link
            </button>
            <Link className="nd-button nd-button--small" to={`/editor/${encodeURIComponent(root.id)}`}>
              Edit
            </Link>
            <Link className="nd-button nd-button--small" to={`/graph/${encodeURIComponent(root.id)}`}>
              Graph
            </Link>
            <Link className="nd-button nd-button--small" to={`/history/${encodeURIComponent(root.id)}`}>
              History
            </Link>
          </div>
        ) : null}
      </header>

      {load.status === "loading" ? (
        <div className="nd-empty">
          <Spinner large label="Loading node" />
        </div>
      ) : null}

      {load.status === "failed" ? (
        <EmptyState
          title={load.failure.title}
          body={load.failure.body}
          action={
            load.failure.kind === "not-found" ? (
              <Link to="/search" className="nd-button">
                Find another node
              </Link>
            ) : (
              <button type="button" className="nd-button" onClick={() => setAttempt((n) => n + 1)}>
                Try again
              </button>
            )
          }
        />
      ) : null}

      {load.status === "ready" ? (
        <div className="nd-node__body">
          <article className="nd-node__content" aria-label="Rendered content">
            <div className="nd-preview" ref={contentRef} />
          </article>
          {/* Wikilinks in the rendered content peek on hover/focus; the space
              preference is the node's own, exactly as the click path uses it. */}
          <NodePeekScope
            containerRef={contentRef}
            space={load.subgraph.nodes[0]?.space_id ?? undefined}
          />

          <aside className="nd-node__rail" aria-label="Edges">
            <h2 className="nd-label">
              {rows.length} edge{rows.length === 1 ? "" : "s"}
            </h2>
            {rows.length === 0 ? (
              <p className="nd-meta">Nothing connects to it.</p>
            ) : (
              <ul className="nd-node__edge-list">
                {rows.map((row) => (
                  <EdgeRow
                    key={row.edge.id}
                    row={row}
                    spaces={spaces.spaces}
                    archivedSpaces={archivedSpaces.spaces}
                  />
                ))}
              </ul>
            )}
            <p className="nd-meta">Click an edge to travel to the far node.</p>
          </aside>
        </div>
      ) : null}

      {linkSource ? (
        <LinkDialog
          source={linkSource}
          onClose={() => setLinkSource(null)}
          onCreated={() => setRefresh((value) => value + 1)}
        />
      ) : null}
    </div>
  );
}

/** One incident edge: direction, type, far endpoint, crossing mark, state. */
function EdgeRow({
  row,
  spaces,
  archivedSpaces,
}: {
  row: IncidentRow;
  spaces: readonly NodeOut[] | null;
  archivedSpaces: readonly NodeOut[];
}) {
  const far = row.far;
  const label = far?.title ?? far?.id ?? "(missing)";
  const crossingTitle =
    row.crossing && far !== null
      ? `Crosses into ${nameSpace(far.space_id ?? "", spaces, archivedSpaces).label}`
      : undefined;

  const cells = (
    <>
      <span className="nd-mono nd-node__edge-dir" aria-hidden="true">
        {row.direction === "out" ? "→" : "←"}
      </span>
      <span className="nd-mono nd-node__edge-type">{row.edge.type}</span>
      <span className="nd-truncate nd-node__edge-far">{label}</span>
      {/* Always a cell, empty when there is no crossing: the row is a fixed
          grid, and a conditional column would shift every other row. */}
      <span className="nd-node__crossing-mark">
        {row.crossing ? <span title={crossingTitle}>crossing</span> : null}
      </span>
      <NodeBadge state={row.edge.state} stateOnly />
    </>
  );

  if (far === null) {
    return <li className="nd-node__edge-row nd-node__edge-row--disabled">{cells}</li>;
  }
  return (
    <li>
      <Link className="nd-node__edge-row" to={`/node/${encodeURIComponent(far.id)}`}>
        {cells}
      </Link>
    </li>
  );
}

/** Put a finished diagram — or an honest failure — into its placeholder. */
function fill(placeholder: HTMLElement, result: DiagramResult): void {
  placeholder.classList.remove("nd-preview__diagram--pending");

  if (result.ok) {
    placeholder.classList.remove("nd-preview__diagram--failed");
    placeholder.innerHTML = result.svg;
    return;
  }

  // Built as nodes, not markup: mermaid's messages quote the offending source,
  // which regularly contains angle brackets.
  placeholder.classList.add("nd-preview__diagram--failed");
  placeholder.replaceChildren();
  placeholder.setAttribute("role", "note");

  const heading = document.createElement("p");
  heading.className = "nd-preview__diagram-title";
  heading.textContent = "Diagram failed to render";

  const detail = document.createElement("pre");
  detail.className = "nd-preview__diagram-message";
  detail.textContent = result.message;

  placeholder.append(heading, detail);
}

/** Show that a diagram is being drawn, reserving roughly its eventual space. */
function markPending(placeholder: HTMLElement): void {
  placeholder.classList.add("nd-preview__diagram--pending");
  placeholder.textContent = "Rendering diagram…";
}
