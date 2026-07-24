import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { NodeBadge } from "../../components";
import type { SearchHit } from "../../api/types";
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
 * @param terms Query terms to mark in the snippet.
 * @param linkRef Ref callback for the title link.
 * @param onKeyDown Row-level keyboard handler.
 */
export function ResultRow({
  hit,
  index,
  knownState,
  terms,
  linkRef,
  onKeyDown,
}: ResultRowProps) {
  const signals = describeSignals(hit);
  const title = hit.title?.trim() ? hit.title : "Untitled";
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
    >
      <div className="nd-search-hit__head">
        <Link
          ref={linkRef}
          to={`/editor/${encodeURIComponent(hit.node_id)}`}
          className="nd-search-hit__title"
        >
          {title}
        </Link>
        <NodeBadge type={hit.type} state={knownState} />
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
        </span>
      </div>
    </li>
  );
}
