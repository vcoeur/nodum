// Full-text search with an optional kind filter; results are clickable to open
// the node in the detail view.

import { useState } from "react";
import type { FormEvent } from "react";

import { apiGet } from "../api";
import { useSchema } from "../schema";
import type { SearchResult } from "../types";
import { errorMessage, nodeText, shortUuid } from "../util";

interface SearchProps {
  onOpen: (uuid: string) => void;
}

export function Search({ onOpen }: SearchProps) {
  const { schema } = useSchema();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [limit, setLimit] = useState("20");
  const [status, setStatus] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);

  async function run(event: FormEvent) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) {
      setStatus("Enter a query.");
      return;
    }
    const params = new URLSearchParams({ q: trimmed, limit: limit || "20" });
    if (kind) params.set("kind", kind);
    setStatus("Searching…");
    setResult(null);
    try {
      const response = await apiGet<SearchResult>(`/search?${params.toString()}`);
      setResult(response);
      setStatus(`${response.total} hit(s) for “${response.query}”`);
    } catch (error) {
      setStatus(errorMessage(error, "Search failed."));
    }
  }

  return (
    <section>
      <h2>Search</h2>
      <form onSubmit={run}>
        <input
          type="search"
          placeholder="Search node text…"
          autoComplete="off"
          autoFocus
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <label className="inline">
          kind
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="">(any kind)</option>
            {schema.node_kinds.map((nodeKind) => (
              <option key={nodeKind.name} value={nodeKind.name}>
                {nodeKind.name}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          limit
          <input
            type="number"
            min={1}
            max={200}
            value={limit}
            onChange={(event) => setLimit(event.target.value)}
          />
        </label>
        <button type="submit">Search</button>
      </form>
      <div className="status" aria-live="polite">
        {status}
      </div>
      <ul className="results">
        {result?.hits.map((hit) => (
          <li key={hit.uuid}>
            <button type="button" className="hit" onClick={() => onOpen(hit.uuid)}>
              <span className="hit-text">{nodeText(hit.content)}</span>
              <span className="hit-meta">
                <span className="tag">{hit.kind}</span>
                <span className="score">score {hit.score.toFixed(4)}</span>
                <span className="uuid">{shortUuid(hit.uuid)}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
