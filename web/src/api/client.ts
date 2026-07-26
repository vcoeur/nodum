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
 *   binds the session's principal server-side; a client-supplied identity
 *   would be ignored, so this client never sends one;
 * - every write carries `Content-Type: application/json` (or multipart, for an
 *   upload), because the server refuses anything else — see {@link rawRequest};
 * - auth is the session cookie, which the browser attaches to every same-origin
 *   request on its own — there is no token in this file. A 401 from any route
 *   but login means the session is gone and is reported through
 *   {@link reportUnauthorized} for the app shell to turn into a redirect.
 *
 * The endpoints themselves land with the API slice; this file is the contract
 * the view slices code against.
 */

import type {
  AcceptProposalsBody,
  AgentCreatedOut,
  AgentOut,
  AgentStateOut,
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
  GrantOut,
  HealthOut,
  HumanOut,
  JsonObject,
  LoginOut,
  NodeFilters,
  NodeOut,
  PathOut,
  ProposalOut,
  RejectProposalsBody,
  RenditionProfile,
  ReviewQueueFilters,
  RevokeGrantBody,
  RotatedTokenOut,
  SearchFilters,
  SearchResult,
  SetGrantBody,
  SpaceOut,
  SubgraphOut,
  SubgraphParams,
  TypeOut,
  TypesOut,
  UndoResult,
  UpdateNodeBody,
  VersionOut,
} from "./types";
import { reportUnauthorized } from "../lib/session";

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

/**
 * A space filter the server would not resolve — the one failure both
 * space-filtered reads collapse to.
 *
 * The wire is inconsistent, by accretion rather than by design. `GET /api/nodes`
 * resolves the filter through `nodum.service`, which raises `TypeNotFound` →
 * **404**; `GET /api/search` resolves it inside `nodum.search`, which raises a
 * bare `ValueError` → **400**, because a domain module does not import the
 * service's exception vocabulary. The split is pre-existing (the `type` filter
 * behaves identically) and inverting the layering to fix it is not worth it —
 * so it is absorbed here instead, once, and no view has to know which endpoint
 * it happened to ask.
 *
 * The normalised `status` is 404 so that `describeFailure` gives *one* answer
 * for the same user-visible event; `wireStatus` keeps what the server actually
 * said, for anyone debugging the round trip.
 *
 * **This never means "no such space".** The server answers a space that does
 * not exist and a space the principal holds no grant on with the same words on
 * purpose — a refusal that leaked the difference would be an existence oracle
 * over the whole file. Copy built on this error must not claim the space is
 * missing.
 *
 * Every call in this file that names a space raises it — the two filtered
 * reads, the write target on `POST /api/nodes`, and all three lifecycle
 * routes. That is deliberate and load-bearing: {@link isUnknownSpace} is the
 * **only** sanctioned discriminator, so a view must never re-test the message
 * itself. Two copies of one discriminator is how the two drift apart.
 */
export class UnknownSpaceError extends ApiError {
  /** The space id or name the caller asked for. */
  readonly space: string;
  /** What the endpoint really answered: 404 from the listing, 400 from search. */
  readonly wireStatus: number;

  constructor(space: string, wireStatus: number, message: string) {
    super(404, "UnknownSpace", message);
    this.name = "UnknownSpaceError";
    this.space = space;
    this.wireStatus = wireStatus;
  }
}

/**
 * Whether a caught value is the space filter being refused.
 *
 * @param error The caught value.
 */
export function isUnknownSpace(error: unknown): error is UnknownSpaceError {
  return error instanceof UnknownSpaceError;
}

/** Every space-resolving route raises this literal text; the status cannot discriminate. */
const UNKNOWN_SPACE_MESSAGE = /^unknown space:/i;

/**
 * The space a space itself lives in (`service.META_SPACE_ID`).
 *
 * `POST /api/spaces` names no space of its own — it is `create_node(type=
 * "space", space="meta")` underneath — so `meta` is the reference an unknown-space
 * refusal from a create would be about, and the one this client reports.
 */
