/**
 * The search view's URL state.
 *
 * Every knob that changes *what* is searched — and the one that changes how the
 * result list is arranged — lives in the query string, so a search is
 * linkable, bookmarkable, and survives a reload. Values equal to the default
 * are omitted when writing, which keeps the common case at a clean
 * `/search?q=knot`.
 *
 * Reading is total: an unknown or malformed parameter falls back to its
 * default rather than throwing, because the query string is user-editable and a
 * typo must not blank the view.
 */

// Imported from the module rather than the `components` barrel: this file is
// pure and the barrel re-exports React components.
import { ANY_SPACE } from "../../components/spaceOptions";
import type { NodeState } from "../../api/types";

/** Node-state filter. `any` searches every state (the service's `state=None`). */
export type StateFilter = NodeState | "any";

/**
 * How the result list is arranged.
 *
 * Neither mode re-ranks: `score` renders the server's list in the order it
 * arrived, and `signal` only *partitions* that list, preserving relative order
 * inside each group. The fusion is the product; the client never redoes it.
 */
export type GroupMode = "score" | "signal";

/** Everything the search view keeps in the URL. */
export interface SearchState {
  /** The raw query text. Empty means "no search yet", not "search for nothing". */
  query: string;
  /** Node-type id, or `""` for every type. */
  type: string;
  /** Node-state filter. */
  state: StateFilter;
  /** `created_by` filter (e.g. `agent:researcher`), or `""` for every writer. */
  createdBy: string;
  /**
   * Space filter: a space id or name, or {@link ANY_SPACE} for every space in
   * scope (the default).
   *
   * The read half of design decision D1. It **narrows and never widens** — the
   * principal's own scope still applies underneath it — and it is completely
   * independent of the write target, which is app-wide state rather than URL
   * state precisely because reading `research` while filing into `main` is the
   * ordinary case.
   */
  space: string;
  /**
   * Include the meta space — types, spaces, conventions. Off by default (D3).
   *
   * Independent of {@link SearchState.space} on the wire but not in effect:
   * naming `meta` in the space filter **is itself the opt-in** server-side,
   * since the default exclusion only applies to an unnarrowed read. So a search
   * narrowed to meta answers with meta nodes whether or not this is on.
   */
  includeMeta: boolean;
  /** Result cap, sent as the server's `k`. */
  limit: number;
  /** Append one-hop active-edge neighbours of the fused hits (the server's `expand`). */
  expand: boolean;
  /** Client-side arrangement of the returned list. */
  group: GroupMode;
}

/** The state filters offered, in the order they appear in the control. */
export const STATE_FILTERS: readonly StateFilter[] = ["active", "proposed", "archived", "any"];

/** Result caps offered in the limit control. */
export const LIMIT_CHOICES: readonly number[] = [10, 20, 50, 100];

/** Highest `k` accepted from a hand-edited URL. */
const MAX_LIMIT = 200;

/**
 * The state a bare `/search` means.
 *
 * `state: "active"` and `limit: 20` mirror the service defaults closely enough
 * that the common search sends almost nothing.
 */
export const DEFAULT_SEARCH_STATE: SearchState = {
  query: "",
  type: "",
  state: "active",
  createdBy: "",
  space: ANY_SPACE,
  includeMeta: false,
  limit: 20,
  expand: false,
  group: "score",
};

/**
 * Decode the search state from a URL query string.
 *
 * @param params The route's search parameters.
 * @returns A fully-populated state; unparseable values fall back to defaults.
 */
export function readSearchState(params: URLSearchParams): SearchState {
  const rawState = params.get("state");
  const rawLimit = Number.parseInt(params.get("k") ?? "", 10);
  const isKnownState = rawState !== null && STATE_FILTERS.includes(rawState as StateFilter);

  return {
    query: params.get("q") ?? DEFAULT_SEARCH_STATE.query,
    type: params.get("type") ?? DEFAULT_SEARCH_STATE.type,
    state: isKnownState ? (rawState as StateFilter) : DEFAULT_SEARCH_STATE.state,
    createdBy: params.get("by") ?? DEFAULT_SEARCH_STATE.createdBy,
    // Kept verbatim, id or name: a space reference resolves either way and only
    // the server can say which one still does. Trimmed because a hand-edited or
    // wrapped URL is the usual source of a stray space.
    space: (params.get("space") ?? DEFAULT_SEARCH_STATE.space).trim(),
    includeMeta: params.get("meta") === "1",
    limit:
      Number.isFinite(rawLimit) && rawLimit > 0
        ? Math.min(rawLimit, MAX_LIMIT)
        : DEFAULT_SEARCH_STATE.limit,
    expand: params.get("expand") === "1",
    group: params.get("group") === "signal" ? "signal" : DEFAULT_SEARCH_STATE.group,
  };
}

/**
 * Encode the search state back into a query string, omitting defaults.
 *
 * @param state The state to serialise.
 * @returns Parameters ready to hand to `setSearchParams`.
 */
export function toSearchParams(state: SearchState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.type) params.set("type", state.type);
  if (state.state !== DEFAULT_SEARCH_STATE.state) params.set("state", state.state);
  if (state.createdBy) params.set("by", state.createdBy);
  if (state.space) params.set("space", state.space);
  if (state.includeMeta) params.set("meta", "1");
  if (state.limit !== DEFAULT_SEARCH_STATE.limit) params.set("k", String(state.limit));
  if (state.expand) params.set("expand", "1");
  if (state.group !== DEFAULT_SEARCH_STATE.group) params.set("group", state.group);
  return params;
}

/** The keys a filter reset restores — every control that narrows the *read*. */
type FilterKey = "type" | "state" | "createdBy" | "space" | "includeMeta" | "limit" | "expand";

/**
 * What "clear the filters" restores.
 *
 * `query` is not here (clearing it would be clearing the search) and neither is
 * `group`, which arranges the list the server returned and changes nothing
 * about what was asked for — offering to clear the filters because of it would
 * be a lie. {@link hasActiveFilters} reads this same object, so the badge that
 * offers the reset and the reset itself cannot come to disagree.
 */
export const CLEARABLE_FILTERS: Pick<SearchState, FilterKey> = {
  type: DEFAULT_SEARCH_STATE.type,
  state: DEFAULT_SEARCH_STATE.state,
  createdBy: DEFAULT_SEARCH_STATE.createdBy,
  space: DEFAULT_SEARCH_STATE.space,
  includeMeta: DEFAULT_SEARCH_STATE.includeMeta,
  limit: DEFAULT_SEARCH_STATE.limit,
  expand: DEFAULT_SEARCH_STATE.expand,
};

/** True when nothing but the query (and the arrangement) is at its default. */
export function hasActiveFilters(state: SearchState): boolean {
  const keys = Object.keys(CLEARABLE_FILTERS) as FilterKey[];
  return keys.some((key) => state[key] !== CLEARABLE_FILTERS[key]);
}
