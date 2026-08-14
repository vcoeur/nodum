import { ApiError, isUnknownSpace } from "../../api/client";
import type { NodeOut, NodeState, TypeOut } from "../../api/types";
import { nameSpace } from "../../components/spaceNaming";

/** Maximum rows returned by the existing node-list endpoint. */
export const NODE_BROWSE_LIMIT = 500;

/** Client-side orders applied only to the bounded response. */
export type NodeSort = "created-asc" | "created-desc" | "title-asc" | "title-desc";

/** URL-backed browse controls. */
export interface NodeBrowseState {
  type: string;
  space: string;
  state: "" | NodeState;
  sort: NodeSort;
}

/** One node-type picker option, including a selected value absent from the catalog. */
export interface NodeTypeOption {
  value: string;
  label: string;
  unlisted: boolean;
}

/** Copy for a filter-specific refusal. */
export interface NodeBrowseFailure {
  title: string;
  detail: string;
  clear: "space" | "type" | null;
}

/** Read browse controls from URL search parameters. */
export function readNodeBrowseState(params: URLSearchParams): NodeBrowseState {
  const state = params.get("state");
  const sort = params.get("sort");
  return {
    type: params.get("type") ?? "",
    space: params.get("space") ?? "",
    state: state === "active" || state === "proposed" || state === "archived" ? state : "",
    sort:
      sort === "created-desc" || sort === "title-asc" || sort === "title-desc"
        ? sort
        : "created-asc",
  };
}

/** Encode browse controls, omitting defaults for stable shareable URLs. */
export function toNodeBrowseParams(state: NodeBrowseState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.type) params.set("type", state.type);
  if (state.space) params.set("space", state.space);
  if (state.state) params.set("state", state.state);
  if (state.sort !== "created-asc") params.set("sort", state.sort);
  return params;
}

/** Keep a stale shared type filter visible without offering it as a catalog choice. */
export function nodeTypeOptions(
  types: readonly TypeOut[],
  selected: string,
): NodeTypeOption[] {
  const options = types.map((type) => ({
    value: type.name === selected ? selected : type.id,
    label: type.name,
    unlisted: false,
  }));
  if (selected && !types.some((type) => type.id === selected || type.name === selected)) {
    options.push({ value: selected, label: selected, unlisted: true });
  }
  return options;
}

/** Describe a filter refusal without exposing scoped-space server copy. */
export function describeNodeBrowseFailure(
  error: unknown,
  selectedType: string,
  activeSpaces: readonly NodeOut[] | null,
  archivedSpaces: readonly NodeOut[],
): NodeBrowseFailure {
  if (isUnknownSpace(error)) {
    const named = nameSpace(error.space, activeSpaces, archivedSpaces);
    const detail =
      named.kind === "archived"
        ? `${named.label} has been archived, so the server will no longer apply it as a space filter. Its nodes remain in the graph. Clear the filter or choose an active space.`
        : `${named.label} could not be applied. A space stops resolving once it is archived, and a renamed space no longer answers to its old name. Clear the filter or choose an active space.`;
    return { title: "That space filter could not be applied", detail, clear: "space" };
  }
  if (
    selectedType &&
    error instanceof ApiError &&
    error.type === "TypeNotFound" &&
    error.message.startsWith("unknown node type:")
  ) {
    return {
      title: "That node type filter could not be applied",
      detail: `${selectedType} is no longer in the node-type catalog. The shared URL has been preserved; clear this filter or choose a current type.`,
      clear: "type",
    };
  }
  return {
    title: "Nodes could not be loaded",
    detail: error instanceof Error ? error.message : "The request failed.",
    clear: null,
  };
}

/** Sort a copy of one bounded server response. */
export function sortNodes(nodes: readonly NodeOut[], sort: NodeSort): NodeOut[] {
  const direction = sort.endsWith("desc") ? -1 : 1;
  const byTitle = sort.startsWith("title");
  return [...nodes].sort((left, right) => {
    const leftValue = byTitle ? (left.title ?? "") : left.created_at;
    const rightValue = byTitle ? (right.title ?? "") : right.created_at;
    const compared = leftValue.localeCompare(rightValue);
    return compared === 0 ? left.id.localeCompare(right.id) : compared * direction;
  });
}