const SPACE_HOME = "meta";

/**
 * Recognise an unknown-space refusal and re-shape it; pass anything else through.
 *
 * Keyed on the message rather than the status, because no status is specific
 * enough on its own: a 404 from `/api/nodes` is equally an unknown `type`
 * filter, a 400 from `/api/search` is any bad parameter at all, and a 404 from
 * `POST /api/nodes` is equally an unknown node *type*.
 *
 * Applied to **every** call that names a space — the two filtered reads, the
 * write target, and the three lifecycle routes — so that
 * {@link isUnknownSpace} is a complete answer and no view has to keep a second
 * copy of this test.
 *
 * @param error The caught value.
 * @param space The space the call asked for, if it asked for one.
 */
function asUnknownSpace(error: unknown, space: string | undefined): unknown {
  if (!space) return error;
  if (!(error instanceof ApiError)) return error;
  if (error.status !== 404 && error.status !== 400) return error;
  if (!UNKNOWN_SPACE_MESSAGE.test(error.message)) return error;
  return new UnknownSpaceError(space, error.status, error.message);
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

  const method = options.method ?? "GET";
  let body: BodyInit | undefined;
  if (options.form) {
    // The browser sets `multipart/form-data` with its own boundary; setting it
    // here would send a boundary-less header the server cannot parse.
    body = options.form;
  } else if (method !== "GET") {
    // Every state-changing JSON route requires `Content-Type: application/json`
    // — including the ones that take no body (`POST /nodes/{id}/archive`). That
    // is deliberate on the server: `application/json` is not a CORS-simple
    // content type, so a cross-origin form cannot forge one of these requests.
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body ?? {});
  }

  const init: RequestInit = { method, headers };
  if (body !== undefined) init.body = body;
  if (options.signal) init.signal = options.signal;

  // The session cookie rides along by default (`credentials: "same-origin"` is
  // the fetch default and the app is same-origin — the dev proxy included), so
  // there is nothing to set here. What this client must do about auth is react
  // to its absence: a 401 from any route but login means the session is gone,
  // and the one correct reaction is the login view, which only the shell can
  // navigate to. Login itself is exempt — a 401 there is a wrong password,
  // which the login form renders.
  const response = await fetch(path, init);
  if (response.status === 401 && path !== `${API_BASE}/login`) reportUnauthorized();
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
/* Session + accounts                                                   */
/* ------------------------------------------------------------------ */

/**
 * `POST /api/login` — password login, the one route outside the session gate.
 *
 * The response body only names the human; the credential is the `HttpOnly`
 * cookie the server sets, which this app never reads. A wrong name or password
 * is a 401 — and deliberately indistinguishable between the two.
 */
export function login(name: string, password: string, signal?: AbortSignal): Promise<LoginOut> {
  return request<LoginOut>("/login", {
    method: "POST",
    body: { name, password },
    ...(signal ? { signal } : {}),
  });
}

