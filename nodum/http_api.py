"""The HTTP adapter (design §9) — a thin Starlette front over the service layer.

This surface is for the **human**, and it is the exact inverse of
:mod:`nodum.mcp_server`. The MCP server forces an ``agent:<name>`` actor and
refuses ``human``; here every write is attributed to :data:`HTTP_ACTOR` —
``human`` — and *nothing a request carries can change that*.

**How the actor boundary is structural.** There is exactly one expression in
this module that binds a service function's ``actor`` argument, and it lives in
:func:`_write`, where it binds the module constant. Route handlers never name
an actor at all — they cannot read one from a body, a header, or a query
string, because none of them mentions the concept — and request data is never
forwarded wholesale: every handler picks the fields it uses by name. A new
endpoint inherits the guarantee by construction; breaking it would take a
second ``actor=`` binding or a direct call to a write function, both of which
``tests/test_http_api.py`` fails on. The service layer's own human tier
(``_require_human_reviewer`` over ``HUMAN_ONLY_ACTIONS``) then applies as it
does to every other surface — this module re-implements none of it.

Everything else is convention, shared rather than re-stated:

* **Envelope** — :mod:`nodum.envelope`, the same helper the CLI prints
  through, so ``GET /api/nodes/{id}`` and ``nodum node get <id>`` emit the same
  bytes.
* **Errors** — one :data:`EXCEPTION_STATUS` table installed as Starlette
  exception handlers, reusing the exact exception classes ``cli._run`` catches
  and echoing its one-line message as ``{"error": {"type", "message"}}``.
  Anything unmapped is a 500 with a generic message; the traceback goes to the
  server log, never into a response body.
* **Auth** — bind loopback (``nodum serve`` defaults to ``127.0.0.1``); an
  optional static bearer token gates ``/api/*`` and nothing else. No CORS: the
  UI is served by this same process, same origin.
* **Static hosting** — the built Vite bundle at ``nodum/_web/`` is served at
  ``/``, with unknown non-API paths falling through to its ``index.html`` so
  client-side routes survive a reload. When the bundle is absent (a source
  checkout that never ran ``make web-build``, or a directory Vite has just
  emptied) the tracked ``nodum/_web_placeholder.html`` is served instead — a
  page that says what to run, not a crash on a missing directory.
  ``/favicon.ico`` is the one path exempted from that fall-through: a browser
  asks for it unprompted, and answering an icon request with an HTML document
  under a 200 is a lie the client cannot detect (see :func:`create_app`).

Handlers call the service inline rather than through a thread pool: the service
opens one short-lived connection per call and SQLite has a single writer
anyway, so a local single-user server gains nothing from concurrency here and
stays much easier to reason about.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import tempfile
from http import HTTPStatus
from importlib import metadata
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import Headers, QueryParams, UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from nodum import assets, db, service
from nodum import search as search_module
from nodum.assets import AssetNotFound, AssetSourceChanged, AssetTooLarge, UnsupportedRendition
from nodum.envelope import envelope, list_envelope, render_json
from nodum.service import (
    EventNotFound,
    InvalidTransition,
    PolicyNotFound,
    RecordNotFound,
    ReviewNotPermitted,
    TypeNotFound,
    UndoNotPossible,
)

#: The actor every write on this surface is attributed to. The HTTP API *is*
#: the human surface (design §9), so this is a module constant rather than
#: anything a request can influence — the inverse of the MCP server, which
#: forces ``agent:<name>`` and refuses ``human``.
HTTP_ACTOR = service.ACTOR_HUMAN

#: Prefix every API route carries. ``/healthz`` deliberately sits outside it:
#: a liveness probe must answer without credentials.
API_PREFIX = "/api"

#: The built frontend bundle (Vite's ``outDir``). Gitignored whole and absent
#: in a source checkout that never ran ``make web-build`` — resolved at request
#: time, never at import time, so building the bundle needs no restart.
WEB_ROOT = Path(__file__).resolve().parent / "_web"

#: The tracked fallback page served when :data:`WEB_ROOT` holds no bundle. It
#: lives outside ``_web/`` because Vite's ``emptyOutDir`` wipes that directory
#: on every build.
WEB_PLACEHOLDER = Path(__file__).resolve().parent / "_web_placeholder.html"

#: Read size for spooling a multipart upload to disk — the same 1 MiB chunk
#: :mod:`nodum.assets` streams blobs with, so a large file is never held whole.
UPLOAD_CHUNK_BYTES = 1 << 20

#: Node fields ``PATCH /api/nodes/{id}`` may change (the service's
#: ``VERSION_FIELDS``). An allowlist, so an unexpected body key is inert rather
#: than forwarded.
PATCHABLE_FIELDS = ("title", "content", "props")

#: Review-queue filter keys, shared by the queue listing and the filter form of
#: batch accept/reject.
PROPOSAL_FILTERS = ("created_by", "type", "kind", "created_before", "created_after")

#: Methods the ``/api`` catch-all answers, so a wrong verb on an unknown route
#: is a JSON 404 rather than a bare 405 from the router.
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

#: Query-string values accepted as booleans (``?expand=1``, ``?expand=true``).
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

#: Characters kept when an id is echoed into a ``Content-Disposition`` filename.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

#: Exception → HTTP status. The classes are exactly the ones ``cli._run``
#: catches, so a failure reads the same on both surfaces and the status is the
#: HTTP translation of the CLI's exit 1. Lookup is by MRO, so the subclasses
#: listed here (``InvalidTransition``, ``UndoNotPossible``) win over the
#: ``ValueError`` they inherit from.
EXCEPTION_STATUS: dict[type[Exception], int] = {
    # 404 — an id, name, or seq that resolves to nothing. RecordNotFound covers
    # node/edge/version ids and the transitions that accept all three.
    RecordNotFound: 404,
    TypeNotFound: 404,
    EventNotFound: 404,
    PolicyNotFound: 404,
    AssetNotFound: 404,
    # 400 — the request itself is wrong: a bad value, an impossible transition,
    # an asset that cannot be stored or rendered.
    ValueError: 400,
    InvalidTransition: 400,
    AssetTooLarge: 400,
    AssetSourceChanged: 400,
    UnsupportedRendition: 400,
    # 403 — the human tier refused a non-human actor. Unreachable from this
    # surface by construction; mapped so it could never surface as a 500.
    ReviewNotPermitted: 403,
    # 409 — the graph has grown past the event being undone.
    UndoNotPossible: 409,
    # 503 — SQLite has one writer and a large asset registration holds it for
    # the whole copy, so "database is locked" is retryable, not a server error.
    sqlite3.OperationalError: 503,
}

#: Body of the catch-all 500. Generic on purpose: the traceback belongs in the
#: server log, not in a response.
INTERNAL_ERROR = {"type": "InternalError", "message": "internal server error"}

try:
    VERSION = metadata.version("nodum")
except metadata.PackageNotFoundError:  # pragma: no cover - uninstalled source checkout
    VERSION = "unknown"


# ── The actor boundary ────────────────────────────────────────────────────────


def _write(operation: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call a service write as :data:`HTTP_ACTOR` — the only actor this surface has.

    Every write, review, policy, archive, and undo handler goes through here,
    and this is the **one** place in the module that binds ``actor`` at all. A
    caller cannot supply one: an ``actor`` keyword arriving here would mean a
    handler forwarded request data wholesale, so it is refused rather than
    honoured and no request field can ever reach the service as an identity.

    Args:
        operation: The :mod:`nodum.service` function to invoke.
        *args: Positional arguments for it.
        **kwargs: Keyword arguments for it, never including ``actor``.

    Returns:
        Whatever the service function returns.

    Raises:
        RuntimeError: If a caller tried to supply an actor.
    """
    if "actor" in kwargs:
        raise RuntimeError(
            "the HTTP surface never takes an actor from a caller: "
            f"writes are always attributed to {HTTP_ACTOR!r}"
        )
    return operation(*args, actor=HTTP_ACTOR, **kwargs)


