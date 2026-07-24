/**
 * The single HTTP client for the nodum API.
 *
 * Every view talks to the backend through this module — no view issues its own
 * `fetch`. The server is same-origin (the Python process serves the built
 * bundle at `/`), so requests are relative and there is no CORS anywhere.
 *
 * Conventions this client encodes, taken from the CLI contract in `AGENTS.md`:
 * - list responses are wrapped as `{"<plural>": [...], "count": n}` and are
 *   unwrapped here, so callers get a plain array;
 * - errors are `{"error": {"type", "message"}}` with a non-2xx status and are
 *   raised as {@link ApiError};
 * - writes never carry an actor. The HTTP surface *is* the human surface and
 *   forces `actor="human"` server-side; a client-supplied actor would be
 *   ignored, so this client never sends one.
 *
 * The endpoints themselves land with the API slice; this file is the contract
 * the view slices code against.
 */

import type {
  AcceptProposalsBody,
  ApiErrorBody,
  AssetOut,
  BatchTransitionOut,
  CreateEdgeBody,
  CreateNodeBody,
  DiffOut,
  EdgeFilters,
  EdgeOut,
  EdgeTypeOut,
  EventOut,
  HealthOut,
  JsonObject,
  NodeFilters,
  NodeOut,
  PathOut,
  PolicyOut,
  ProposalOut,
  RejectProposalsBody,
  RenditionProfile,
  ReviewQueueFilters,
  SearchFilters,
  SearchResult,
  SetPolicyBody,
  SubgraphOut,
  SubgraphParams,
  TypeOut,
  TypesOut,
  UndoResult,
  UpdateNodeBody,
  VersionOut,
} from "./types";

/** Prefix every API route carries. `/healthz` sits outside it, by design. */
export const API_BASE = "/api";

/** A non-2xx response, carrying the server's error taxonomy verbatim. */
export class ApiError extends Error {
  /** HTTP status code. */
  readonly status: number;
  /** The server's exception class name, e.g. `NodeNotFound`. */
  readonly type: string;

  constructor(status: number, type: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
  }

  /** True for a 404 — the id did not resolve. */
  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** True for a 403 — a human-only operation was refused. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** True for a 503 — the single SQLite writer is busy; the call is retryable. */
  get isRetryable(): boolean {
    return this.status === 503;
  }
}

/** Optional bearer token, for the LAN case (`nodum serve --token`). */
let authToken: string | null = null;

/**
 * Set (or clear) the bearer token sent with every request.
 *
 * @param token The token, or null to send no `Authorization` header.
 */
export function setAuthToken(token: string | null): void {
  authToken = token;
}

/**
 * Build a query string, dropping undefined/null and repeating array values.
 *
 * Takes a plain object rather than a `Record`, so the typed filter interfaces
 * can be passed straight in without a cast.
 *
 * @param params Parameter bag; omitted keys never reach the server, so the
 *   server's own defaults apply.
 * @returns `"?a=1&b=2"`, or `""` when nothing is set.
 */
function query(params: object | undefined): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params) as [string, unknown][]) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, String(item));
    } else {
      search.append(key, String(value));
    }
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

/**
 * Read the error body of a failed response, tolerating a non-JSON body.
 *
 * A proxy or a crash can return HTML or nothing at all; the status is then the
 * only signal we have, and it still has to become an ApiError.
 */
async function toApiError(response: Response): Promise<ApiError> {
  let type = "HTTPError";
  let message = `${response.status} ${response.statusText}`.trim();
  try {
    const body = (await response.json()) as Partial<ApiErrorBody>;
    if (body && typeof body === "object" && body.error) {
      type = body.error.type ?? type;
      message = body.error.message ?? message;
    }
  } catch {
    // Body was not JSON — keep the status-derived message.
  }
  return new ApiError(response.status, type, message);
}

/** Options for {@link rawRequest}. */
interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  /** JSON-serialised into the body. Mutually exclusive with `form`. */
  body?: unknown;
  /** Sent as-is; the browser sets the multipart boundary. */
  form?: FormData;
  signal?: AbortSignal;
}

/**
 * Issue a request against an absolute site path and parse the JSON response.
 *
 * @param path Site-absolute path, e.g. `/healthz`.
 * @throws ApiError On any non-2xx response.
 */
async function rawRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;

  let body: BodyInit | undefined;
  if (options.form) {
    body = options.form;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  const init: RequestInit = { method: options.method ?? "GET", headers };
  if (body !== undefined) init.body = body;
  if (options.signal) init.signal = options.signal;

  const response = await fetch(path, init);
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Issue a request against an API route (prefixed with `/api`).
 *
 * @param path Route below `/api`, e.g. `/nodes`.
 * @throws ApiError On any non-2xx response.
 */
export function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return rawRequest<T>(`${API_BASE}${path}`, options);
}