/** `POST /api/logout` — drop the server-side session row and clear the cookie. */
export function logout(signal?: AbortSignal): Promise<{ status: string }> {
  return request<{ status: string }>("/logout", {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/** `GET /api/me` — the session's own human account. */
export function getMe(signal?: AbortSignal): Promise<HumanOut> {
  return request<HumanOut>("/me", signal ? { signal } : {});
}

/** `GET /api/humans` — every human account. */
export function listHumans(signal?: AbortSignal): Promise<HumanOut[]> {
  return requestList<HumanOut>("humans", "/humans", signal ? { signal } : {});
}

/** `GET /api/agents` — every agent account. */
export function listAgents(signal?: AbortSignal): Promise<AgentOut[]> {
  return requestList<AgentOut>("agents", "/agents", signal ? { signal } : {});
}

/**
 * `POST /api/agents` — create an external agent owned by the session's human.
 *
 * The token comes back in this body — the one and only place it is ever shown.
 */
export function createAgent(name: string, signal?: AbortSignal): Promise<AgentCreatedOut> {
  return request<AgentCreatedOut>("/agents", {
    method: "POST",
    body: { name },
    ...(signal ? { signal } : {}),
  });
}

/**
 * `POST /api/agents/{id}/token-rotate` — replace the token; the old one dies
 * the moment the new one is issued. The new token is in this body and nowhere
 * else.
 */
export function rotateAgentToken(id: string, signal?: AbortSignal): Promise<RotatedTokenOut> {
  return request<RotatedTokenOut>(`/agents/${encodeURIComponent(id)}/token-rotate`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/** `POST /api/agents/{id}/disable` — refuse the agent's token from now on. */
export function disableAgent(id: string, signal?: AbortSignal): Promise<AgentStateOut> {
  return request<AgentStateOut>(`/agents/${encodeURIComponent(id)}/disable`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/** `POST /api/agents/{id}/enable` — re-admit a disabled agent's token. */
export function enableAgent(id: string, signal?: AbortSignal): Promise<AgentStateOut> {
  return request<AgentStateOut>(`/agents/${encodeURIComponent(id)}/enable`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/** `GET /api/grants` — grant rows, optionally one agent's (`agent` filter). */
export function listGrants(agent?: string, signal?: AbortSignal): Promise<GrantOut[]> {
  return requestList<GrantOut>(
    "grants",
    `/grants${query({ agent })}`,
    signal ? { signal } : {},
  );
}

/** `POST /api/grants` — grant (or re-level) an agent's access to a space. */
export function setGrant(body: SetGrantBody, signal?: AbortSignal): Promise<GrantOut> {
  return request<GrantOut>("/grants", { method: "POST", body, ...(signal ? { signal } : {}) });
}

/** `POST /api/grants/revoke` — revoke an agent's grant on a space. */
export function revokeGrant(
  body: RevokeGrantBody,
  signal?: AbortSignal,
): Promise<{ ok: boolean; agent: string; space: string }> {
  return request<{ ok: boolean; agent: string; space: string }>("/grants/revoke", {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
}

/* ------------------------------------------------------------------ */
/* Spaces                                                               */
/* ------------------------------------------------------------------ */

/**
 * `GET /api/spaces` — every active space, with its live node count and the
 * agents holding grants on it.
 *
 * Spaces are nodes in the meta space, which `/api/nodes` excludes by default,
 * so this is the only listing that has them: the vocabulary behind every space
 * picker and the `/spaces` screen's whole read. Human-only server-side, like
 * `/api/grants` — an agent learning the shape of the delegation around it is
 * precisely what the grant model withholds.
 *
 * Archived spaces are absent, and there is no un-archive: the state machine has
 * no `active ← archived` transition, so a listed archived space could offer
 * nothing.
 */
export function listSpaces(signal?: AbortSignal): Promise<SpaceOut[]> {
  return requestList<SpaceOut>("spaces", "/spaces", signal ? { signal } : {});
}

/**
 * `POST /api/spaces` — create a space.
 *
 * A space is an ordinary node (builtin type `space`, living in meta), so this
 * is event-logged, versioned and undoable like any other write — and the space
 * it resolves is `meta`, which is why a refusal here throws
 * {@link UnknownSpaceError} naming that rather than the name being created.
 *
 * @param name The space's name, which is the node's title.
 */
export async function createSpace(name: string, signal?: AbortSignal): Promise<NodeOut> {
  try {
    return await request<NodeOut>("/spaces", {
      method: "POST",
      body: { name },
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    throw asUnknownSpace(error, SPACE_HOME);
  }
}

/**
 * `POST /api/spaces/{id}/rename` — rename a space.
 *
 * A space is a node, so a rename is a node-title update. The path segment
 * resolves as a **space**, so this route cannot be used to rename a node that
 * is not one. Returns the updated node: the HTTP surface writes as `human`, so
 * the rename always lands rather than staging a proposed version.
 *
 * A space the server will not resolve throws {@link UnknownSpaceError}, the
 * same class the filtered reads raise.
 *
 * @param space The space's id **or** its current name.
 * @param name The new name.
 */
export async function renameSpace(
  space: string,
  name: string,
  signal?: AbortSignal,
): Promise<NodeOut> {
  try {
    return await request<NodeOut>(`/spaces/${encodeURIComponent(space)}/rename`, {
      method: "POST",
      body: { name },
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    throw asUnknownSpace(error, space);
  }
}

/**
 * `POST /api/spaces/{id}/archive` — retire a space.
 *
 * Its nodes keep their `space_id` and grants on it go inert; nothing is
 * deleted. There is no way back — the state machine has no `active ←
 * archived` transition — so treat this as final in the interface.
 *
 * A space the server will not resolve throws {@link UnknownSpaceError}, the
 * same class the filtered reads raise.
 *
 * @param space The space's id **or** its name.
 */
export async function archiveSpace(space: string, signal?: AbortSignal): Promise<NodeOut> {
  try {
    return await request<NodeOut>(`/spaces/${encodeURIComponent(space)}/archive`, {
      method: "POST",
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    throw asUnknownSpace(error, space);
  }
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

/**
 * `GET /api/nodes` — list nodes, optionally filtered by type/state/parent/space.
 *
 * `space` narrows to one space and `include_meta` opts into the meta space;
 * both are off by default, which is the whole file minus meta. A space the
 * server will not resolve throws {@link UnknownSpaceError}.
 */
export async function listNodes(
  filters?: NodeFilters,
  signal?: AbortSignal,
): Promise<NodeOut[]> {
  try {
    return await requestList<NodeOut>(
      "nodes",
      `/nodes${query(filters)}`,
      signal ? { signal } : {},
    );
  } catch (error) {
    throw asUnknownSpace(error, filters?.space);
  }
}

/**
 * `POST /api/nodes` — create a node. The server attributes it to `human`.
 *
 * `body.space` is the write target — where the node lands, by id or name,
 * `main` when absent. It says nothing about *who* wrote it: identity stays
 * server-side, as it does on every call in this file. A target the server will
 * not resolve throws {@link UnknownSpaceError}: the write path names exactly
 * one space, so the editor gets the same discriminator the filtered reads do
 * rather than having to re-test the message itself.
 */
export async function createNode(
  body: CreateNodeBody,
  signal?: AbortSignal,
): Promise<NodeOut> {
  try {
    return await request<NodeOut>("/nodes", {
      method: "POST",
      body,
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    throw asUnknownSpace(error, body.space);
  }
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
 *
 * `filters.space` and `filters.include_meta` are the same two read-side
 * controls the node listing takes, and a space the server will not resolve
 * throws the same {@link UnknownSpaceError} here as it does there — which is
 * the point of that class, since the two endpoints answer with different
 * statuses.
 */
export async function search(
  q: string,
  filters?: SearchFilters,
  signal?: AbortSignal,
): Promise<SearchResult> {
  try {
    return await request<SearchResult>(
      `/search${query({ q, ...filters })}`,
      signal ? { signal } : {},
    );
  } catch (error) {
    throw asUnknownSpace(error, filters?.space);
  }
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
/* Review (human tier)                                         */
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
  login,
  logout,
  getMe,
  listHumans,
  listAgents,
  createAgent,
  rotateAgentToken,
  disableAgent,
  enableAgent,
  listGrants,
  setGrant,
  revokeGrant,
  listSpaces,
  createSpace,
  renameSpace,
  archiveSpace,
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
  uploadAsset,
  listAssets,
  getAsset,
  renditionUrl,
  listEvents,
  undo,
  exportNode,
};
