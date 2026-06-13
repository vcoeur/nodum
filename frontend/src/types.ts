// Mirrors the JSON the nodum API emits: the metamodel contract (`GET /schema`)
// and the model envelopes shared by the CLI and API.

export type FieldType = "str" | "int" | "float" | "bool" | "enum" | "list[str]";

export interface FieldSpec {
  type: FieldType;
  required: boolean;
  choices?: string[];
  description?: string;
}

export interface NodeKind {
  name: string;
  group: string;
  text_label: string;
  fields: Record<string, FieldSpec>;
}

export interface EdgeKind {
  name: string;
  from: string[];
  to: string[];
  symmetric: boolean;
  fields: Record<string, FieldSpec>;
}

export interface Schema {
  node_kinds: NodeKind[];
  edge_kinds: EdgeKind[];
}

export type NodeData = Record<string, unknown> & { text?: string };

export interface NodeOut {
  uuid: string;
  kind: string;
  data: NodeData;
  created_at: string;
  updated_at: string;
}

export interface EdgeOut {
  uuid: string;
  kind: string;
  from_uuid: string;
  to_uuid: string;
  data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface SearchHit extends NodeOut {
  score: number;
}

export interface SearchResult {
  query: string;
  total: number;
  hits: SearchHit[];
}

export interface NodeWithEdges {
  node: NodeOut;
  edges: EdgeOut[];
}

export interface Subgraph {
  seed: string[];
  depth: number;
  nodes: NodeOut[];
  edges: EdgeOut[];
}

export interface Deleted {
  uuid: string;
  deleted: number;
}

export interface SessionInfo {
  configured: boolean;
  authenticated: boolean;
}
