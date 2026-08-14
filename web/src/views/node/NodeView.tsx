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
 * a row travels to the far node. Under it, the **backlinks** section narrows
 * the same read the other way — who wikilinked here, and the sentence they
 * wrote it in.
 *
 * Every action this view owns is reachable three ways: the header's buttons,
 * the header's `⋯` menu, and a right-click on the heading. That is the shared
 * `ContextMenu` primitive, and the edge rows carry the same menu for their far
 * node plus an explicitly separate action for the relationship itself.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, getNode } from "../../api/client";
import {
  ArchiveNodeDialog,
  ArchiveEdgeDialog,
  ContextMenu,
  EmptyState,
  LinkDialog,
  MenuButton,
  NodeBadge,
  NodePeekScope,
  Spinner,
  archiveRefusal,
  edgeArchiveRefusal,
  nameSpace,
  spaceNameNote,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useContextMenu,
  useEdgeArchive,
  useNodeArchive,
  useSpaces,
  useToast,
} from "../../components";
import type { MenuAction } from "../../components";
import type { EdgeArchiveSubject } from "../../components";
import type { NodeOut, SubgraphOut } from "../../api/types";
import { actionForResolution, attachWikilinkClicks, describeFailure } from "../../lib";
import type { FailureDescription } from "../../lib";
import { focusProgrammatically } from "../../lib/programmaticFocus";
import { formatAbsolute, formatTimestampLong } from "../../lib/time";
import { DIAGRAM_PLACEHOLDER_CLASS, renderMarkdown } from "../editor/markdownRender";
import { peekDiagram, renderDiagram } from "../editor/mermaidRender";
import type { DiagramResult } from "../editor/mermaidRender";
import { backlinks, incidentRows, edgeCountLabel } from "./nodeEdges";
import type { Backlink, IncidentRow } from "./nodeEdges";
import "./node.css";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; subgraph: SubgraphOut }
  | { status: "failed"; failure: FailureDescription };