/**
 * Issue a request for a list route and unwrap the `{"<plural>": [...], "count": n}`
 * envelope every nodum surface uses.
 *
 * @param key The plural key the server wraps the array under.
 */
async function requestList<T>(
  key: string,
  path: string,
  options: RequestOptions = {},
): Promise<T[]> {
  const payload = await request<Record<string, unknown>>(path, options);
  const items = payload[key];
  if (!Array.isArray(items)) {
    throw new ApiError(
      500,
      "MalformedListEnvelope",
      `expected a "${key}" array in the response for ${path}`,
    );
  }
  return items as T[];
}

/* ------------------------------------------------------------------ */
/* Health + catalog                                                     */
/* ------------------------------------------------------------------ */

/** `GET /healthz` — liveness probe. Sits outside `/api` on purpose. */
export function getHealth(signal?: AbortSignal): Promise<HealthOut> {
  return rawRequest<HealthOut>("/healthz", signal ? { signal } : {});
}

/** `GET /api/types` — the live node-type and edge-type catalog. */
export function getTypes(signal?: AbortSignal): Promise<TypesOut> {
  return request<TypesOut>("/types", signal ? { signal } : {});
}

/**
 * `GET /api/schema/{type}` — one type's JSON schema.
 *
 * Resolves a node type or an edge type; the server picks by id/name, so the
 * result is the union.
 */
export function getSchema(type: string, signal?: AbortSignal): Promise<TypeOut | EdgeTypeOut> {
  return request<TypeOut | EdgeTypeOut>(
    `/schema/${encodeURIComponent(type)}`,
    signal ? { signal } : {},
  );
}

/* ------------------------------------------------------------------ */
/* Nodes                                                                */
/* ------------------------------------------------------------------ */

/** `GET /api/nodes` — list nodes, optionally filtered by type/state/parent. */
export function listNodes(filters?: NodeFilters, signal?: AbortSignal): Promise<NodeOut[]> {
  return requestList<NodeOut>(
    "nodes",
    `/nodes${query(filters)}`,
    signal ? { signal } : {},
  );
}

/** `POST /api/nodes` — create a node. The server attributes it to `human`. */
export function createNode(body: CreateNodeBody, signal?: AbortSignal): Promise<NodeOut> {
  return request<NodeOut>("/nodes", { method: "POST", body, ...(signal ? { signal } : {}) });
}

/**
 * `GET /api/nodes/{id}` — one node, or its neighbourhood when `depth` is given.
 *
 * Depth 0 and above returns a {@link SubgraphOut} (the node plus the active
 * edges reached); omitting `depth` returns the bare node.
 */
export function getNode(id: string, signal?: AbortSignal): Promise<NodeOut>;
export function getNode(
  id: string,
  options: { depth: number },
  signal?: AbortSignal,
): Promise<SubgraphOut>;
export function getNode(
  id: string,
  optionsOrSignal?: { depth: number } | AbortSignal,
  maybeSignal?: AbortSignal,
): Promise<NodeOut | SubgraphOut> {
  const hasOptions = optionsOrSignal !== undefined && !(optionsOrSignal instanceof AbortSignal);
  const depth = hasOptions ? (optionsOrSignal as { depth: number }).depth : undefined;
  const signal = hasOptions ? maybeSignal : (optionsOrSignal as AbortSignal | undefined);
  return request<NodeOut | SubgraphOut>(
    `/nodes/${encodeURIComponent(id)}${query({ depth })}`,
    signal ? { signal } : {},
  );
}

/**
 * `PATCH /api/nodes/{id}` — update the named fields only.
 *
 * Returns the updated node: the HTTP surface writes as `human`, so the edit
 * always applies in place rather than staging a proposed version.
 */