# ── Responses ─────────────────────────────────────────────────────────────────


class EnvelopeResponse(JSONResponse):
    """A JSON response rendered exactly as the CLI prints the same envelope.

    Rendering through :func:`nodum.envelope.render_json` — plus the newline
    ``print`` adds — is what makes ``GET /api/nodes/{id}`` byte-identical to
    ``nodum node get <id>`` on stdout, parity a test can assert literally
    instead of after re-parsing both sides.
    """

    def render(self, content: Any) -> bytes:
        """Render the payload as indented, non-ASCII-escaped JSON plus a newline."""
        return (render_json(content) + "\n").encode("utf-8")


def _error(status_code: int, error_type: str, message: str) -> EnvelopeResponse:
    """Build the ``{"error": {"type", "message"}}`` body every failure carries."""
    return EnvelopeResponse(
        {"error": {"type": error_type, "message": message}}, status_code=status_code
    )


def _exception_handler(status_code: int) -> Any:
    """Build the handler installed for one mapped exception class."""

    async def handler(request: Request, exc: Exception) -> Response:
        # `database error: …` is the line the CLI prints for a SQLite failure,
        # so both surfaces report the same problem in the same words.
        message = f"database error: {exc}" if isinstance(exc, sqlite3.Error) else str(exc)
        return _error(status_code, type(exc).__name__, message)

    return handler


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """Render Starlette's own errors (404, 405, the 401 below) in the error envelope."""
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", str(exc))
    return _error(status_code, HTTPStatus(status_code).phrase.replace(" ", ""), detail)


