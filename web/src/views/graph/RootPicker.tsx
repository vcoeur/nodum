/**
 * Choosing what to look at.
 *
 * A graph view with no root is not an error state, it is a question, so the
 * empty state asks it properly: a title search, a list of what is actually in
 * the graph to start from, and a field that takes a node id straight if you
 * already have one. The same component backs the "change root" popover, which
 * is why it takes a `compact` mode rather than being written twice.
 */

import { useEffect, useState } from "react";
import { NodeBadge, Spinner } from "../../components";
import { classifyFailure } from "./errors";
import { listRootCandidates, searchRoots } from "./rootSearch";
import type { RootCandidate } from "./rootSearch";

/** How long typing settles before the lookup runs. */
const SEARCH_DEBOUNCE_MS = 200;

/** Candidates offered for a query, and for the opening state. */
const SEARCH_LIMIT = 20;
const BROWSE_LIMIT = 12;

interface RootPickerProps {
  /** Called with the chosen node id. */
  onPick: (nodeId: string) => void;
  /** Tighter layout, for use inside the toolbar popover. */
  compact?: boolean;
  /** Take focus on mount — true in the popover, where the user just asked for it. */
  autoFocus?: boolean;
}

/**
 * Render the root picker.
 *
 * @param props See {@link RootPickerProps}.
 */
export function RootPicker({ onPick, compact = false, autoFocus = false }: RootPickerProps) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<RootCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    const trimmed = query.trim();
    const timer = window.setTimeout(() => {
      const lookup = trimmed
        ? searchRoots(trimmed, SEARCH_LIMIT, controller.signal)
        : listRootCandidates(BROWSE_LIMIT, controller.signal);
      lookup
        .then((rows) => {
          setCandidates(rows);
          setLoading(false);
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          setError(cause);
          setCandidates([]);
          setLoading(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  const failure = error ? classifyFailure(error) : null;

  return (
    <div className={compact ? "nd-graph__picker nd-graph__picker--compact" : "nd-graph__picker"}>
      <label className="nd-field">
        <span className="nd-label">Root node</span>
        <input
          name="graph-root"
          className="nd-input"
          type="search"
          value={query}
          autoFocus={autoFocus}
          placeholder="Search by title, or paste a node id"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            // A pasted id is not a title search — let Enter take it as-is.
            if (event.key === "Enter" && query.trim() && candidates.length === 0) {
              onPick(query.trim());
            }
          }}
        />
      </label>

      <p className="nd-graph__hint">
        {query.trim()
          ? "Titles starting with what you typed."
          : "Nodes in the graph, oldest first — or type to search."}
      </p>

      {failure ? (
        <p className="nd-graph__warn">
          {failure.title}. {failure.detail}
        </p>
      ) : null}

      {loading ? (
        <div className="nd-graph__picker-loading">
          <Spinner label="Looking up nodes" />
        </div>
      ) : null}

      {!loading && !failure && candidates.length === 0 ? (
        <p className="nd-graph__hint">
          {query.trim()
            ? "No titles start with that. Press Enter to treat it as a node id."
            : "The graph has no nodes yet."}
        </p>
      ) : null}

      <ul className="nd-graph__candidates">
        {candidates.map((candidate) => (
          <li key={candidate.id}>
            <button
              type="button"
              className="nd-graph__candidate"
              onClick={() => onPick(candidate.id)}
            >
              <span className="nd-graph__candidate-title nd-truncate">
                {candidate.title ?? "(untitled)"}
              </span>
              <NodeBadge type={candidate.type} />
              <span className="nd-mono nd-truncate">{candidate.id}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
