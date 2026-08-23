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
 *   asset registration), because the server refuses anything else. The one
 *   exception is the capability upload, whose raw body sits outside that gate
 *   by design — all three branches are in {@link rawRequest};
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
  ConsolidationOut,
  CreateEdgeBody,
  CreateNodeBody,
  CycleDetailOut,
  CycleOut,
  DiffOut,
  EdgeFilters,
  EdgeOut,
  EdgeTypeOut,
  EventOut,
  GrantOut,
  HealthOut,
  HumanOut,
  IngestOut,
  JsonObject,
  LoginOut,
  NodeFilters,
  NodeOut,
  PathOut,
  ProjectorRun,
  ProposalOut,
  RejectProposalsBody,
  RenditionProfile,
  RequestUploadBody,
  ReviewQueueFilters,
  RevokeGrantBody,
  RollbackConflictOut,
  RollbackCycleBody,
  RollbackOut,
  RotatedTokenOut,
  RunCycleBody,
  SearchFilters,
  SearchResult,
  SetGrantBody,
  SettingAdoptOut,
  SettingChangeOut,
  SettingsOut,
  SpaceOut,
  SubgraphOut,
  SubgraphParams,
  TitleResolution,
  TypeOut,
  TypesOut,
  UndoResult,
  UpdateNodeBody,
  UploadGrantOut,
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
 * reads, the write target on `POST /api/nodes`, all three lifecycle routes, and
 * **both** halves of the capability upload. That is deliberate and
 * load-bearing: {@link isUnknownSpace} is the **only** sanctioned
 * discriminator, so a view must never re-test the message itself. Two copies of
 * one discriminator is how the two drift apart. The upload pair raises the
 * {@link UnknownUploadSpaceError} subclass, which `isUnknownSpace` answers for
 * as well: it adds *which request* refused, not a second way to ask whether one
 * did.
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

/**
 * Every space-resolving route raises this literal text; the status cannot
 * discriminate. The capture is the reference the server itself named, which is
 * the only thing a call handed a bare token — the redemption — has to go on.
 */
const UNKNOWN_SPACE_MESSAGE = /^unknown space:\s*(\S.*?)\s*$/i;

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
 * write target, the three lifecycle routes, and a cycle's `scope` — so that
 * {@link isUnknownSpace} is a complete answer and no view has to keep a second
 * copy of this test.
 *
 * @param error The caught value.
 * @param space The space the call asked for, if it asked for one.
 */
function asUnknownSpace(error: unknown, space: string | undefined): unknown {
  if (!space) return error;
  const named = unknownSpaceReference(error);
  if (named === null) return error;
  return new UnknownSpaceError(space, (error as ApiError).status, (error as ApiError).message);
}

/**
 * The space reference an unknown-space refusal named, or null for anything else.
 *
 * Split out of {@link asUnknownSpace} for the one call that cannot supply the
 * reference itself: {@link redeemUploadGrant} is handed a token, and the space
 * it was minted against lives in the token row rather than in the call. The
 * server's message carries the *resolved* id there, so a caller that knows the
 * reference the human typed re-labels it afterwards.
 *
 * @param error The caught value.
 * @returns The reference the refusal named, or null when this is not one.
 */
function unknownSpaceReference(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status !== 404 && error.status !== 400) return null;
  return UNKNOWN_SPACE_MESSAGE.exec(error.message)?.[1] ?? null;
}

/**
 * A Python exception name and the `": "` a stored failure puts after it.
 *
 * `nodum.consolidate` records a failure as `f"{type(failure).__name__}: {failure}"`,
 * so the stored text is one prefix away from the message a response would have
 * carried. Anchored and identifier-shaped, so a message that merely *contains* a
 * colon (`unknown space: research`) is left whole.
 */
const RECORDED_EXCEPTION_PREFIX = /^[A-Za-z_][A-Za-z0-9_]*:\s*/;

