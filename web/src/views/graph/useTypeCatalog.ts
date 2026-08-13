/**
 * The live node-type and edge-type catalog, fetched once per mount.
 *
 * The type filters need real type ids to offer. When `/api/types` cannot be
 * reached the view falls back to the types present in the rendered subgraph —
 * a smaller list, but never a blank one, and it keeps the filter usable on a
 * server that is only half up.
 */

import { useNodeTypes } from "../../components";

/** The catalog, or what is known of it. */
export interface TypeCatalog {
  nodeTypes: string[];
  edgeTypes: string[];
  /** True while the first fetch is in flight. */
  loading: boolean;
  /** True when the catalog could not be read and the lists are fallbacks. */
  degraded: boolean;
}

/**
 * Fetch the type catalog.
 *
 * @returns Sorted node-type and edge-type ids.
 */
export function useTypeCatalog(): TypeCatalog {
  const nodeTypeList = useNodeTypes();
  return {
    nodeTypes: (nodeTypeList.types ?? []).map((type) => type.id).sort(),
    edgeTypes: (nodeTypeList.edgeTypes ?? []).map((type) => type.id).sort(),
    loading: nodeTypeList.types === null,
    degraded: nodeTypeList.failed,
  };
}

/**
 * Merge the catalog with the types actually present, so a filter can always be
 * cleared even when it names a type the catalog does not list.
 *
 * @param catalog Type ids from the server.
 * @param present Type ids seen in the current result.
 * @param selected Type ids the URL currently filters on.
 * @returns The union, sorted.
 */
export function offeredTypes(
  catalog: readonly string[],
  present: readonly string[],
  selected: readonly string[],
): string[] {
  return [...new Set([...catalog, ...present, ...selected])].sort();
}
