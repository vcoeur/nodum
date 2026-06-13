// React context exposing the metamodel (`GET /schema`) plus indexed lookups, so
// any component can resolve a kind's fields, an edge's signature, or a node
// kind's display group without re-fetching.

import { createContext, useContext } from "react";
import type { EdgeKind, NodeKind, Schema } from "./types";

export interface SchemaIndex {
  schema: Schema;
  nodeKind: (name: string) => NodeKind | undefined;
  edgeKind: (name: string) => EdgeKind | undefined;
  groupOf: (kindName: string) => string | undefined;
  /** Re-fetch `GET /schema` so callers reflect a runtime schema change. */
  reload: () => Promise<void>;
}

/** Build the indexed lookups from a freshly fetched schema (the parent supplies `reload`). */
export function indexSchema(schema: Schema): Omit<SchemaIndex, "reload"> {
  const nodeKinds = new Map(schema.node_kinds.map((nk) => [nk.name, nk]));
  const edgeKinds = new Map(schema.edge_kinds.map((ek) => [ek.name, ek]));
  return {
    schema,
    nodeKind: (name) => nodeKinds.get(name),
    edgeKind: (name) => edgeKinds.get(name),
    groupOf: (kindName) => nodeKinds.get(kindName)?.group,
  };
}

export const SchemaContext = createContext<SchemaIndex | null>(null);

/** Access the schema index; throws if used outside the provider. */
export function useSchema(): SchemaIndex {
  const context = useContext(SchemaContext);
  if (!context) throw new Error("useSchema must be used within a SchemaContext provider");
  return context;
}