/**
 * The same refusal, recognised in a failure a **cycle report recorded** rather
 * than in a response this client received.
 *
 * A cycle's report carries its failures as strings — there is no response left
 * to catch, and by the time the journal renders one the request that produced it
 * finished hours ago. The journal still has to keep that text out of its copy,
 * because `open_cycle` resolves a cycle's `scope` through the ordinary space
 * rule and so records the server's own *"TypeNotFound: unknown space: 909a…"*
 * verbatim — the one phrasing nothing user-facing may render.
 *
 * It lives **here**, beside {@link isUnknownSpace}, for the reason that
 * discriminator is a singleton at all: the match is one regex with one owner,
 * and a second copy of it in a view is how the two drift apart. This is not a
 * second discriminator — it is the same one, reading a string that never came
 * back through `fetch`.
 *
 * @param recorded The `error` string out of a cycle's report.
 * @returns The space reference the refusal named, or null when the recorded
 *   failure was not an unresolvable space.
 */
export function recordedUnknownSpace(recorded: string): string | null {
  const message = recorded.replace(RECORDED_EXCEPTION_PREFIX, "");
  return UNKNOWN_SPACE_MESSAGE.exec(message)?.[1] ?? null;
}

/**
 * The gardener's own scope refusal (`consolidate._require_gardener_scope`).
 *
 * The **second** message shape a scoped cycle can record about a space, and the
 * one that ships on a default install: migration `0014` seeds the gardener with
 * `main` and `meta` only, so the first click on the journal's scope picker over
 * any space a human created is refused here. The message echoes *the reference
 * the caller supplied*, and the caller that reaches this path by clicking is the
 * web UI, whose picker value is a space **id** — so the recorded failure carries
 * a bare 32-hex id, twice, and the journal used to render it verbatim in both
 * the list and the detail page.
 *
 * It lives beside {@link recordedUnknownSpace} for that function's own reason:
 * a discriminator with two owners is a discriminator that drifts. Between them
 * they cover every refusal in this system whose text names a space, and
 * `journal.ts` fails closed on any *third* shape rather than waiting for it to
 * be added here.
 *
 * The reference is matched out of Python's `repr` — `{reference!r}` — which is
 * single-quoted unless the value itself holds a single quote, so both quotings
 * are read. The exception prefix is stripped first exactly as above, and the
 * same match works on a **live** `ApiError.message` (403), which carries the
 * sentence with no prefix: `http_api._failure_message` exempts this package's
 * own exceptions from the storage rewrite, so what arrives is `str(exc)`.
 *
 * @param recorded The `error` string out of a cycle's report, or the `message`
 *   off a caught `ApiError`.
 * @returns The space reference the refusal named, or null when the failure was
 *   not the gardener being ungranted.
 */
export function recordedUngrantedScope(recorded: string): string | null {
  const message = recorded.replace(RECORDED_EXCEPTION_PREFIX, "");
  const match = UNGRANTED_SCOPE_MESSAGE.exec(message);
  if (match === null) return null;
  return match[1] ?? match[2] ?? null;
}

/**
 * `consolidate._require_gardener_scope`'s literal opening, with the reference it
 * echoes back.
 *
 * Anchored on the whole opening clause rather than on "gardener", so an
 * unrelated message merely mentioning it cannot be rewritten as this refusal.
 */
const UNGRANTED_SCOPE_MESSAGE =
  /^the gardener holds no grant on space (?:'([^']*)'|"([^"]*)")/i;

/**
 * A rollback the graph has moved past — 409, with the rows that are in the way.
 *
 * The one refusal on this surface whose body carries more than `type` and
 * `message` (`http_api._rollback_conflict_handler`), and the extra is the whole
 * point of decision C4: rollback is atomic and refuses rather than clobbers, so
 * it **names what is blocking it** rather than reporting that it failed. Parsing
 * that back out of the message is the alternative this class avoids.
 *
 * A caller normally never sees one, because the dry-run preflight answers the
 * same list under a 200 — see {@link rollbackCycle}. This is the race: the graph
 * moved between the preflight and the commit.
 */
