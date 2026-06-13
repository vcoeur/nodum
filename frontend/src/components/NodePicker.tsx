// A search-and-select picker restricted to a set of allowed node kinds, used to
// choose an edge's endpoints. The selected hit is owned by the parent.

import { useState } from "react";
import type { KeyboardEvent } from "react";

import { apiGet } from "../api";
import type { SearchHit, SearchResult } from "../types";
import { errorMessage, nodeText, shortUuid } from "../util";

interface NodePickerProps {
  allowedKinds: string[];
  selected: SearchHit | null;
  onSelect: (hit: SearchHit) => void;
}

export function NodePicker({ allowedKinds, selected, onSelect }: NodePickerProps) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);

  async function run() {
    const trimmed = query.trim();
    if (!trimmed) return;
    setStatus("Searching…");
    setHits([]);
    try {
      const response = await apiGet<SearchResult>(`/search?q=${encodeURIComponent(trimmed)}&limit=20`);
      const filtered = response.hits.filter((hit) => allowedKinds.includes(hit.kind));
      setHits(filtered);
      setStatus(filtered.length === 0 ? "No matches in allowed kinds." : "");
    } catch (error) {
      setStatus(errorMessage(error, "Search failed."));
    }
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter") {
      event.preventDefault();
      run();
    }
  }

  return (
    <div className="picker">
      <div className="picker-input-row">
        <input
          type="search"
          placeholder="type, then Find…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <button type="button" onClick={run}>
          Find
        </button>
      </div>
      {status && <div className="status">{status}</div>}
      <ul className="picker-results">
        {hits.map((hit) => (
          <li key={hit.uuid}>
            <button
              type="button"
              className="picker-hit"
              onClick={() => {
                onSelect(hit);
                setHits([]);
              }}
            >
              <span className="tag">{hit.kind}</span>
              <span className="hit-text">{nodeText(hit.data)}</span>
              <span className="uuid">{shortUuid(hit.uuid)}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className={selected ? "picker-selected" : "picker-selected muted"}>
        {selected ? (
          <>
            <span className="tag">{selected.kind}</span>
            <span className="hit-text">{nodeText(selected.data)}</span>
            <span className="uuid">{shortUuid(selected.uuid)}</span>
          </>
        ) : (
          "none selected"
        )}
      </div>
    </div>
  );
}
