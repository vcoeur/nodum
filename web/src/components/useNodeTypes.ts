/** The shared node-type catalog read used by every type picker. */

import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { EdgeTypeOut, TypeOut } from "../api/types";
import { describeError } from "../lib";

/** State returned by {@link useNodeTypes}. */
export interface NodeTypeList {
  /** Node types in server catalog order, or null while loading. */
  types: TypeOut[] | null;
  /** Edge types from the same catalog read, for callers that need both. */
  edgeTypes: EdgeTypeOut[] | null;
  /** Human-readable failure, or null after a successful read. */
  error: string | null;
  /** Whether the catalog request failed. */
  failed: boolean;
}

/** Fetch the node-type catalog once for a mounted caller. */
export function useNodeTypes(): NodeTypeList {
  const [types, setTypes] = useState<TypeOut[] | null>(null);
  const [edgeTypes, setEdgeTypes] = useState<EdgeTypeOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .getTypes(controller.signal)
      .then((catalog) => {
        setTypes(catalog.node_types);
        setEdgeTypes(catalog.edge_types);
        setError(null);
        setFailed(false);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setTypes([]);
        setEdgeTypes([]);
        setError(describeError(caught));
        setFailed(true);
      });
    return () => controller.abort();
  }, []);

  return { types, edgeTypes, error, failed };
}