export class RollbackConflictError extends ApiError {
  /** The rows standing between the cycle and its reversal, verbatim. */
  readonly conflicts: RollbackConflictOut[];

  constructor(status: number, type: string, message: string, conflicts: RollbackConflictOut[]) {
    super(status, type, message);
    this.name = "RollbackConflictError";
    this.conflicts = conflicts;
  }
}

/**
 * Whether a caught value is a refused rollback carrying its conflicts.
 *
 * @param error The caught value.
 */
export function isRollbackConflict(error: unknown): error is RollbackConflictError {
  return error instanceof RollbackConflictError;
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
 *
 * The `conflicts` branch is here rather than in the calling route because this
 * is the **only** place an error body is parsed, and the rows it carries exist
 * nowhere else once it returns — unlike the unknown-space normalisation, which
 * re-reads the message a caller already has. That is the whole difference
 * between the two shapes.
 */
async function toApiError(response: Response): Promise<ApiError> {
  let type = "HTTPError";
  let message = `${response.status} ${response.statusText}`.trim();
  let conflicts: RollbackConflictOut[] | null = null;
  try {
    const body = (await response.json()) as Partial<ApiErrorBody>;
    if (body && typeof body === "object" && body.error) {
      type = body.error.type ?? type;
      message = body.error.message ?? message;
      if (Array.isArray(body.error.conflicts)) conflicts = body.error.conflicts;
    }
  } catch {
    // Body was not JSON — keep the status-derived message.
  }
  if (conflicts !== null) {
    return new RollbackConflictError(response.status, type, message, conflicts);
  }
  return new ApiError(response.status, type, message);
}

/** Options for {@link rawRequest}. */
interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  /** JSON-serialised into the body. Mutually exclusive with `form` and `raw`. */
  body?: unknown;
  /** Sent as-is; the browser sets the multipart boundary. */
  form?: FormData;
  /**
   * Sent as the whole body, with no `Content-Type` of ours — the capability
   * upload. Mutually exclusive with `body` and `form`.
   */
  raw?: Blob;
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
  } else if (options.raw) {
    // A raw body, and no content type of ours. The two capability routes sit
    // outside the content-type gate (`http_api._is_capability_path`) precisely
    // so a raw upload need not claim to be JSON, and claiming it would be a
    // lie about the bytes. What the browser derives from the Blob is harmless:
    // `PUT /api/uploads/{token}` reads the name and the MIME off the token row
    // it minted, never off the request.
    body = options.raw;
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
 * Archived spaces are absent. They still hold their names, though — a space
 * title is reserved for good (`0013_unique_space_titles`) — so a create can be
 * refused for a name that is in nothing this list returns. That refusal says
 * the holder is archived rather than leaving the human to look for it here.
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
 * deleted. Treat it as final in the interface: the state machine has no
 * `active ← archived` transition, and the one route back — undoing the
 * `node.archive` event — is not on any screen. The space keeps its name either
 * way, so the name stays reserved and that undo can never collide.
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

/** `POST /api/edges/{id}/archive` — retire one relationship. Human-only, server-enforced. */
export function archiveEdge(id: string, signal?: AbortSignal): Promise<EdgeOut> {
  return request<EdgeOut>(`/edges/${encodeURIComponent(id)}/archive`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
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

/**
 * `GET /api/nodes/resolve` — resolve `[[wikilink]]` titles to node ids, batch.
 *
 * One call for a whole rendered document. Matching is exact and casefolded
 * over non-archived nodes, with ambiguity reported rather than guessed, so a
 * click on a wikilink never travels somewhere arbitrary.
 *
 * `options.space` is the read-side **preference** for breaking ties — the
 * same filter {@link listNodes} takes — and a space the server will not
 * resolve throws {@link UnknownSpaceError} exactly as it does there.
 */
export async function resolveTitles(
  titles: string[],
  options?: { space?: string },
  signal?: AbortSignal,
): Promise<TitleResolution[]> {
  try {
    return await requestList<TitleResolution>(
      "resolutions",
      `/nodes/resolve${query({ titles, space: options?.space })}`,
      signal ? { signal } : {},
    );
  } catch (error) {
    throw asUnknownSpace(error, options?.space);
  }
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
 *
 * **This route only registers bytes**: no `asset_ref`, no `source`, no event.
 * It is the editor's drop — an image whose describing node is the note that
 * carries it inline — and it admits rasters alone for that reason. A document
 * the graph is meant to *know about* goes through {@link ingestUpload}, which
 * is the whole difference between bytes in the store and a subgraph
 * (design decision D1).
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
/* Uploads — the capability flow (design §5.7 rule 4)                   */
/* ------------------------------------------------------------------ */

/** Which of the upload flow's two requests something happened on. */
export type UploadPhase =
  /** `POST /api/uploads` — nothing has been sent yet. */
  | "mint"
  /** `PUT /api/uploads/{token}` — the grant is spent and the body has gone. */
  | "redemption";

/**
 * A refused write target on the upload flow, tagged with which request refused.
 *
 * A subclass rather than a second discriminator, and that is the point:
 * {@link isUnknownSpace} stays the **only** test for space-ness and answers
 * true for this too. What it adds is a different fact — *when* in the flow the
 * refusal happened — because the two are not the same event for a human. A
 * refused mint sent nothing at all; a refused redemption spent the grant and
 * streamed the body before the pipeline resolved the space and stopped. Copy
 * that cannot tell them apart ends up claiming "nothing was uploaded" about a
 * request that uploaded the whole file.
 */
export class UnknownUploadSpaceError extends UnknownSpaceError {
  /** The request that refused the space. */
  readonly phase: UploadPhase;

  constructor(space: string, wireStatus: number, message: string, phase: UploadPhase) {
    super(space, wireStatus, message);
    this.name = "UnknownUploadSpaceError";
    this.phase = phase;
  }
}

/**
 * Which upload request refused a space, when that is what happened.
 *
 * @param error The caught value.
 * @returns The phase, or null for anything that is not an upload's own
 *   space refusal — including a bare {@link UnknownSpaceError}, which carries
 *   no phase to report.
 */
export function uploadRefusalPhase(error: unknown): UploadPhase | null {
  return error instanceof UnknownUploadSpaceError ? error.phase : null;
}

/**
 * Tag an unknown-space refusal with the request it came from.
 *
 * @param error The caught value.
 * @param space The space the call asked for, if it asked for one.
 * @param phase Which request this is.
 */
function asUploadSpaceRefusal(
  error: unknown,
  space: string | undefined,
  phase: UploadPhase,
): unknown {
  const normalised = asUnknownSpace(error, space);
  if (!isUnknownSpace(normalised)) return normalised;
  return new UnknownUploadSpaceError(
    normalised.space,
    normalised.wireStatus,
    normalised.message,
    phase,
  );
}

/**
 * `POST /api/uploads` — mint a single-use grant to PUT one file to.
 *
 * The mint is where everything is checked *before* any bytes move: the
 * declared `size` against the server's own ceiling, and the target `space`
 * against what this session may write. A space it will not resolve throws
 * {@link UnknownSpaceError}, like every other call in this file that names one
 * — so a caller branches on {@link isUnknownSpace} and never on the message.
 *
 * Callers want {@link ingestUpload}; this half is exported because the two
 * requests fail for genuinely different reasons and a caller may want to know
 * which one it was.
 */
export async function requestUploadUrl(
  body: RequestUploadBody,
  signal?: AbortSignal,
): Promise<UploadGrantOut> {
  try {
    return await request<UploadGrantOut>("/uploads", {
      method: "POST",
      body,
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    throw asUploadSpaceRefusal(error, body.space, "mint");
  }
}

/**
 * `PUT /api/uploads/{token}` — spend a grant and run the ingestion pipeline.
 *
 * **Redeemed against our own origin, never against `grant.url`** (design
 * decision D5): that field is absolute and built from `NODUM_PUBLIC_URL`,
 * which exists so a foreign host can be told where this server lives and may
 * name an address that is not the one this page was served from. The client
 * owns its origin; the grant only carries the capability.
 *
 * The body is the file itself, with no content type of ours — see
 * {@link rawRequest}'s `raw` branch.
 *
 * The token is spent by `urls.consume` **before** the body is read, so a
 * refusal of the bytes still spends the grant. There is nothing to resume and
 * no revoke endpoint: a retry is a fresh mint.
 *
 * **This half normalises a refused space itself**, because it is exported and
 * can therefore leak on its own. The pipeline resolves the token row's space
 * again on the far side of the PUT, so this request refuses an unresolvable
 * target exactly as the mint does — and a bare `ApiError(404, "TypeNotFound",
 * "unknown space: sp-old")` reaching `describeFailure` renders as *"The server
 * has no record of this upload. unknown space: sp-old"*, which is two forbidden
 * phrasings in one sentence. The space is read out of the server's own message,
 * since this call is handed a token and the target lives in the token row;
 * {@link ingestUpload} re-labels it with the reference the human actually typed,
 * because the message names the **resolved** 32-hex id.
 *
 * @param token The `token` from the grant, not its `url`.
 * @param file The bytes to send.
 * @throws UnknownUploadSpaceError When the pipeline could not resolve the space
 *   the grant was minted against, phase `redemption`.
 */
export async function redeemUploadGrant(
  token: string,
  file: Blob,
  signal?: AbortSignal,
): Promise<IngestOut> {
  try {
    return await request<IngestOut>(`/uploads/${encodeURIComponent(token)}`, {
      method: "PUT",
      raw: file,
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    throw asUploadSpaceRefusal(error, unknownSpaceReference(error) ?? undefined, "redemption");
  }
}

/**
 * Ingest one file this browser is holding: mint a grant, then spend it.
 *
 * The capability flow was built for "an agent host with no shared filesystem",
 * and a browser is exactly that — it holds bytes, not a path the server can
 * read, which is why `POST /api/ingest` (one of `path` or `url`, both
 * server-side) cannot serve it. What comes back is therefore the subgraph
 * ingestion wrote — `asset_ref`, `source`, the `derived_from` edge and one
 * `block` per page — rather than a registration receipt.
 *
 * No `sha256` is declared, deliberately: see {@link RequestUploadBody}.
 *
 * @param file The file to ingest; its name and size are declared on the mint.
 * @param options `space` is the write target for the describing nodes.
 * @throws ApiError From either request — a mint the size or the space refused,
 *   or a redemption the server's type policy refused. Both are ordinary
 *   failures for the caller to describe; neither leaves a resumable state. A
 *   refused space arrives as {@link UnknownUploadSpaceError}, whose `phase`
 *   says which request it was — the two are not the same event on screen, since
 *   a refused mint sent nothing and a refused redemption sent the whole file.
 */
export async function ingestUpload(
  file: File,
  options: { space?: string } = {},
  signal?: AbortSignal,
): Promise<IngestOut> {
  const minted = await requestUploadUrl(
    {
      name: file.name,
      // A browser reports "" for a type it cannot guess from the extension.
      // Saying so plainly beats inventing one, and the server prefers what it
      // sniffs out of the bytes over anything declared here anyway.
      mime: file.type || "application/octet-stream",
      size: file.size,
      ...(options.space === undefined ? {} : { space: options.space }),
    },
    signal,
  );

  if (minted.grant === null) {
    // Only a *declared* sha256 the store already holds produces a grantless
    // answer, and this client declares none — so reaching here means the
    // response did not match its own contract. Reported as a refusal rather
    // than dereferenced into a `TypeError`, and worded for what the caller has
    // to know: no ingestion happened, so nothing describes these bytes.
    throw new ApiError(
      500,
      "MissingUploadGrant",
      "the server answered with no upload grant, so these bytes were not ingested",
    );
  }

  try {
    return await redeemUploadGrant(minted.grant.token, file, signal);
  } catch (error) {
    // `redeemUploadGrant` already normalised the refusal — it has to, being
    // exported — but it could only name the space the *server* named, which is
    // the resolved 32-hex id. This is the only place that knows the reference
    // the human typed, so it re-labels rather than leaving an id on screen.
    throw relabelUploadSpace(error, options.space);
  }
}

/**
 * Re-label an upload's space refusal with the reference the caller asked for.
 *
 * The mint refuses the reference it was given, so it needs none of this; the
 * redemption refuses the id the token row resolved to, which is a 32-hex string
 * nobody typed. The phase is preserved — that is the fact the copy branches on.
 *
 * @param error The caught value.
 * @param space The reference the caller asked for, if it asked for one.
 */
function relabelUploadSpace(error: unknown, space: string | undefined): unknown {
  if (space === undefined) return error;
  if (!(error instanceof UnknownUploadSpaceError)) return error;
  if (error.space === space) return error;
  return new UnknownUploadSpaceError(space, error.wireStatus, error.message, error.phase);
}

/* ------------------------------------------------------------------ */
/* Consolidation cycles — the dream journal (design §8.4)               */
/* ------------------------------------------------------------------ */

/**
 * `GET /api/cycles` — the consolidation journal, newest first.
 *
 * Human-only server-side, for the reason the event log is: a journal entry says
 * what the gardener did across every space in the file.
 */
export function listCycles(limit?: number, signal?: AbortSignal): Promise<CycleOut[]> {
  return requestList<CycleOut>("cycles", `/cycles${query({ limit })}`, signal ? { signal } : {});
}

/**
 * `GET /api/cycles/{id}` — one entry, its metrics, and the events it wrote.
 *
 * The diff is `list_events` narrowed to the cycle — the same append-only log
 * every other read comes from — so the entry cannot become a second record that
 * disagrees with what happened. `limit` bounds the event window and
 * `events_truncated` says when it bit.
 */
export function getCycle(
  id: string,
  options?: { limit?: number },
  signal?: AbortSignal,
): Promise<CycleDetailOut> {
  return request<CycleDetailOut>(
    `/cycles/${encodeURIComponent(id)}${query({ limit: options?.limit })}`,
    signal ? { signal } : {},
  );
}

/**
 * `POST /api/cycles` — run a consolidation cycle now.
 *
 * The on-demand half of "a cycle runs, on demand and on a schedule". The
 * schedule is off unless configured, so without this a fresh install's journal
 * stays empty forever.
 *
 * `dry_run` rehearses it: every job computed, the report written, and **no graph
 * event emitted**, which is the checkable form of "it changed nothing". It is
 * sent as a real boolean because the server refuses a string there rather than
 * coercing it.
 *
 * `scope` names a space, so a target the server will not resolve throws
 * {@link UnknownSpaceError} exactly as every other space-naming call in this
 * file does — `open_cycle` resolves the scope through the ordinary space rule,
 * and a space archived since the picker was filled is the live case.
 */
export async function runCycle(
  body: RunCycleBody = {},
  signal?: AbortSignal,
): Promise<ConsolidationOut> {
  try {
    return await request<ConsolidationOut>("/cycles", {
      method: "POST",
      body,
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    throw asUnknownSpace(error, body.scope);
  }
}

/**
 * `POST /api/cycles/{id}/abandon` — close an interrupted cycle as `failed`.
 *
 * The door out of a stuck run, and the **precondition** for the route below
 * rather than a tidier journal: `service._rollback_plan` refuses a cycle that
 * has not closed, and `undo` refuses every event a cycle stamped, so a run a
 * `SIGKILL` or a shutdown left `running` has its writes irreversible on every
 * surface until somebody closes the row. It changes nothing the run wrote.
 *
 * Takes no body. It refuses a cycle that is not `running` with
 * `InvalidTransition` (**400**) — one that has said how it ended is not
 * abandoned, and re-closing it would overwrite that record — and an unknown id
 * with `RecordNotFound` (**404**). Human-only in the service, which every
 * session on this surface satisfies by construction.
 *
 * @param id The cycle to abandon.
 * @returns The cycle row, now `failed`.
 */
export function abandonCycle(id: string, signal?: AbortSignal): Promise<CycleOut> {
  return request<CycleOut>(`/cycles/${encodeURIComponent(id)}/abandon`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/**
 * `POST /api/cycles/{id}/stop` — ask a running cycle to stop (the kill switch).
 *
 * **Not {@link abandonCycle}, and not a softer version of it.** Abandoning is a
 * repair: a human closing a dead process's entry from *outside*, which is what
 * makes its writes reversible. This is an instruction to a run that is still
 * alive — the row comes back still `running`, with `stop_requested_by` and
 * `stop_requested_at` stamped on it, and the run closes its own entry when it
 * notices. The journal keeps the two apart because a `failed` cycle read the
 * next morning has to say whether the operator stopped it or the process died.
 *
 * It reverses nothing: every write the run already made stays in the graph,
 * stamped with the cycle, and {@link rollbackCycle} is what takes those back
 * once the entry has closed.
 *
 * Takes no body. It refuses a cycle that is not `running` with
 * `InvalidTransition` (**400**) — nothing is left to obey it — and an unknown id
 * with `RecordNotFound` (**404**). A **second** stop is a **200** that keeps the
 * first asker rather than a refusal, so a human who presses twice is never left
 * wondering whether the first press worked.
 *
 * @param id The cycle to stop.
 * @returns The cycle row, still `running`, now carrying the stop.
 */
export function requestCycleStop(id: string, signal?: AbortSignal): Promise<CycleOut> {
  return request<CycleOut>(`/cycles/${encodeURIComponent(id)}/stop`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/**
 * `POST /api/cycles/{id}/rollback` — take a whole cycle back (design D7).
 *
 * **Call it with `dryRun` first.** A dry run opens no cycle, writes nothing, and
 * returns its verdict under a **200** — which is the "would this succeed?" a
 * confirm dialog needs, so the human meets a refusal *before* committing rather
 * than after. A real rollback that meets a conflict refuses with 409 and the
 * same list, raised as {@link RollbackConflictError}; that path is the race
 * where the graph moved between the two calls, not the ordinary one.
 *
 * The verdict is **two** lists, `conflicts` and `blockers`, and it is clean only
 * when both are empty — a caller reading one of them offers a confirm button for
 * a rollback that will fail. Only `conflicts` has a 409 body to come back in:
 * a blocker met for real raises `UndoNotPossible` carrying the guard's sentence
 * and no list, which is an ordinary {@link ApiError}.
 *
 * @param id The cycle to take back.
 * @param options `dryRun` rehearses the reversal.
 */
export function rollbackCycle(
  id: string,
  options: { dryRun?: boolean } = {},
  signal?: AbortSignal,
): Promise<RollbackOut> {
  const body: RollbackCycleBody =
    options.dryRun === undefined ? {} : { dry_run: options.dryRun };
  return request<RollbackOut>(`/cycles/${encodeURIComponent(id)}/rollback`, {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
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

/* ------------------------------------------------------------------ */
/* Settings — the file beside the graph                                 */
/* ------------------------------------------------------------------ */

/**
 * `GET /api/settings` — every setting: value in force, provenance, default.
 *
 * The read is `nodum config list` verbatim (same envelope, same bytes) plus
 * the build-capability flags. A secret's `value` is never carried — the row
 * says whether one is set and nothing more.
 */
export function getSettings(signal?: AbortSignal): Promise<SettingsOut> {
  return request<SettingsOut>("/settings", signal ? { signal } : {});
}

/**
 * `PUT /api/settings` — apply several changes atomically: all of them or none.
 *
 * `null` removes a key from the file (falling back down the ladder); an empty
 * string is refused by the server — unset it instead. A key the environment
 * pins is a **409** (`SettingPinned`) with the file untouched; every other
 * refusal is a 400 carrying the seam's own sentence.
 */
export function applySettings(
  changes: Record<string, string | null>,
  signal?: AbortSignal,
): Promise<SettingChangeOut[]> {
  return requestList<SettingChangeOut>("changes", "/settings", {
    method: "PUT",
    body: changes,
    ...(signal ? { signal } : {}),
  });
}

/**
 * `DELETE /api/settings/{name}` — remove one key from the file.
 *
 * Removing a key the file never carried is the state the caller asked for:
 * 200 with `changed: false`, never a 404.
 */
export function unsetSetting(name: string, signal?: AbortSignal): Promise<SettingChangeOut> {
  return request<SettingChangeOut>(`/settings/${encodeURIComponent(name)}`, {
    method: "DELETE",
    ...(signal ? { signal } : {}),
  });
}

/**
 * `POST /api/settings/adopt-env` — store every editable, non-empty
 * environment value.
 *
 * An adopted key keeps resolving `provenance: "environment"` — adopt never
 * touches the environment, which stays a host-side step — but the file now
 * carries the same value, so unsetting the variable later no longer moves
 * what is in force. A value the registry refuses is skipped and named in the
 * answer, not a batch failure.
 */
export function adoptEnvironment(signal?: AbortSignal): Promise<SettingAdoptOut> {
  return request<SettingAdoptOut>("/settings/adopt-env", {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
}

/**
 * `POST /api/settings/export` — the effective configuration as a `.env`
 * download. The one settings call that does not go through {@link request}:
 * the response **is** the file (`application/octet-stream`), not the JSON
 * envelope, so this is the named second branch beside it.
 *
 * No URL and no token exist for the export — a secret-bearing URL would land
 * in access logs — so the caller saves the Blob itself (createObjectURL).
 * With `includeSecrets`, `password` is required: the server re-verifies the
 * session human's password through the login path, and a wrong one answers
 * 401 with the ordinary error body.
 */
export async function exportSettings(
  options: { includeSecrets: boolean; password?: string },
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await fetch(`${API_BASE}/settings/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/octet-stream" },
    body: JSON.stringify(options),
    ...(signal ? { signal } : {}),
  });
  if (!response.ok) throw await toApiError(response);
  return response.blob();
}

/**
 * `POST /api/projectors/{name}/rebuild` — drop one projector's derived state
 * and replay the event log from event 0.
 *
 * The coupling that makes a `NODUM_EMBED_MODEL` change safe: after a model
 * write every stored chunk is invisible to search until the `vec` projector
 * re-embeds them, and this is the offered action. Human-only at the domain,
 * like the settings writes, and each completed run appends a
 * `projector.rebuild` event naming the session's human.
 */
export function rebuildProjector(name: string, signal?: AbortSignal): Promise<ProjectorRun> {
  return request<ProjectorRun>(`/projectors/${encodeURIComponent(name)}/rebuild`, {
    method: "POST",
    ...(signal ? { signal } : {}),
  });
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
  archiveEdge,
  listEdges,
  createEdge,
  search,
  suggestLinks,
  resolveTitles,
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
  requestUploadUrl,
  redeemUploadGrant,
  ingestUpload,
  listCycles,
  getCycle,
  runCycle,
  abandonCycle,
  requestCycleStop,
  rollbackCycle,
  listEvents,
  undo,
  exportNode,
  getSettings,
  applySettings,
  unsetSetting,
  adoptEnvironment,
  exportSettings,
  rebuildProjector,
};
