import type { KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ContextMenu, MenuButton, NodeBadge, NodePeek, useContextMenu } from "../../components";
import type { MenuAction, SpaceName } from "../../components";
import type { SearchHit } from "../../api/types";
import { hitSpaceTitle } from "./resultSpace";
import { SignalBreakdown } from "./SignalBreakdown";
import { describeSignals } from "./signals";
import { renderSnippet } from "./snippet";

/**
 * One result row.
 *
 * The title is a real `<a href>` to the editor, which is what makes the list
 * keyboard-navigable for free: arrow keys move DOM focus between the title
 * links and Enter is the browser's own activation, not a re-implementation of
 * it. The row highlights on `:focus-within`, so focus anywhere inside it —
 * title, subgraph link — reads as "this row".
 *
 * The head's right-hand marks are the row's dimensions, in filter order: the
 * space (only when the filter left it open — see `resultSpace.ts`), then type
 * and state. They sit at one x position down the list precisely because the
 * list is scanned rather than read.
 *
 * A right-click anywhere on the row — or its `⋯` — opens the shared context
 * menu on that hit's node. The actions are **travel only**: a search hit is a
 * projection (id, title, type, snippet), not a node, so nothing here can state
 * what archiving it would cost. Retirement lives one click away, on the
 * surfaces that hold the whole node.
 */

interface ResultRowProps {
  hit: SearchHit;
  /** Position in the flattened display order; the arrow-key handler needs it. */
  index: number;
  /**
   * The state every hit in this response is in, or null when it is unknowable.
   *
   * `SearchHit` carries no state field, but the server filters by `n.state`, so
   * with a concrete state filter in force every hit provably has that state.
   * Under "any state" it is genuinely unknown and the badge shows type alone
   * rather than guessing.
   */
  knownState: string | null;
  /**
   * This hit's space, resolved, or null when the row states none.
   *
   * Computed by `hitSpaceName` (`resultSpace.ts`), which follows the same rule
   * as `knownState` one dimension over: an active space filter determines every
   * hit's space, so the row names it only when the search spanned more than
   * one. It carries *how* the name resolved as well as the name, because a hit
   * in a space archived since it was written is a fact the row has to show —
   * the alternative is the 32-hex id this once rendered.
   */
  spaceName: SpaceName | null;
  /** Query terms to mark in the snippet. */
  terms: string[];
  /** Registers the title link so the view can move focus to this row. */
  linkRef: (element: HTMLAnchorElement | null) => void;
  /** Row-level key handling: arrows, Escape, and the subgraph shortcut. */
  onKeyDown: (event: KeyboardEvent<HTMLElement>, index: number) => void;
}

/**
 * Render one search hit.
 *
 * @param hit The hit, verbatim from the server.
 * @param index Position in the flattened display order.
 * @param knownState The state implied by the active filter, or null.
 * @param spaceName The space to name on this row, or null.
 * @param terms Query terms to mark in the snippet.
 * @param linkRef Ref callback for the title link.
 * @param onKeyDown Row-level keyboard handler.
 */
export function ResultRow({
  hit,
  index,
  knownState,
  spaceName,
  terms,
  linkRef,
  onKeyDown,
}: ResultRowProps) {
  const navigate = useNavigate();
  const menu = useContextMenu();
  const signals = describeSignals(hit);
  const title = hit.title?.trim() ? hit.title : "Untitled";
  const nodePath = encodeURIComponent(hit.node_id);
  const items: MenuAction[] = [
    { id: "open", label: "Open", group: "go", onSelect: () => navigate(`/node/${nodePath}`) },
    { id: "edit", label: "Edit", group: "go", onSelect: () => navigate(`/editor/${nodePath}`) },
    { id: "graph", label: "Open in graph", group: "go", onSelect: () => navigate(`/graph/${nodePath}`) },
    { id: "history", label: "Version history", group: "go", onSelect: () => navigate(`/history/${nodePath}`) },
  ];
  // An expansion hit has no matched text: the server falls back to the title,
  // which would render as a snippet echoing the line above it. Say what the row
  // actually is instead.
  const snippetEchoesTitle = hit.snippet.trim() === (hit.title ?? "").trim();
  const showPlaceholder = signals.isNeighbour && snippetEchoesTitle;

  return (
    <li
      className={
        signals.isNeighbour ? "nd-search-hit nd-search-hit--neighbour" : "nd-search-hit"
      }
      onKeyDown={(event) => onKeyDown(event, index)}
      onContextMenu={menu.openAt}
    >
      <div className="nd-search-hit__head">
        <NodePeek nodeId={hit.node_id}>
          <Link
            ref={linkRef}
            to={`/editor/${encodeURIComponent(hit.node_id)}`}
            className="nd-search-hit__title"
          >
            {title}
          </Link>
        </NodePeek>
        <span className="nd-row nd-search-hit__marks">
          {spaceName === null ? null : (
            <span className="nd-meta nd-search-hit__space" title={hitSpaceTitle(spaceName)}>
              in <span className="nd-mono">{spaceName.label}</span>
              {/* Marked, not merely named: a hit in a retired space is worth a
                  second's pause, and the filter cannot narrow to it. */}
              {spaceName.kind === "archived" ? (
                <span className="nd-badge nd-badge--archived nd-search-hit__space-mark">
                  <span className="nd-badge__dot" aria-hidden="true" />
                  archived
                </span>
              ) : null}
            </span>
          )}
          <NodeBadge type={hit.type} state={knownState} />
        </span>
      </div>

      {showPlaceholder ? (
        <p className="nd-search-hit__snippet nd-search-hit__snippet--placeholder">
          No matched text — reached in one hop from a match.
        </p>
      ) : (
        <p className="nd-search-hit__snippet">{renderSnippet(hit.snippet, terms)}</p>
      )}

      <div className="nd-search-hit__foot">
        <SignalBreakdown signals={signals} score={hit.score} />
        <span className="nd-search-hit__actions">
          <span className="nd-mono nd-search-hit__id" title={hit.node_id}>
            {hit.node_id}
          </span>
          <Link
            to={`/graph/${encodeURIComponent(hit.node_id)}`}
            className="nd-button nd-button--ghost nd-button--small"
            title="Open this node as the root of a subgraph (→)"
          >
            Subgraph
          </Link>
          <MenuButton label={`Actions for ${title}`} controller={menu} />
        </span>
      </div>

      {menu.anchor !== null ? (
        <ContextMenu
          label={`Actions for ${title}`}
          anchor={menu.anchor}
          ignore={menu.opener}
          items={items}
          onClose={menu.close}
        />
      ) : null}
    </li>
  );
}