export function updateNode(
  id: string,
  body: UpdateNodeBody,
  signal?: AbortSignal,
): Promise<NodeOut> {
  return request<NodeOut>(`/nodes/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
    ...(signal ? { signal } : {}),
  });
}

/** `GET /api/nodes/{id}/children` — children in `position` order. */
export function listChildren(id: string, signal?: AbortSignal): Promise<NodeOut[]> {
  return requestList<NodeOut>(
    "nodes",
    `/nodes/${encodeURIComponent(id)}/children`,
    signal ? { signal } : {},
  );
}

/** `GET /api/nodes/{id}/history` — the node's version snapshots, chronological. */
export function getHistory(id: string, signal?: AbortSignal): Promise<VersionOut[]> {
  return requestList<VersionOut>(
    "versions",
    `/nodes/${encodeURIComponent(id)}/history`,
    signal ? { signal } : {},
  );
}

/** `POST /api/nodes/{id}/archive` — retire a node. Human-only, server-enforced. */
export function archiveNode(id: string, signal?: AbortSignal): Promise<NodeOut> {
  return request<NodeOut>(`/nodes/${encodeURIComponent(id)}/archive`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/* ------------------------------------------------------------------ */
/* Edges                                                                */
/* ------------------------------------------------------------------ */

/** `GET /api/edges` — list edges, optionally filtered by incident node/type/state. */
export function listEdges(filters?: EdgeFilters, signal?: AbortSignal): Promise<EdgeOut[]> {
  return requestList<EdgeOut>(
    "edges",
    `/edges${query(filters)}`,
    signal ? { signal } : {},
  );
}

/** `POST /api/edges` — create a typed, directed edge. */
export function createEdge(body: CreateEdgeBody, signal?: AbortSignal): Promise<EdgeOut> {
  return request<EdgeOut>("/edges", { method: "POST", body, ...(signal ? { signal } : {}) });
}

/* ------------------------------------------------------------------ */
/* Search                                                               */
/* ------------------------------------------------------------------ */

/**
 * `GET /api/search` — hybrid BM25 + vector search, RRF-fused.
 *
 * Each hit carries a `signals` breakdown naming the contributing signals; the
 * vector signal is silently absent when no embedding provider is configured.
 */
export function search(
  q: string,
  filters?: SearchFilters,
  signal?: AbortSignal,
): Promise<SearchResult> {
  return request<SearchResult>(
    `/search${query({ q, ...filters })}`,
    signal ? { signal } : {},
  );
}

/**
 * `GET /api/links/suggest` — title-prefix candidates for `[[` autocomplete.
 *
 * Returns full `NodeOut` rows under the CLI's `nodes` key: the endpoint keeps
 * envelope parity with `nodum suggest-links` rather than inventing a narrower
 * shape, and a `NodeOut` is a superset of what autocomplete needs.
 */
export function suggestLinks(
  prefix: string,
  limit?: number,
  signal?: AbortSignal,
): Promise<NodeOut[]> {
  return requestList<NodeOut>(
    "nodes",
    `/links/suggest${query({ prefix, limit })}`,
    signal ? { signal } : {},
  );
}

/* ------------------------------------------------------------------ */
/* Graph                                                                */
/* ------------------------------------------------------------------ */

/**
 * `GET /api/graph/subgraph` — a bounded, filtered neighbourhood.
 *
 * Node-capped server-side: the graph view must never be handed an unbounded
 * result set.
 */
export function getSubgraph(params: SubgraphParams, signal?: AbortSignal): Promise<SubgraphOut> {
  return request<SubgraphOut>(
    `/graph/subgraph${query(params)}`,
    signal ? { signal } : {},
  );
}

/** `GET /api/graph/path` — the shortest active-edge path between two nodes. */
export function findPath(a: string, b: string, signal?: AbortSignal): Promise<PathOut> {
  return request<PathOut>(`/graph/path${query({ a, b })}`, signal ? { signal } : {});
}

/* ------------------------------------------------------------------ */
/* Review + policy (human tier)                                         */
/* ------------------------------------------------------------------ */

/** `GET /api/review/queue` — pending proposals with reviewer context, oldest first. */
export function getReviewQueue(
  filters?: ReviewQueueFilters,
  signal?: AbortSignal,
): Promise<ProposalOut[]> {
  return requestList<ProposalOut>(
    "proposals",
    `/review/queue${query(filters)}`,
    signal ? { signal } : {},
  );
}

/**
 * `POST /api/review/accept` — accept proposals by id, one event each.
 *
 * Ids that are unknown or no longer `proposed` come back in `failed`; the batch
 * never aborts on a single bad id.
 */
export function acceptProposals(
  body: AcceptProposalsBody,
  signal?: AbortSignal,
): Promise<BatchTransitionOut> {
  return request<BatchTransitionOut>("/review/accept", {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
}

/** `POST /api/review/reject` — reject proposals by id. The reason is mandatory. */
export function rejectProposals(
  body: RejectProposalsBody,
  signal?: AbortSignal,
): Promise<BatchTransitionOut> {
  return request<BatchTransitionOut>("/review/reject", {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
}

/** `GET /api/diff` — unified diff between two version snapshots. */
export function diffVersions(a: number, b: number, signal?: AbortSignal): Promise<DiffOut> {
  return request<DiffOut>(`/diff${query({ a, b })}`, signal ? { signal } : {});
}

/** `GET /api/policies` — every stored agent policy. */
export function listPolicies(signal?: AbortSignal): Promise<PolicyOut[]> {
  return requestList<PolicyOut>("policies", "/policies", signal ? { signal } : {});
}

/** `GET /api/policies/{agent}` — one agent's ruleset. */
export function getPolicy(agent: string, signal?: AbortSignal): Promise<PolicyOut> {
  return request<PolicyOut>(`/policies/${encodeURIComponent(agent)}`, signal ? { signal } : {});
}

/**
 * `PUT /api/policies/{agent}` — replace one agent's ruleset.
 *
 * Human-only: a policy grants auto-accept, so an agent setting one would
 * self-grant the live write the human tier withholds.
 */
export function setPolicy(
  agent: string,
  body: SetPolicyBody,
  signal?: AbortSignal,
): Promise<PolicyOut> {
  return request<PolicyOut>(`/policies/${encodeURIComponent(agent)}`, {
    method: "PUT",
    body,
    ...(signal ? { signal } : {}),
  });
}

/* ------------------------------------------------------------------ */
/* Assets                                                               */
/* ------------------------------------------------------------------ */

/**
 * `POST /api/assets` — register a binary asset (multipart).
 *
 * Registration is idempotent sha256 dedup, so re-uploading the same bytes
 * returns the existing asset.
 */
export function uploadAsset(file: File, signal?: AbortSignal): Promise<AssetOut> {
  const form = new FormData();
  form.append("file", file, file.name);
  return request<AssetOut>("/assets", { method: "POST", form, ...(signal ? { signal } : {}) });
}

/** `GET /api/assets` — registered assets, metadata only. */
export function listAssets(signal?: AbortSignal): Promise<AssetOut[]> {
  return requestList<AssetOut>("assets", "/assets", signal ? { signal } : {});
}

/** `GET /api/assets/{id}` — one asset's metadata, by hash or by node id. */
export function getAsset(id: string, signal?: AbortSignal): Promise<AssetOut> {
  return request<AssetOut>(`/assets/${encodeURIComponent(id)}`, signal ? { signal } : {});
}

/**
 * The URL of an asset's image rendition — for an `<img src>`, not a fetch.
 *
 * The bytes are served as WebP straight from the database; renditions are
 * generated lazily on first request, so the first load of a profile is slower.
 *
 * @param id Asset hash, or the id of a node carrying an `asset_hash` prop.
 * @param profile `thumb` (≤256px) or `preview` (≤1024px).
 */
export function renditionUrl(id: string, profile: RenditionProfile = "preview"): string {
  return `${API_BASE}/assets/${encodeURIComponent(id)}/rendition/${encodeURIComponent(profile)}`;
}

/* ------------------------------------------------------------------ */
/* Event log + export                                                   */
/* ------------------------------------------------------------------ */

/** `GET /api/events` — the append-only event log, most recent first. */
export function listEvents(limit?: number, signal?: AbortSignal): Promise<EventOut[]> {
  return requestList<EventOut>("events", `/events${query({ limit })}`, signal ? { signal } : {});
}

/**
 * `POST /api/undo` — reverse one event (default: the latest reversible one).
 *
 * Human-only: restoring an event's payload can write `state = 'active'` back.
 */
export function undo(seq?: number, signal?: AbortSignal): Promise<UndoResult> {
  return request<UndoResult>("/undo", {
    method: "POST",
    body: seq === undefined ? {} : { seq },
    ...(signal ? { signal } : {}),
  });
}

/**
 * `GET /api/export/node/{id}` — thin read-only JSON export of a node or its
 * subgraph.
 *
 * Deliberately untyped beyond "JSON object": the full Markdown Mirror export is
 * Phase 6, and pinning a shape here would freeze the wrong one.
 */
export function exportNode(
  id: string,
  options?: { depth?: number },
  signal?: AbortSignal,
): Promise<JsonObject> {
  return request<JsonObject>(
    `/export/node/${encodeURIComponent(id)}${query({ depth: options?.depth })}`,
    signal ? { signal } : {},
  );
}

/** Every endpoint, grouped for `import { api } from "../api/client"` ergonomics. */
export const api = {
  getHealth,
  getTypes,
  getSchema,
  listNodes,
  createNode,
  getNode,
  updateNode,
  listChildren,
  getHistory,
  archiveNode,
  listEdges,
  createEdge,
  search,
  suggestLinks,
  getSubgraph,
  findPath,
  getReviewQueue,
  acceptProposals,
  rejectProposals,
  diffVersions,
  listPolicies,
  getPolicy,
  setPolicy,
  uploadAsset,
  listAssets,
  getAsset,
  renditionUrl,
  listEvents,
  undo,
  exportNode,
};