async def _server_error_handler(request: Request, exc: Exception) -> Response:
    """Return the generic 500 body; Starlette re-raises so the server logs the traceback."""
    return EnvelopeResponse({"error": INTERNAL_ERROR}, status_code=500)


# ── Auth ──────────────────────────────────────────────────────────────────────


class BearerTokenMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``/api/*``.

    Installed only when a token is configured (``nodum serve --token``), which
    is the LAN case; the default loopback bind has no auth at all. ``/healthz``
    and the static UI stay open either way — a liveness probe that needs
    credentials is not a liveness probe, and the page that *holds* the token
    cannot itself require it.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject unauthenticated API requests; pass everything else through."""
        if scope["type"] == "http" and scope["path"].startswith(API_PREFIX):
            presented = Headers(scope=scope).get("authorization", "")
            # Constant-time: a short shared secret compared on every request.
            if not secrets.compare_digest(presented, self.expected):
                response = _error(401, "Unauthorized", "missing or invalid bearer token")
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ── Request parsing (nothing here ever yields an identity) ────────────────────


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON object body; an empty body is an empty object.

    Raises:
        ValueError: If the body is not a JSON object.
    """
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"expected a JSON object body: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object body, got {type(data).__name__}")
    return data


def _required(body: dict[str, Any], key: str) -> Any:
    """Return a required body field.

    Raises:
        ValueError: If the key is missing or null.
    """
    value = body.get(key)
    if value is None:
        raise ValueError(f"missing required field: {key!r}")
    return value


def _param(params: QueryParams, *names: str) -> str | None:
    """Return the first of ``names`` present in the query string.

    A few endpoints accept both the name this surface documents and the one the
    service function's own parameter uses (``root``/``root_id``), so a caller
    cannot silently miss with the wrong-but-obvious spelling.
    """
    for name in names:
        value = params.get(name)
        if value is not None:
            return value
    return None


def _required_param(params: QueryParams, *names: str) -> str:
    """Return a required query parameter.

    Raises:
        ValueError: If none of ``names`` is present.
    """
    value = _param(params, *names)
    if value is None:
        raise ValueError(f"missing required query parameter: {names[0]!r}")
    return value


def _int_param(params: QueryParams, *names: str, default: int | None = None) -> Any:
    """Parse an integer query parameter, falling back to ``default``.

    Raises:
        ValueError: If the value is not an integer.
    """
    raw = _param(params, *names)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{names[0]} must be an integer, got {raw!r}") from exc


def _float_param(params: QueryParams, name: str) -> float | None:
    """Parse an optional float query parameter.

    Raises:
        ValueError: If the value is not a number.
    """
    raw = params.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool_param(params: QueryParams, name: str, *, default: bool = False) -> bool:
    """Parse a boolean query parameter (``1``/``true``/``yes``/``on``).

    Raises:
        ValueError: If the value is not recognisable as a boolean.
    """
    raw = params.get(name)
    if raw is None:
        return default
    folded = raw.strip().casefold()
    if folded in _TRUE_VALUES:
        return True
    if folded in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _list_param(params: QueryParams, *names: str) -> list[str] | None:
    """Collect a repeatable query parameter (``?edge_type=a&edge_type=b``).

    Returns ``None`` when absent, which the service functions read as "no
    filter" — an empty list means something else entirely to some of them.
    """
    values = [value for name in names for value in params.getlist(name)]
    return values or None


def _state_param(params: QueryParams, name: str = "state") -> str | None:
    """Read a search state filter, mapping the CLI's ``any`` to "no filter"."""
    state = params.get(name)
    return None if state in (None, "any") else state


def _proposal_filters(source: Any) -> dict[str, Any]:
    """Pick the review-queue filter keys out of a body or a query string.

    An allowlist by construction: keys outside :data:`PROPOSAL_FILTERS` are
    never read, so nothing a caller invents reaches a service argument.
    """
    filters = {key: source.get(key) for key in PROPOSAL_FILTERS}
    # `agent` is the review UI's word for the proposing author.
    if filters["created_by"] is None:
        filters["created_by"] = source.get("agent")
    return filters


def _selective_filters(body: dict[str, Any], action: str) -> dict[str, Any]:
    """Return the proposal filters of a batch-by-filter review body.

    Raises:
        ValueError: If the body names neither ids nor a single filter — an
            empty body would otherwise mean "review the entire queue", which is
            not something a request should be able to say by accident.
    """
    filters = _proposal_filters(body)
    if not any(value is not None for value in filters.values()):
        raise ValueError(
            f"{action} needs 'ids', or at least one filter "
            f"({', '.join(PROPOSAL_FILTERS)}) — refusing to {action} the whole queue"
        )
    return filters


def _reject_reason(body: dict[str, Any]) -> str:
    """Return the mandatory reject reason.

    Both spellings of a reject record it — one id or a hundred — so this
    surface requires it exactly as ``nodum reject --reason`` does.

    Raises:
        ValueError: If no non-empty reason was given.
    """
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a reject needs a non-empty 'reason': it is recorded in every event")
    return reason


def _id_list(value: Any) -> list[str]:
    """Coerce an ``ids`` body field to a list of strings.

    Raises:
        ValueError: If it is not a list.
    """
    if not isinstance(value, list):
        raise ValueError("'ids' must be a list of node, edge, or version ids")
    return [str(item) for item in value]


# ── Static hosting ────────────────────────────────────────────────────────────


def _web_entry_point() -> Path:
    """Return the page unknown non-API paths fall through to.

    The built bundle's ``index.html`` when it exists, otherwise the tracked
    placeholder — resolved per request, so ``make web-build`` takes effect
    without a restart and a missing (or entirely absent) ``_web/`` directory is
    a page rather than a crash.
    """
    index = WEB_ROOT / "index.html"
    return index if index.is_file() else WEB_PLACEHOLDER


def _web_file(relative: str) -> Path | None:
    """Resolve a request path to a file inside :data:`WEB_ROOT`, or ``None``.

    ``None`` for anything that is not a regular file *inside* the bundle, so
    ``..`` segments and symlinks pointing out of it fall through to the entry
    point instead of serving something they should not.
    """
    if not relative:
        return None
    try:
        root = WEB_ROOT.resolve(strict=True)
        candidate = (root / relative).resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


# ── The app ───────────────────────────────────────────────────────────────────


def create_app(*, db_path: str | Path | None = None, token: str | None = None) -> Starlette:
    """Build the nodum HTTP app: the JSON API plus the human web UI.

    Args:
        db_path: Explicit database path; defaults to ``NODUM_DB`` resolution,
            exactly as every other adapter resolves it.
        token: Optional static bearer token. When set, every ``/api/*`` request
            must carry ``Authorization: Bearer <token>``; when ``None`` there is
            no auth at all, which is the loopback default.

    Returns:
        The configured Starlette application. Bind it to loopback unless a
        token is set: there are no accounts, sessions, or roles here, by design
        (single-user, design §9).
    """

    # ── Health and catalog ────────────────────────────────────────────────

    async def healthz(request: Request) -> Response:
        """Liveness probe — open even when a bearer token is configured."""
        resolved = Path(db_path) if db_path is not None else db.db_path()
        return EnvelopeResponse({"status": "ok", "version": VERSION, "db_path": str(resolved)})

    async def get_types(request: Request) -> Response:
        """The live type catalog (node types and edge types)."""
        return EnvelopeResponse(envelope(service.list_types(path=db_path)))

    async def get_schema(request: Request) -> Response:
        """One node or edge type's catalog entry, including its JSON schema."""
        return EnvelopeResponse(
            envelope(service.get_schema(request.path_params["type"], path=db_path))
        )

    # ── Nodes ─────────────────────────────────────────────────────────────

    async def list_nodes(request: Request) -> Response:
        """List nodes in creation order, optionally filtered."""
        params = request.query_params
        nodes = service.list_nodes(
            type=params.get("type"),
            state=params.get("state"),
            parent_id=_param(params, "parent_id", "parent"),
            limit=_int_param(params, "limit", default=500),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("nodes", nodes))

    async def create_node(request: Request) -> Response:
        """Create a node. It lands ``active``: this is the human surface."""
        body = await _json_body(request)
        node = _write(
            service.create_node,
            type=_required(body, "type"),
            title=body.get("title"),
            content=body.get("content") or "",
            parent_id=body.get("parent_id"),
            props=body.get("props"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(node))

    async def get_node(request: Request) -> Response:
        """One node, or its active-edge neighborhood when ``depth`` is given."""
        node_id = request.path_params["id"]
        depth = _int_param(request.query_params, "depth")
        if depth is not None and depth != 0:
            # Mirrors `nodum node get --depth`: 0 (and absent) is the bare
            # node, and a negative depth reaches the service, which rejects it.
            result = service.get_neighborhood(node_id, depth=depth, path=db_path)
        else:
            result = service.get_node(node_id, path=db_path)
        return EnvelopeResponse(envelope(result))

    async def update_node(request: Request) -> Response:
        """Update the named fields of a node, and only those."""
        body = await _json_body(request)
        fields = {name: body[name] for name in PATCHABLE_FIELDS if name in body}
        if not fields:
            raise ValueError(
                f"nothing to update: send one of {', '.join(repr(f) for f in PATCHABLE_FIELDS)}"
            )
        node = _write(service.update_node, request.path_params["id"], path=db_path, **fields)
        return EnvelopeResponse(envelope(node))

    async def list_children(request: Request) -> Response:
        """A node's children in ``position`` order (the document tree)."""
        nodes = service.list_children(request.path_params["id"], path=db_path)
        return EnvelopeResponse(list_envelope("nodes", nodes))

    async def node_history(request: Request) -> Response:
        """A node's version snapshots, chronological."""
        versions = service.history(request.path_params["id"], path=db_path)
        return EnvelopeResponse(list_envelope("versions", versions))

    async def archive_node(request: Request) -> Response:
        """Retire a node (``active`` → ``archived``) — the service's human tier."""
        node = _write(service.transition, request.path_params["id"], "archive", path=db_path)
        return EnvelopeResponse(envelope(node))

    # ── Edges ─────────────────────────────────────────────────────────────

    async def list_edges(request: Request) -> Response:
        """List edges, optionally filtered by incident node, type, or state."""
        params = request.query_params
        edges = service.list_edges(
            node_id=_param(params, "node_id", "node"),
            type=params.get("type"),
            state=params.get("state"),
            limit=_int_param(params, "limit", default=500),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("edges", edges))

    async def create_edge(request: Request) -> Response:
        """Create a typed, directed edge between two nodes."""
        body = await _json_body(request)
        edge = _write(
            service.create_edge,
            str(_required(body, "src_id")),
            str(_required(body, "dst_id")),
            str(_required(body, "type")),
            props=body.get("props"),
            confidence=body.get("confidence"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(edge))

    # ── Search and link suggestions ───────────────────────────────────────

    async def search(request: Request) -> Response:
        """Hybrid search (BM25 + vector, RRF-fused) with optional graph expansion."""
        params = request.query_params
        result = search_module.search(
            _required_param(params, "q", "query"),
            k=_int_param(params, "limit", "k", default=10),
            state=_state_param(params),
            type=params.get("type"),
            created_by=params.get("created_by"),
            created_after=params.get("created_after"),
            created_before=params.get("created_before"),
            expand=_bool_param(params, "expand"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    async def suggest_links(request: Request) -> Response:
        """Title-prefix candidates for the editor's ``[[`` autocomplete."""
        params = request.query_params
        nodes = service.suggest_links(
            params.get("prefix", ""),
            limit=_int_param(params, "limit", default=20),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("nodes", nodes))

    # ── Graph ─────────────────────────────────────────────────────────────

    async def get_subgraph(request: Request) -> Response:
        """A bounded, filtered neighborhood — the node cap is applied while walking."""
        params = request.query_params
        result = service.subgraph(
            _required_param(params, "root", "root_id"),
            depth=_int_param(params, "depth", default=2),
            edge_types=_list_param(params, "edge_type", "edge_types"),
            edge_states=_list_param(params, "edge_state", "edge_states"),
            min_confidence=_float_param(params, "min_confidence"),
            created_by=params.get("created_by"),
            node_types=_list_param(params, "node_type", "node_types"),
            limit=_int_param(params, "limit", default=200),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    async def get_path(request: Request) -> Response:
        """The shortest active-edge path between two nodes."""
        params = request.query_params
        result = service.find_path(
            _required_param(params, "a"), _required_param(params, "b"), path=db_path
        )
        return EnvelopeResponse(envelope(result))

    # ── Review queue (the human tier) ─────────────────────────────────────

    async def review_queue(request: Request) -> Response:
        """Pending proposals with reviewer context, oldest first."""
        params = request.query_params
        proposals = service.list_proposals(
            **_proposal_filters(params),
            limit=_int_param(params, "limit", default=500),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("proposals", proposals))

    async def review_accept(request: Request) -> Response:
        """Accept proposals by id, or every proposal matching a filter."""
        body = await _json_body(request)
        if "ids" in body:
            result = _write(service.accept_proposals, _id_list(body["ids"]), path=db_path)
        else:
            result = _write(
                service.accept_matching, **_selective_filters(body, "accept"), path=db_path
            )
        return EnvelopeResponse(envelope(result))

    async def review_reject(request: Request) -> Response:
        """Reject proposals by id, or by filter. The reason is mandatory."""
        body = await _json_body(request)
        reason = _reject_reason(body)
        if "ids" in body:
            result = _write(
                service.reject_proposals, _id_list(body["ids"]), reason=reason, path=db_path
            )
        else:
            result = _write(
                service.reject_matching,
                reason=reason,
                **_selective_filters(body, "reject"),
                path=db_path,
            )
        return EnvelopeResponse(envelope(result))

    async def diff_versions(request: Request) -> Response:
        """Unified diff between two versions of one node."""
        params = request.query_params
        a = _int_param(params, "a")
        b = _int_param(params, "b")
        if a is None or b is None:
            raise ValueError("diff needs two version ids: ?a=<id>&b=<id>")
        return EnvelopeResponse(envelope(service.diff_versions(a, b, path=db_path)))

    # ── Policies ──────────────────────────────────────────────────────────

    async def list_policies(request: Request) -> Response:
        """Every stored agent policy."""
        return EnvelopeResponse(list_envelope("policies", service.list_policies(path=db_path)))

    async def get_policy(request: Request) -> Response:
        """One agent's ruleset."""
        policy = service.get_policy(request.path_params["agent"], path=db_path)
        return EnvelopeResponse(envelope(policy))

    async def set_policy(request: Request) -> Response:
        """Replace one agent's ruleset (audited as ``policy.set``; human-only).

        The body is ``{"rules": [...]}`` and nothing else. Disabling a policy
        is ``{"rules": []}`` — the service's only representation of it, and
        there is no ``enabled`` flag to add here: an adapter-invented one would
        wipe a stored ruleset on a value the domain cannot even express.
        """
        body = await _json_body(request)
        rules = body.get("rules")
        if not isinstance(rules, list):
            raise ValueError("'rules' must be a list of rule objects")
        policy = _write(service.set_policy, request.path_params["agent"], rules, path=db_path)
        return EnvelopeResponse(envelope(policy))

    # ── Assets ────────────────────────────────────────────────────────────

    async def upload_asset(request: Request) -> Response:
        """Register an uploaded file as a content-addressed asset.

        The upload is spooled to a temp file and handed to the existing
        ``assets.register_asset``, so this path inherits its sha256 dedup, its
        blob-limit check, and its hash/copy cross-check rather than growing a
        second registration path with weaker guarantees.
        """
        async with request.form() as form:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise ValueError("expected a multipart form with a 'file' part")
            original_name = upload.filename or "upload"
            with tempfile.TemporaryDirectory(prefix="nodum-upload-") as directory:
                spooled = Path(directory) / (Path(original_name).name or "upload")
                with spooled.open("wb") as handle:
                    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                        handle.write(chunk)
                asset = assets.register_asset(spooled, name=original_name, path=db_path)
        return EnvelopeResponse(envelope(asset))

    async def list_assets(request: Request) -> Response:
        """Registered assets, metadata only — the bytes stay in the database."""
        return EnvelopeResponse(list_envelope("assets", assets.list_assets(path=db_path)))

    async def get_asset(request: Request) -> Response:
        """One asset's metadata, by hash or by asset-reference node id."""
        asset = assets.get_asset(request.path_params["id"], path=db_path)
        return EnvelopeResponse(envelope(asset))

    async def get_rendition(request: Request) -> Response:
        """The WebP bytes of an image rendition — generated lazily, cached in the DB.

        Renditions only (design §5.7): ``thumb`` and ``preview`` are the whole
        vocabulary, and originals are never served — here or anywhere else.
        """
        rendition = assets.get_rendition(
            request.path_params["id"], profile=request.path_params["profile"], path=db_path
        )
        payload = assets.read_rendition_bytes(rendition, path=db_path)
        return Response(payload, media_type=assets.RENDITION_MIME)

    # ── Event log, undo, export ───────────────────────────────────────────

    async def list_events(request: Request) -> Response:
        """The append-only event log, newest first."""
        limit = _int_param(request.query_params, "limit", default=50)
        return EnvelopeResponse(
            list_envelope("events", service.list_events(limit=limit, path=db_path))
        )

    async def undo(request: Request) -> Response:
        """Reverse one event (default: the latest reversible one) — human tier."""
        body = await _json_body(request)
        seq = body.get("seq")
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int)):
            raise ValueError(f"'seq' must be an event seq (integer), got {seq!r}")
        result = _write(service.undo, seq, path=db_path)
        return EnvelopeResponse(envelope(result))

    async def export_node(request: Request) -> Response:
        """Download a node — and optionally its neighborhood — as a JSON file."""
        node_id = request.path_params["id"]
        depth = _int_param(request.query_params, "depth", default=0)
        result = service.get_neighborhood(node_id, depth=depth, path=db_path)
        response = EnvelopeResponse(envelope(result))
        safe_id = _SAFE_FILENAME_RE.sub("-", node_id)[:64] or "node"
        response.headers["content-disposition"] = f'attachment; filename="nodum-{safe_id}.json"'
        return response

    # ── Fallbacks ─────────────────────────────────────────────────────────

    async def api_not_found(request: Request) -> Response:
        """Every unmatched ``/api`` path is a JSON 404, never the SPA shell."""
        raise HTTPException(404, f"no such API route: {request.url.path}")

    async def favicon(request: Request) -> Response:
        """Answer ``/favicon.ico`` as an icon request, never as the SPA shell.

        A browser asks for this path on its own, and it is the one non-API path
        that is definitely *not* a client route — so it must not fall through to
        the catch-all, which would return ``index.html`` with a 200 and
        ``text/html``. A client asking for an icon has no way to tell that from
        a real answer.

        The page declares its icon as an inlined SVG data URI, so there is no
        ``.ico`` file to serve and 204 is the honest answer: nothing here, and
        nothing to retry. A real ``favicon.ico`` dropped into the bundle is
        served instead, so adding one later needs no change here.
        """
        icon = _web_file("favicon.ico")
        return FileResponse(icon) if icon is not None else Response(status_code=204)

    async def web_app(request: Request) -> Response:
        """Serve the built UI, falling back to its entry point for client routes.

        A path matching a file inside the bundle is that file; anything else is
        the entry point, because the router that owns ``/graph/:id`` is the one
        in the browser. With no bundle built, that entry point is the
        placeholder page.
        """
        asset_file = _web_file(request.path_params.get("path", ""))
        return FileResponse(asset_file if asset_file is not None else _web_entry_point())

    routes = [
        Route("/healthz", healthz),
        Route("/api/types", get_types),
        Route("/api/schema/{type}", get_schema),
        Route("/api/nodes", list_nodes),
        Route("/api/nodes", create_node, methods=["POST"]),
        Route("/api/nodes/{id}", get_node),
        Route("/api/nodes/{id}", update_node, methods=["PATCH"]),
        Route("/api/nodes/{id}/children", list_children),
        Route("/api/nodes/{id}/history", node_history),
        Route("/api/nodes/{id}/archive", archive_node, methods=["POST"]),
        Route("/api/edges", list_edges),
        Route("/api/edges", create_edge, methods=["POST"]),
        Route("/api/search", search),
        Route("/api/links/suggest", suggest_links),
        Route("/api/graph/subgraph", get_subgraph),
        Route("/api/graph/path", get_path),
        Route("/api/review/queue", review_queue),
        Route("/api/review/accept", review_accept, methods=["POST"]),
        Route("/api/review/reject", review_reject, methods=["POST"]),
        Route("/api/diff", diff_versions),
        Route("/api/policies", list_policies),
        Route("/api/policies/{agent}", get_policy),
        Route("/api/policies/{agent}", set_policy, methods=["PUT"]),
        Route("/api/assets", list_assets),
        Route("/api/assets", upload_asset, methods=["POST"]),
        Route("/api/assets/{id}", get_asset),
        Route("/api/assets/{id}/rendition/{profile}", get_rendition),
        Route("/api/events", list_events),
        Route("/api/undo", undo, methods=["POST"]),
        Route("/api/export/node/{id}", export_node),
        # Order matters from here: an unmatched /api path is a JSON 404, then
        # /favicon.ico is answered as an icon rather than as a document, and
        # only then does everything else fall through to the single-page app.
        Route("/api/{path:path}", api_not_found, methods=ALL_METHODS),
        Route("/favicon.ico", favicon),
        Route("/{path:path}", web_app),
    ]

    exception_handlers: dict[Any, Any] = {
        exception: _exception_handler(status) for exception, status in EXCEPTION_STATUS.items()
    }
    exception_handlers[HTTPException] = _http_exception_handler
    exception_handlers[Exception] = _server_error_handler

    middleware = [Middleware(BearerTokenMiddleware, token=token)] if token is not None else []

    return Starlette(routes=routes, middleware=middleware, exception_handlers=exception_handlers)
