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
 * principals tree (migrations 0001–0011).
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
  space_id: string | null;
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
 *
 * `space_id` is where the node lives. A result list spans every space in scope
 * unless the filter narrowed it, so without this the busiest surface in the UI
 * cannot answer "which space is this in?" — see `views/search/resultSpace.ts`
 * for when a row renders it.
 */
export interface SearchHit {
  node_id: string;
  space_id: string | null;
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
 * `truncated` is true when a cap — on nodes **or** on edges — stopped the walk
 * before it ran out of graph. A filter removing nodes is not truncation.
 * Say "showing 200 of more" rather than presenting a partial subgraph as the
 * whole neighbourhood. Only the capped read sets it; the uncapped walks always
 * report false.
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
 * database file, keyed by the same sha256. `extracted_text` is what ingestion
 * pulled out of those bytes, and stays null when no handler claimed the type
 * or the handler produced nothing (a scanned PDF with no OCR installed, an
 * image with no words in it) — an asset with no text is still registered and
 * still described.
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

/**
 * What extraction got out of one asset, as reported to clients.
 *
 * The text itself is not echoed here — it lands on the asset's
 * `extracted_text` and, capped, as the `source` node's content, and an
 * envelope carrying a whole PDF's text would be unusable. `chars` and `pages`
 * are how a caller tells "nothing came out" from "a lot came out", and
 * `detail` says why when the answer is nothing: it is where *"install the
 * `pdf` extra"* and *"a scanned PDF needs OCR"* are already phrased, so a
 * readout that drops it drops the only actionable sentence in the response.
 */
export interface ExtractionOut {
  /** The handler that ran, or `"none"` when nothing claimed the MIME. */
  handler: string;
  chars: number;
  /** Pages the handler produced; 0 for a format that is not paginated. */
  pages: number;
  detail: string | null;
}

/**
 * The outcome of ingesting one file or URL (design §5.5–§5.7).
 *
 * Bytes in, reviewable subgraph out: the `asset` itself, an `asset_ref` node
 * describing it *in one space* (which is what makes the bytes reachable by
 * anyone but a human, and findable at all), a `source` node carrying the
 * extracted text, a `derived_from` edge between them, and one `block` child
 * per page that had text.
 *
 * `created` is false when the target space already had that describing node:
 * ingestion is idempotent per `(hash, space)`, so a re-run returns the
 * existing subgraph rather than duplicating it. **That flag is the server's
 * own account of *ingested* versus *already ingested*, and the only one a
 * client may use** — there is no client-side hash bookkeeping to be done here.
 *
 * `pages` are the per-page `block` children under `source`, and
 * `pages_truncated` says the document had more pages than the cap allowed —
 * never a silent truncation.
 */
export interface IngestOut {
  asset: AssetOut;
  asset_ref: NodeOut;
  source: NodeOut;
  pages: NodeOut[];
  pages_truncated: boolean;
  edges: EdgeOut[];
  extraction: ExtractionOut;
  created: boolean;
  event_seq: number;
}

/**
 * A short-lived, single-use capability URL (design §5.7 rule 4).
 *
 * `token` is shown once and never stored in the clear — the database keeps
 * only its sha256, exactly as it keeps an agent token.
 *
 * **`url` is not for this client.** It is absolute and built from
 * `NODUM_PUBLIC_URL`, which exists for a host that is *not* this browser and
 * may name another address entirely; the browser owns its own origin and
 * carries only the capability, so it redeems `token` against `/api/uploads/…`
 * here and never follows this field (design decision D5).
 */
export interface UrlGrantOut {
  /** `"download"` or `"upload"`. */
  kind: string;
  token: string;
  url: string;
  asset_hash: string | null;
  expires_at: string;
  /** The body ceiling the redemption enforces; null on a download grant. */
  max_bytes: number | null;
}

/**
 * The answer to `POST /api/uploads`: a grant, or an instant dedup hit.
 *
 * Exactly one side is populated, never both and never neither. `asset` with a
 * null `grant` is the dedup shortcut, and it is reachable **only** by
 * declaring a `sha256` the store already holds — which this client never does
 * (see {@link RequestUploadBody} and design decision D4). So the grantless
 * shape is one to handle honestly rather than a path anything here takes.
 */
export interface UploadGrantOut {
  grant: UrlGrantOut | null;
  asset: AssetOut | null;
}

/** A grant level, weakest to strongest (the server's `GRANT_LEVEL_NAMES`). */
export type GrantLevel = "read" | "suggest" | "edit";

/** A human account (identity + credentials + attribution, never a scope). */
export interface HumanOut {
  id: string;
  name: string;
  has_password: boolean;
  disabled: boolean;
  created_at: string;
}

/** An agent account. `has_token` is all anyone ever learns of the token. */
export interface AgentOut {
  id: string;
  kind: string;
  name: string;
  owner_human_id: string | null;
  has_token: boolean;
  disabled: boolean;
  created_at: string;
}

/** A new agent plus its token — the one and only time the token is shown. */
export interface AgentCreatedOut {
  agent: AgentOut;
  token: string;
}

/** One (agent, space) grant row. */
export interface GrantOut {
  agent_id: string;
  space_id: string;
  level: string;
  created_at: string;
}

/**
 * A space, as `GET /api/spaces` and `nodum space-list` render it.
 *
 * A space **is** a node — builtin type `space`, living in the meta space — so
 * every {@link NodeOut} field is here unchanged and anything that only wants
 * the node keeps reading it as one. The two additions are what make it
 * *territory* rather than a name:
 *
 * - `node_count` counts the space's **live** nodes, `active` plus `proposed`.
 *   A space holding nothing but an agent's proposals is not empty, so this is
 *   deliberately not a count of `active` alone.
 * - `grants` lists the agents holding a grant on the space. It is how a human
 *   sees delegated territory at a glance — and an `edit`-granted space governs
 *   itself, so it never reaches the review queue at all.
 */
export interface SpaceOut extends NodeOut {
  node_count: number;
  grants: GrantOut[];
}

/* ------------------------------------------------------------------ */
/* Consolidation cycles — the dream journal (design §8.4)               */
/* ------------------------------------------------------------------ */

/** How a cycle came to exist — `service.CYCLE_TRIGGERS`. */
export type CycleTrigger = "manual" | "scheduled" | "curative" | "rollback";

/** Where a cycle is in its life — `service.CYCLE_STATUSES`. */
export type CycleStatus = "running" | "completed" | "failed" | "rolled_back";

/**
 * One consolidation cycle — a dream-journal entry.
 *
 * The omission is the design's, not an oversight: there is **no diff here**.
 * What the cycle changed is `list_events(cycle_id=…)`, which
 * {@link CycleDetailOut} carries, so the journal can never become a second
 * record that disagrees with the append-only log.
 *
 * `triggered_by` is who *asked* — a `human:<id>`, or the literal `scheduler` —
 * and is deliberately not the actor on the events inside, which is who *acted*
 * (the gardener). `report` is null while the cycle is still running, as
 * `finished_at` is, and `rolled_back_by` names the rollback cycle that reversed
 * this one.
 *
 * `trigger` and `status` are typed as plain strings for the same reason every
 * other enum-shaped field here is: the server may add a value, and a union that
 * silently excludes it would make a real row unrepresentable.
 */
export interface CycleOut {
  id: string;
  trigger: string;
  triggered_by: string;
  scope: string | null;
  dry_run: boolean;
  status: string;
  report: JsonObject | null;
  started_at: string;
  finished_at: string | null;
  rolled_back_by: string | null;
}

/**
 * The coherence metrics, one snapshot per key: `{before: {...}, after: {...}}`.
 *
 * Keyed rather than fixed on purpose (`ConsolidationReport.metrics`): 5b's two
 * judgement-dependent metrics join the object when they can be computed, with
 * no migration and no change here.
 */
export type CycleMetrics = Record<string, Record<string, number>>;

/**
 * `GET /api/cycles/{id}` — one journal entry with the diff a reviewer reads it by.
 *
 * `events` is the append-only log narrowed to this cycle, newest first, and
 * `events_truncated` is true when the read hit its limit. It is deliberately
 * conservative — it says the list may be short, not that it provably is — so a
 * surface must say the list may be incomplete rather than that it certainly is.
 *
 * `metrics` is a projection of `cycle.report["metrics"]`, `{}` for a cycle whose
 * report carries none: a rollback, or a one-op curative cycle.
 */
export interface CycleDetailOut {
  cycle: CycleOut;
  metrics: CycleMetrics;
  events: EventOut[];
  events_truncated: boolean;
}

/**
 * `POST /api/cycles` — the closed cycle plus its report typed.
 *
 * `cycle.report` and `report` are the same data, so a caller that just ran a
 * cycle needs no second request to render it.
 */
export interface ConsolidationOut {
  cycle: CycleOut;
  report: JsonObject;
}

/** `POST /api/cycles` body. Both fields are the runner's own parameters. */
export interface RunCycleBody {
  /** Confine the cycle to one space, by id or name; absent is the whole file. */
  scope?: string;
  /**
   * Rehearse it: every job computed, the report written, **no graph event
   * emitted**. A real boolean — the server refuses the string `"false"` rather
   * than coercing it, which is the right posture for a rehearsal flag.
   */
  dry_run?: boolean;
}

/**
 * One row standing between a cycle and its rollback (decision C4).
 *
 * Rollback refuses rather than clobbers, so the refusal names both ends of the
 * collision: the cycle's own event, and the event that moved the row since.
 * `kind` is `node` or `edge`; `conflicting_cycle_id` is set when the later work
 * was itself a cycle's — still outside this cycle, and still a conflict.
 */
export interface RollbackConflictOut {
  kind: string;
  row_id: string;
  cycle_event_seq: number;
  cycle_event_op: string;
  conflicting_seq: number;
  conflicting_op: string;
  conflicting_actor: string;
  conflicting_cycle_id: string | null;
}

/**
 * `POST /api/cycles/{id}/rollback` — the outcome, or on a dry run the verdict.
 *
 * `rollback_cycle_id` names the new `trigger='rollback'` cycle every reversal
 * event is stamped with, and is null on a dry run, which opens no cycle and
 * writes nothing. `skipped_events` are the cycle's non-graph events — audit
 * records with no graph effect to reverse.
 *
 * `conflicts` is empty on a rollback that happened; on a dry run it is the
 * reason it would not. A **real** rollback that meets one refuses with 409 and
 * the same list in the error body — see `RollbackConflictError`.
 */
export interface RollbackOut {
  cycle_id: string;
  rollback_cycle_id: string | null;
  dry_run: boolean;
  reversed_events: number[];
  skipped_events: number[];
  restored_nodes: string[];
  restored_edges: string[];
  deleted_nodes: string[];
  deleted_edges: string[];
  redirects_removed: string[];
  conflicts: RollbackConflictOut[];
}

/** `POST /api/cycles/{id}/rollback` body. */
export interface RollbackCycleBody {
  /**
   * Compute the plan and return it without writing anything — the "would this
   * succeed?" a confirm dialog needs, which answers **200** with the conflicts
   * in `conflicts` instead of raising.
   */
  dry_run?: boolean;
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

/** `POST /api/login` — the session's human id; the cookie does the rest. */
export interface LoginOut {
  human: string;
}

/** `POST /api/agents/{id}/token-rotate` — the replacement token, shown once. */
export interface RotatedTokenOut {
  agent_id: string;
  token: string;
}

/** `POST /api/agents/{id}/disable|enable` — the new disabled flag. */
export interface AgentStateOut {
  ok: boolean;
  agent_id: string;
  disabled: boolean;
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

/**
 * The error body every non-2xx response carries.
 *
 * `conflicts` is the one failure on this surface whose body carries more than
 * `type` and `message`: a refused rollback names the rows in the way, because a
 * human told which four rows are blocking it can act and one told "rollback
 * failed" cannot. Every other failure omits the key.
 */
export interface ApiErrorBody {
  error: {
    type: string;
    message: string;
    conflicts?: RollbackConflictOut[];
  };
}

/* ------------------------------------------------------------------ */
/* Request bodies / filter bags                                         */
/* ------------------------------------------------------------------ */

/**
 * The two read-side space controls, shared by `GET /api/nodes` and
 * `GET /api/search` (the CLI's `--space` / `--include-meta`).
 *
 * They are a **filter**, not a mode: omitting `space` reads every space the
 * principal can, and the filter only ever narrows. The write target is a
 * separate, independent control (`CreateNodeBody.space`) — reading `research`
 * while still filing into `main` is the ordinary case.
 */
export interface SpaceReadControls {
  /**
   * Narrow to one space, by id **or** name. Omitted reads every space in scope.
   *
   * A space that does not exist and a space the principal holds no grant on
   * are refused identically and deliberately — see `UnknownSpaceError` in
   * `api/client.ts`, which is what a caller branches on.
   */
  space?: string;
  /**
   * Include the meta space (types, spaces, conventions). Off server-side by
   * default. Naming `space: "meta"` **is itself the opt-in** — the default
   * exclusion applies only to an unnarrowed read, so a `meta` filter returning
   * nothing would be a trap rather than a rule.
   */
  include_meta?: boolean;
}

/** Filters for `GET /api/nodes` (mirrors `service.list_nodes`). */
export interface NodeFilters extends SpaceReadControls {
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
  /**
   * The **write target**: which space the node lands in, by id or name.
   * Omitted lands it in `main`.
   *
   * A space says *where a node goes*, never *who wrote it* — the session's
   * human is still the only writer this surface has.
   */
  space?: string;
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
export interface SearchFilters extends SpaceReadControls {
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

/**
 * Body for `POST /api/grants` (mirrors `service.grant`).
 *
 * `space` accepts a space id or title — the picker sends the id.
 */
export interface SetGrantBody {
  agent: string;
  space: string;
  level: GrantLevel;
}

/** Body for `POST /api/grants/revoke` (mirrors `service.revoke`). */
export interface RevokeGrantBody {
  agent: string;
  space: string;
}

/**
 * Body for `POST /api/uploads` (mirrors `urls.mint_upload`).
 *
 * `size` is the ceiling the redemption then enforces on the body *as it
 * streams*, so it is the file's real length rather than an estimate. The
 * service refuses one above `urls.MAX_UPLOAD_BYTES` at mint time — before any
 * bytes cross the network — as it refuses a `space` the session cannot write.
 *
 * **There is deliberately no `sha256` field.** A declared hash the store
 * already holds is answered with the existing asset and *no grant*, and that
 * shortcut proves the **bytes** exist rather than that anything *describes*
 * them (design decision D4). A file registered earlier through the editor's
 * drop is exactly the undescribed case, so declaring the hash would silently
 * skip the ingestion the human asked for. The client has no way to express
 * one — the same shape as its having no way to express an identity.
 */
export interface RequestUploadBody {
  /** The name the bytes will be stored and titled under. */
  name: string;
  /** The declared content type. Advisory: the server stores what it sniffs. */
  mime: string;
  size: number;
  /**
   * The **write target**: which space the describing nodes land in, by id or
   * name. Omitted lands them in `main`.
   */
  space?: string;
}
