import { SpaceFilter } from "../../components";
import type { NodeOut, SpaceOut, TypeOut } from "../../api/types";
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
 * a reason rather than pretending "any type" was a choice. The space list is
 * the same shape of thing over `GET /api/spaces`, which is why both go through
 * the shared control rather than a local `<select>`.
 *
 * The space filter is the **read** half of design decision D1 and nothing else:
 * it narrows what this search spans and says nothing about where the editor
 * files new nodes. Reading `research` while still writing into `main` is the
 * ordinary case, so nothing here may touch the write target.
 */

interface SearchFilterBarProps {
  state: SearchState;
  /** Node types from the live catalog; null while loading or after a failure. */
  nodeTypes: TypeOut[] | null;
  /** True once the catalog request failed — the control says so. */
  typesFailed: boolean;
  /** Active spaces from `GET /api/spaces`; null while loading or after a failure. */
  spaces: SpaceOut[] | null;
  /**
   * Archived space nodes, so a filter left pointing at one is named rather than
   * shown as its id. Never a choice — see `SpaceFilter`.
   */
  archivedSpaces: readonly NodeOut[];
  /** True once the space list request failed — the control says so. */
  spacesFailed: boolean;
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
 * @param spaces The live space list.
 * @param archivedSpaces Archived space nodes, for naming a retired selection.
 * @param spacesFailed Whether the space-list request failed.
 * @param onChange Applies a partial state change.
 */
export function SearchFilterBar({
  state,
  nodeTypes,
  typesFailed,
  spaces,
  archivedSpaces,
  spacesFailed,
  onChange,
}: SearchFilterBarProps) {
  return (
    <div className="nd-search-filters" role="group" aria-label="Search filters">
      <SpaceFilter
        className="nd-search-filters__field"
        controlClassName="nd-search-filters__control"
        value={state.space}
        onChange={(space) => onChange({ space })}
        spaces={spaces}
        archivedSpaces={archivedSpaces}
        failed={spacesFailed}
      />

      <label className="nd-search-filters__field">
        <span className="nd-label">Type</span>
        <select
          name="search-type"
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
          name="search-state"
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
          name="search-created-by"
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
          name="search-limit"
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
          name="search-order"
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
          name="search-expand"
          type="checkbox"
          checked={state.expand}
          onChange={(event) => onChange({ expand: event.target.checked })}
        />
        Include neighbours of matches
      </label>

      {/* D3: meta holds the type and space vocabulary, not content, so it is
          off by default and every content listing stays clean. Narrowing the
          space filter to `meta` is the same opt-in said precisely, which is why
          this toggle is not disabled when that is the selection. */}
      <label
        className="nd-search-filters__toggle"
        title="Search the meta space too — node types, edge types, spaces, conventions. Off by default so content listings stay clean. Narrowing the space filter to meta does the same thing."
      >
        <input
          name="search-include-meta"
          type="checkbox"
          checked={state.includeMeta}
          onChange={(event) => onChange({ includeMeta: event.target.checked })}
        />
        Show meta
      </label>
    </div>
  );
}