const EMPTY_ROWS: IncidentRow[] = [];
const EMPTY_BACKLINKS: Backlink[] = [];

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
  // lands. An archive and its undo bump it too — the badge in the header is
  // the only thing that says which state the node is now in.
  const [refresh, setRefresh] = useState(0);
  /** Which node a create-link dialog is anchored on, or null while closed. */
  const [linkSource, setLinkSource] = useState<NodeOut | null>(null);
  /** Which node an archive confirm is up for, or null while closed. */
  const [archiving, setArchiving] = useState<NodeOut | null>(null);
  const [archivingEdge, setArchivingEdge] = useState<EdgeArchiveSubject | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const focusAfterEdgeArchive = useRef(false);
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

  // `?? null` and not just the index: an envelope with no nodes is a shape the
  // server does not produce, but `undefined` narrows differently from `null`
  // and every action below is gated on one check.
  const root = load.status === "ready" ? (load.subgraph.nodes[0] ?? null) : null;

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
  const inbound = useMemo(
    () => (load.status === "ready" ? backlinks(load.subgraph) : EMPTY_BACKLINKS),
    [load],
  );

  // The root's edge count as a *fact*, or null when the walk hit its cap and
  // the number is only a floor.
  const rootEdgeCount =
    load.status === "ready" && !load.subgraph.truncated ? rows.length : null;

  const refetch = useCallback(() => setRefresh((value) => value + 1), []);
  const nodeArchive = useNodeArchive(refetch);
  const edgeArchive = useEdgeArchive(() => {
    if (archivingEdge !== null) focusAfterEdgeArchive.current = true;
    refetch();
  });
  const headerMenu = useContextMenu();

  // A successful relationship archive removes its menu opener. Modal cannot
  // restore focus to that detached row, so the host returns it to the reading
  // view's persistent heading once the refreshed rail has committed.
  useEffect(() => {
    if (!focusAfterEdgeArchive.current || load.status !== "ready") return;
    focusAfterEdgeArchive.current = false;
    if (headingRef.current !== null) focusProgrammatically(headingRef.current);
  }, [load]);

  // The header's actions, shared verbatim by its buttons and its menu — the
  // two must not be able to disagree about what this view can do.
  const rootRefusal = root === null ? null : archiveRefusal(root);
  const headerActions: MenuAction[] = root === null ? [] : [
    { id: "edit", label: "Edit", group: "go", onSelect: () => navigate(`/editor/${encodeURIComponent(root.id)}`) },
    { id: "graph", label: "Open in graph", group: "go", onSelect: () => navigate(`/graph/${encodeURIComponent(root.id)}`) },
    { id: "history", label: "Version history", group: "go", onSelect: () => navigate(`/history/${encodeURIComponent(root.id)}`) },
    { id: "link", label: "Link to another node…", group: "act", onSelect: () => setLinkSource(root) },
    {
      id: "archive",
      label: "Archive…",
      group: "act",
      danger: true,
      ...(rootRefusal === null ? {} : { unavailable: rootRefusal }),
      onSelect: () => setArchiving(root),
    },
  ];

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

    const detach = attachWikilinkClicks(container, (title, nodeId) => {
      // An id-form wikilink (`[[<id>]]`) names its node directly; the
      // resolution read is title-only and would miss it, so navigate.
      if (nodeId !== null) {
        navigate(`/node/${nodeId}`);
        return;
      }
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
        <div
          className="nd-node__heading"
          {...(root === null ? {} : { onContextMenu: headerMenu.openAt })}
        >
          {/* Gated on the loaded root: "(untitled)" is a fact about a node
              with no title, never a placeholder for one that has not loaded. */}
          {root ? (
            <h1 ref={headingRef} tabIndex={-1}>
              {root.title ?? "(untitled)"}
            </h1>
          ) : null}
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
            {/* Retirement is one click from the content it retires, and
                disabled with its reason rather than hidden: a node the server
                will not archive is a fact worth reading once. */}
            <button
              type="button"
              className="nd-button nd-button--small nd-button--danger"
              onClick={() => setArchiving(root)}
              disabled={rootRefusal !== null}
              title={rootRefusal ?? undefined}
            >
              Archive
            </button>
            <MenuButton label="Actions for this node" controller={headerMenu} />
          </div>
        ) : null}
      </header>

      {headerMenu.anchor !== null && root !== null ? (
        <ContextMenu
          label="Actions for this node"
          anchor={headerMenu.anchor}
          items={headerActions}
          onClose={headerMenu.close}
        />
      ) : null}

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
              {edgeCountLabel(rows.length, load.subgraph.truncated)}
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
                    onArchiveNode={setArchiving}
                    onArchiveEdge={setArchivingEdge}
                  />
                ))}
              </ul>
            )}
            <p className="nd-meta">
              Click an edge to travel to the far node. Its actions are on the
              row's ⋯ button, or a right-click.
            </p>

            <BacklinksSection backlinks={inbound} />
          </aside>
        </div>
      ) : null}

      {linkSource ? (
        <LinkDialog
          source={linkSource}
          onClose={() => setLinkSource(null)}
          onCreated={refetch}
        />
      ) : null}

      {archiving ? (
        <ArchiveNodeDialog
          node={archiving}
          // Null unless this is the root *and* the walk was complete. `rows`
          // is the root's neighbourhood, so an edge row's menu — which
          // archives the far node — would otherwise state a count belonging to
          // a different node; and a truncated walk's count is the cap rather
          // than the size, which the rail header above states as a floor
          // ("200+ edges") for exactly that reason. Either way the confirm
          // falls back to the uncounted sentence, which is still true.
          edgeCount={rootEdgeCount === null || archiving.id !== root?.id ? null : rootEdgeCount}
          onConfirm={() => nodeArchive.archive(archiving)}
          onClose={() => setArchiving(null)}
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

/**
 * Who links here, and the sentence they link from.
 *
 * A second narrowing of the read the rail above already made, not a second
 * request: an inbound `mentions` edge *is* a wikilink somebody wrote, and the
 * content it was written in came back in the same envelope. Rendered as plain
 * text — a backlink is a glance, and the peek card's rule applies here for the
 * same reason: nothing transient pays for sanitisation.
 */
function BacklinksSection({ backlinks }: { backlinks: readonly Backlink[] }) {
  if (backlinks.length === 0) return null;
  return (
    <section className="nd-node__backlinks" aria-label="Backlinks">
      <h2 className="nd-label">
        {backlinks.length} backlink{backlinks.length === 1 ? "" : "s"}
      </h2>
      <ul className="nd-node__backlink-list">
        {backlinks.map((backlink) => (
          <li key={backlink.edge.id} className="nd-node__backlink">
            <Link
              className="nd-truncate nd-node__backlink-title"
              to={`/node/${encodeURIComponent(backlink.from.id)}`}
            >
              {backlink.from.title ?? backlink.from.id}
            </Link>
            {backlink.crossing ? (
              <span className="nd-node__crossing-mark">crossing</span>
            ) : null}
            {backlink.context === null ? (
              // Says what was observed and stops. Why the link is not findable
              // is not knowable from here: the target may have been renamed
              // since (the mention edge survives until that node is next
              // written), the text may have been edited, or the edge may never
              // have come from a wikilink at all — `mentions` is a selectable
              // type in the link dialog and over MCP. Naming one of those would
              // be inventing a cause.
              <p className="nd-meta nd-node__backlink-context">
                Active edge; no wikilink to this node in that text as it stands.
              </p>
            ) : (
              <p className="nd-node__backlink-context">{backlink.context}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * One incident edge: direction, type, far endpoint, crossing mark, state.
 *
 * A right-click (or the row's `⋯`) distinguishes actions on the far node from
 * the destructive action on the relationship represented by this row.
 */
function EdgeRow({
  row,
  spaces,
  archivedSpaces,
  onArchiveNode,
  onArchiveEdge,
}: {
  row: IncidentRow;
  spaces: readonly NodeOut[] | null;
  archivedSpaces: readonly NodeOut[];
  /** Opens the archive confirm for the far node; the view owns the dialog. */
  onArchiveNode: (node: NodeOut) => void;
  /** Opens the archive confirm for this exact relationship. */
  onArchiveEdge: (subject: EdgeArchiveSubject) => void;
}) {
  const navigate = useNavigate();
  const menu = useContextMenu();
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
    return (
      <li className="nd-node__edge-line">
        <span className="nd-node__edge-row nd-node__edge-row--disabled">{cells}</span>
        {/* A spacer the size of the button every other row carries, for the
            reason the cells above keep an empty crossing column: the rail is
            scanned down its right-hand edge, and one row without the control
            would shift its state badge out of line with all the others. Not a
            disabled button — a disabled control is unfocusable and its `title`
            unreliable, so the reason would be readable by nobody. The row
            already carries it: the far endpoint renders as "(missing)". */}
        <span className="nd-menu-button nd-menu-button--absent" aria-hidden="true">
          ⋯
        </span>
      </li>
    );
  }

  const refusal = archiveRefusal(far);
  const edgeRefusal = edgeArchiveRefusal(row.edge);
  const items: MenuAction[] = [
    { id: "open", label: "Open", group: "go", onSelect: () => navigate(`/node/${encodeURIComponent(far.id)}`) },
    { id: "edit", label: "Edit", group: "go", onSelect: () => navigate(`/editor/${encodeURIComponent(far.id)}`) },
    { id: "graph", label: "Open in graph", group: "go", onSelect: () => navigate(`/graph/${encodeURIComponent(far.id)}`) },
    {
      id: "archive-node",
      label: "Archive far node…",
      group: "act",
      danger: true,
      ...(refusal === null ? {} : { unavailable: refusal }),
      onSelect: () => onArchiveNode(far),
    },
    {
      id: "archive-edge",
      label: "Archive relationship…",
      group: "act",
      danger: true,
      ...(edgeRefusal === null ? {} : { unavailable: edgeRefusal }),
      onSelect: () =>
        onArchiveEdge({
          edge: row.edge,
          source: row.edge.src_id === far.id ? far : row.near,
          destination: row.edge.dst_id === far.id ? far : row.near,
        }),
    },
  ];

  return (
    // The row is a link and the button is its sibling, not its child: an
    // interactive control inside an anchor is invalid and unreachable. The
    // button is not optional — a right-click does not exist on touch, and
    // these neighbour actions are the whole point of the rail's menu.
    <li className="nd-node__edge-line">
      <Link
        className="nd-node__edge-row"
        to={`/node/${encodeURIComponent(far.id)}`}
        onContextMenu={menu.openAt}
      >
        {cells}
      </Link>
      <MenuButton label={`Actions for ${label}`} controller={menu} />
      {menu.anchor !== null ? (
        <ContextMenu
          label={`Actions for ${label}`}
          anchor={menu.anchor}
          items={items}
          onClose={menu.close}
        />
      ) : null}
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
