/**
 * TypeScript mirror of `nodum/models.py` — the pydantic I/O schema every nodum
 * surface serialises with `model_dump(mode="json")`.
 *
 * Rules used when mirroring:
 * - A pydantic field is always present in the dumped JSON, including the ones
 *   that carry a default, so every field below is required.
 * - `X | None` in Python becomes `X | null` here — never `?:`. Optionality in
 *   pydantic is about *input*, and these are output models.
 * - Field names are copied verbatim; do not camelCase them.
 *
 * Keep this file in lockstep with `nodum/models.py`. Mirrored against the
 * Phase-2 tree (migrations 0001–0008).
 */

/** Arbitrary JSON object, as `dict[str, Any]` dumps. */
export type JsonObject = Record<string, unknown>;

/** Lifecycle state shared by nodes and edges. */
export type NodeState = "proposed" | "active" | "archived";

/** `VersionOut.state`: an applied snapshot, a pending proposal, or a rejected one. */
export type VersionState = "applied" | "proposed" | "archived";

/** What a review-queue entry proposes. */
export type ProposalKind = "node" | "edge" | "update";

/** Image rendition profiles served by `nodum.assets` (design §5.7). */
export type RenditionProfile = "thumb" | "preview";

/** A graph node as emitted to clients. `type` is the type id. */
export interface NodeOut {
  id: string;
  graph_id: string;
  type: string;
  parent_id: string | null;
  position: number | null;
  title: string | null;
  content: string;
  props: JsonObject;
  state: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

/** A typed, directed edge. `type` is the edge-type id. */
export interface EdgeOut {
  id: string;
  graph_id: string;
  src_id: string;
  dst_id: string;
  type: string;
  props: JsonObject;
  confidence: number | null;
  created_by: string;
  state: string;
  valid_from: string | null;
  valid_to: string | null;
  created_at: string;
}

/**
 * One snapshot of a node's title/content/props after a mutation.
 *
 * `proposed_fields` names the fields a proposed update actually asked to
 * change — the only ones an accept writes back. It is null on snapshots that
 * are not proposals.
 */
export interface VersionOut {
  id: number;
  node_id: string;
  title: string | null;
  content: string;
  props: JsonObject;
  actor: string;
  event_seq: number;
  state: string;
  proposed_fields: string[] | null;
  created_at: string;
}

/** One append-only event-log entry. */
export interface EventOut {
  seq: number;
  actor: string;
  op: string;
  payload: JsonObject;
  cycle_id: string | null;
  created_at: string;
}

/** A node type (user-extensible class). */
export interface TypeOut {
  id: string;
  name: string;
  parent_type_id: string | null;
  json_schema: JsonObject;
  is_builtin: boolean;
}

/** An edge type, optionally naming its inverse. */
export interface EdgeTypeOut {
  id: string;
  name: string;
  inverse_name: string | null;
  json_schema: JsonObject;
  is_builtin: boolean;
}

/** The live type catalog: node types and edge types. */
export interface TypesOut {
  node_types: TypeOut[];
  edge_types: EdgeTypeOut[];
}

/**
 * The outcome of reversing one event.
 *
 * `restored` is the row state written back (null when the reversal deleted a
 * created row); `deleted` lists rows removed by a create reversal.
 */
export interface UndoResult {
  undone_seq: number;
  undone_op: string;
  restored: JsonObject | null;
  deleted: JsonObject[];
  undo_event_seq: number;
}

/** The outcome of `init`: where the DB lives and what was applied. */
export interface InitResult {
  db_path: string;
  applied: string[];
  already_applied: string[];
}

/** One projector's checkpoint state and backlog. */
export interface ProjectorStatus {
  name: string;
  last_event_seq: number;
  pending_events: number;
  rows: number;
  available: boolean;
  detail: string | null;
}

/** The outcome of running (or rebuilding) one projector. */
export interface ProjectorRun {
  name: string;
  applied: number;
  from_seq: number;
  to_seq: number;
  detail: string | null;
}

/**
 * One search result: a node plus its fused score and per-signal breakdown.
 *
 * `signals` carries each retrieval signal's contribution: RRF contributions
 * for `bm25` and `vector` (they sum to `score`), and the edge weight for
 * `graph` expansion hits.
 */
export interface SearchHit {
  node_id: string;
  type: string;
  title: string | null;
  snippet: string;
  score: number;
  signals: Record<string, number>;
}

/** A ranked result list for one query. */
export interface SearchResult {
  query: string;
  k: number;
  hits: SearchHit[];
}

/**
 * One agent's policy ruleset (design §8.3).
 *
 * `rules` is the stored JSON ruleset verbatim: rule objects keyed by `job` or
 * `edge_type`, each with an `action` and an optional `min_confidence` gate.
 */
export interface PolicyOut {
  agent: string;
  rules: PolicyRule[];
  updated_by: string;
  updated_at: string;
}

/**
 * One policy rule. Unknown extra keys are preserved by the service for forward
 * compatibility, hence the index signature.
 *
 * A `min_confidence` gate grades the agent's *self-reported* confidence, so it
 * is inert unless the same rule sets `trust_self_reported_confidence: true`.
 */
export interface PolicyRule {
  job?: string;
  edge_type?: string;
  action?: "auto_accept" | "auto_apply" | "always_propose" | string;
  min_confidence?: number;
  trust_self_reported_confidence?: boolean;
  [key: string]: unknown;
}

/**
 * One pending proposal in the review queue.
 *
 * `context` carries what a reviewer needs beyond the row itself — for an edge,
 * the source/target node ids and titles; for a node, its parent's id/title; for
 * an update, the current node's id/title/content.
 */
export interface ProposalOut {
  kind: string;
  id: string;
  type: string;
  created_by: string;
  created_at: string;
  node: NodeOut | null;
  edge: EdgeOut | null;
  version: VersionOut | null;
  context: JsonObject;
}

/** One id a batch transition could not process, with the reason. */
export interface TransitionFailure {
  id: string;
  error: string;
}

/**
 * The outcome of a batch accept/reject.
 *
 * `transitioned` lists the ids that moved state; `failed` lists ids skipped
 * because they were unknown or not in the required state — a batch never
 * aborts on a single bad id.
 */
export interface BatchTransitionOut {
  action: string;
  actor: string;
  reason: string | null;
  transitioned: string[];
  failed: TransitionFailure[];
}

/**
 * A rooted subgraph: the nodes and edges reached by a traversal.
 *
 * `nodes` always includes the root (first); `edges` are the active edges the
 * walk followed (empty at depth 0).
 *
 * `truncated` is true when a node cap stopped the walk before it ran out of
 * graph — say "showing 200 of more" rather than presenting a partial subgraph
 * as the whole neighbourhood. Only the capped read sets it; the uncapped walks
 * always report false.
 */
export interface SubgraphOut {
  root: string;
  depth: number;
  nodes: NodeOut[];
  edges: EdgeOut[];
  truncated: boolean;
}

/**
 * The shortest active-edge path between two nodes.
 *
 * When `found` is true, `edges[i]` connects `nodes[i]` to `nodes[i+1]` (in
 * either stored direction).
 */
export interface PathOut {
  found: boolean;
  hops: number;
  nodes: NodeOut[];
  edges: EdgeOut[];
}

/**
 * A unified diff between two versions of a node.
 *
 * `diff` is a difflib unified diff over a stable text rendering (title line,
 * props JSON, then content); `changed_fields` names the fields that differ.
 */
export interface DiffOut {
  node_id: string;
  a: VersionOut;
  b: VersionOut;
  changed_fields: string[];
  diff: string;
}

/** One item a batch create could not process, with the reason. */
export interface ItemFailure {
  index: number;
  error: string;
}

/** The outcome of a batch edge proposal. */
export interface ProposeEdgesOut {
  created: EdgeOut[];
  failed: ItemFailure[];
}

/**
 * A registered content-addressed binary asset (design §5.2).
 *
 * Metadata only — the bytes live in the `asset_blobs` table of the same
 * database file, keyed by the same sha256. `extracted_text` is null until the
 * Phase-4 ingestion pipeline fills it.
 */
export interface AssetOut {
  hash: string;
  mime: string;
  size_bytes: number;
  original_name: string | null;
  extracted_text: string | null;
  created_at: string;
}

/**
 * A derived image rendition of an asset (design §5.7).
 *
 * `cached` is false on the call that generated (or regenerated) the rendition
 * and true on cache hits. `data_base64` carries the WebP bytes only when the
 * caller asked for them (the MCP path) — the web UI uses `renditionUrl`.
 */
export interface RenditionOut {
  id: string;
  asset_hash: string;
  profile: string;
  mime: string;
  width: number;
  height: number;
  size_bytes: number;
  cached: boolean;
  data_base64: string | null;
}

/** The outcome of evicting stored renditions. */
export interface PurgeResult {
  purged: number;
  bytes_freed: number;
}

/* ------------------------------------------------------------------ */
/* Shapes the HTTP surface adds on top of models.py                     */
/* ------------------------------------------------------------------ */

/** `GET /healthz`. Shape is owned by the API slice; kept permissive on purpose. */
export interface HealthOut {
  status: string;
  version?: string;
  db_path?: string;
  [key: string]: unknown;
}

/**
 * One `[[wikilink]]` autocomplete candidate.
 *
 * Backed by the title-prefix `suggest_links(prefix)` service query. The endpoint
 * returns full `NodeOut` rows to keep envelope parity with `nodum suggest-links`,
 * so this is an alias, not a narrower wire shape.
 */
export type LinkSuggestion = NodeOut;

/** The list envelope every nodum surface uses: `{"<plural>": [...], "count": n}`. */
export type ListEnvelope<K extends string, T> = { [P in K]: T[] } & { count: number };

/** The error body every non-2xx response carries. */
export interface ApiErrorBody {
  error: {
    type: string;
    message: string;
  };
}

/* ------------------------------------------------------------------ */
/* Request bodies / filter bags                                         */
/* ------------------------------------------------------------------ */

/** Filters for `GET /api/nodes` (mirrors `service.list_nodes`). */
export interface NodeFilters {
  type?: string;
  state?: NodeState;
  parent_id?: string;
  limit?: number;
}

/** Body for `POST /api/nodes` (mirrors `service.create_node`; actor is server-side). */
export interface CreateNodeBody {
  type: string;
  title?: string | null;
  content?: string;
  parent_id?: string | null;
  props?: JsonObject;
}

/**
 * Body for `PATCH /api/nodes/{id}` (mirrors `service.update_node`).
 *
 * Only the keys present are changed — omitting a key is not the same as
 * sending null, which clears it.
 */
export interface UpdateNodeBody {
  title?: string | null;
  content?: string;
  props?: JsonObject;
}

/** Filters for `GET /api/edges` (mirrors `service.list_edges`). */
export interface EdgeFilters {
  node_id?: string;
  type?: string;
  state?: NodeState;
  limit?: number;
}

/** Body for `POST /api/edges` (mirrors `service.create_edge`). */
export interface CreateEdgeBody {
  src_id: string;
  dst_id: string;
  type: string;
  props?: JsonObject;
  confidence?: number | null;
}

/** Filters for `GET /api/search` (mirrors `search.search`). */
export interface SearchFilters {
  k?: number;
  /** Node-state filter; `"any"` searches every state. Defaults to `active`. */
  state?: NodeState | "any";
  type?: string;
  created_by?: string;
  created_after?: string;
  created_before?: string;
  /** Append one-hop active-edge neighbours of the fused hits (design §7). */
  expand?: boolean;
}

/**
 * Query for `GET /api/graph/subgraph` — the bounded, filtered neighbourhood the
 * graph slice adds server-side (plan slice 5).
 */
export interface SubgraphParams {
  root_id: string;
  depth?: number;
  edge_types?: string[];
  node_types?: string[];
  /**
   * Repeatable: an edge is traversable if its state is any of these. Defaults
   * server-side to `active` alone — proposed structure is inert, not hidden.
   */
  edge_state?: NodeState[];
  /**
   * Opt-in only. The server excludes edges whose `confidence` is NULL, and
   * human-created edges usually carry none — so setting this hides most of the
   * human graph. Never default it on.
   */
  min_confidence?: number;
  created_by?: string;
  /** Server-side node cap; the server may return fewer. */
  limit?: number;
}

/** Filters for `GET /api/review/queue` (mirrors `service.list_proposals`). */
export interface ReviewQueueFilters {
  created_by?: string;
  type?: string;
  kind?: ProposalKind;
  created_before?: string;
  created_after?: string;
  limit?: number;
}

/** Body for `POST /api/review/accept`. Never carries an actor — the server forces `human`. */
export interface AcceptProposalsBody {
  ids: string[];
}

/** Body for `POST /api/review/reject`. The reason is mandatory (design §8.1). */
export interface RejectProposalsBody {
  ids: string[];
  reason: string;
}

/** Body for `PUT /api/policies/{agent}`. */
export interface SetPolicyBody {
  rules: PolicyRule[];
}
