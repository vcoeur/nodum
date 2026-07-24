import type { TypeOut } from "../../api/types";
import { LIMIT_CHOICES, STATE_FILTERS } from "./searchState";
import type { GroupMode, SearchState, StateFilter } from "./searchState";

/**
 * The filter row under the query box.
 *
 * Every control writes straight through to the URL — there is no apply button,
 * because a filter that needs confirming is a filter you stop using. Filters
 * are not debounced (the query is); a select change is already a deliberate act.
 *
 * The type list comes from `GET /api/types` and is never hardcoded: node types
 * are user-extensible, so a fixed list would be wrong the moment someone adds
 * one. When the catalog cannot be loaded the control degrades to disabled with
 * a reason rather than pretending "any type" was a choice.
 */

interface SearchFilterBarProps {
  state: SearchState;
  /** Node types from the live catalog; null while loading or after a failure. */
  nodeTypes: TypeOut[] | null;
  /** True once the catalog request failed — the control says so. */
  typesFailed: boolean;
  /** Apply a partial change to the URL state. */
  onChange: (patch: Partial<SearchState>) => void;
}

/** Human labels for the state filter. */
const STATE_LABEL: Record<StateFilter, string> = {
  active: "Active",
  proposed: "Proposed",
  archived: "Archived",
  any: "Any state",
};

/**
 * Render the filter controls.
 *
 * @param state Current URL-backed search state.
 * @param nodeTypes The live node-type catalog.
 * @param typesFailed Whether the catalog request failed.
 * @param onChange Applies a partial state change.
 */
export function SearchFilterBar({
  state,
  nodeTypes,
  typesFailed,
  onChange,
}: SearchFilterBarProps) {
  return (
    <div className="nd-search-filters" role="group" aria-label="Search filters">
      <label className="nd-search-filters__field">
        <span className="nd-label">Type</span>
        <select
          className="nd-select nd-search-filters__control"
          value={state.type}
          disabled={nodeTypes === null}
          title={
            typesFailed
              ? "The type catalog could not be loaded — is the server running?"
              : "Restrict to one node type"
          }
          onChange={(event) => onChange({ type: event.target.value })}
        >
          <option value="">{typesFailed ? "Types unavailable" : "Any type"}</option>
          {(nodeTypes ?? []).map((type) => (
            <option key={type.id} value={type.id}>
              {type.name}
            </option>
          ))}
        </select>
      </label>

      <label className="nd-search-filters__field">
        <span className="nd-label">State</span>
        <select
          className="nd-select nd-search-filters__control"
          value={state.state}
          onChange={(event) => onChange({ state: event.target.value as StateFilter })}
        >
          {STATE_FILTERS.map((value) => (
            <option key={value} value={value}>
              {STATE_LABEL[value]}
            </option>
          ))}
        </select>
      </label>

      <label className="nd-search-filters__field nd-search-filters__field--wide">
        <span className="nd-label">Created by</span>
        <input
          className="nd-input nd-input--mono nd-search-filters__control"
          type="text"
          value={state.createdBy}
          placeholder="anyone"
          spellCheck={false}
          autoComplete="off"
          title="Exact writer identity, e.g. human or agent:researcher"
          onChange={(event) => onChange({ createdBy: event.target.value })}
        />
      </label>

      <label className="nd-search-filters__field nd-search-filters__field--narrow">
        <span className="nd-label">Limit</span>
        <select
          className="nd-select nd-search-filters__control"
          value={state.limit}
          onChange={(event) => onChange({ limit: Number(event.target.value) })}
        >
          {LIMIT_CHOICES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
          {LIMIT_CHOICES.includes(state.limit) ? null : (
            <option value={state.limit}>{state.limit}</option>
          )}
        </select>
      </label>

      <label className="nd-search-filters__field nd-search-filters__field--narrow">
        <span className="nd-label">Order</span>
        <select
          className="nd-select nd-search-filters__control"
          value={state.group}
          title="Grouping only partitions the server's list — it never re-ranks it."
          onChange={(event) => onChange({ group: event.target.value as GroupMode })}
        >
          <option value="score">Fused score</option>
          <option value="signal">By signal</option>
        </select>
      </label>

      <label
        className="nd-search-filters__toggle"
        title="After fusion, add the one-hop active-edge neighbours of the matches. They did not match your query — they sit next to something that did."
      >
        <input
          type="checkbox"
          checked={state.expand}
          onChange={(event) => onChange({ expand: event.target.checked })}
        />
        Include neighbours of matches
      </label>
    </div>
  );
}
