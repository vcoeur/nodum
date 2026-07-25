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
endpoint inherits the guarantee by construction. ``tests/test_http_api.py``
enforces it from two directions: an adversarial sweep that drives **every route
in the live route table** with actor-carrying bodies and then asserts the event
log and every row written during the sweep name ``human`` — which catches a
rogue endpoint however it reaches the service — and AST properties over this
module (one ``actor=`` binding, no import of an actor-taking service function
under any name, no ``getattr`` on an adapter module, no unreviewed ``**``
unpack). The service layer's own human tier (``_require_human_reviewer`` over
``HUMAN_ONLY_ACTIONS``) then applies as it does to every other surface — this
module re-implements none of it.

Everything else is convention, shared rather than re-stated:

* **Envelope** — :mod:`nodum.envelope`, the same helper the CLI prints
  through, so ``GET /api/nodes/{id}`` and ``nodum node get <id>`` emit the same
  bytes.
* **Errors** — one :data:`EXCEPTION_STATUS` table installed as Starlette
  exception handlers. It covers every class ``cli._run`` catches (``sqlite3``
  failures and ``OSError`` included, by base class) plus the ones only a
  network surface can meet, and echoes the CLI's one-line message as
  ``{"error": {"type", "message"}}``. Anything unmapped is a 500 with a generic
  message; the traceback goes to the server log, never into a response body.
* **Auth is not the same thing as origin control.** ``nodum serve`` binds
  loopback and an optional static bearer token gates ``/api/*``; but loopback
  is reachable from every page the browser loads, so a bind is no defence
  against a *browser*. :class:`RequestGuardMiddleware` is: it validates the
  ``Host`` header against the names this server answers to (DNS rebinding),
  proves a state-changing request is same-origin before it can reach a handler
  (CSRF), and enforces the content type each route class accepts, so that no
  cross-origin request a browser can make without a preflight — and this app
  answers no preflight — can reach a write. See the class docstring for the
  full rule and what it deliberately does not cover.
* **Static hosting** — the built Vite bundle at ``nodum/_web/`` is served at
  ``/``, with unknown non-API paths falling through to its ``index.html`` so
  client-side routes survive a reload. When the bundle is absent (a source
  checkout that never ran ``make web-build``, or a directory Vite has just
  emptied) the tracked ``nodum/_web_placeholder.html`` is served instead — a
  page that says what to run, not a crash on a missing directory.
  ``/favicon.ico`` is the one path exempted from that fall-through: a browser
  asks for it unprompted, and answering an icon request with an HTML document
  under a 200 is a lie the client cannot detect (see :func:`create_app`).
  Everything this route serves carries :data:`CONTENT_SECURITY_POLICY`, which
  is the runtime backstop under the preview's Markdown sanitiser: node content
  is written by agents and rendered in this origin, which is also the origin
  that may write to the API.

**What this surface still does not defend against, on purpose.** Every local
process shares the loopback interface, so anything that can open a socket on
this port can drive this API *as the human* — including an MCP server launched
with ``--actor agent:x``, which would thereby regain the ``accept`` the MCP
tool list structurally withholds. Origin control stops browsers, not
processes; only ``--token`` (a secret the local agent does not hold) does.
``nodum serve`` says so out loud at startup rather than leaving it implicit.

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
from collections.abc import Iterable, Sequence
from http import HTTPStatus
from importlib import metadata
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import Headers, QueryParams, UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nodum import assets, service
from nodum import search as search_module
from nodum.assets import (
    AssetNotFound,
    AssetSourceChanged,
    AssetTooLarge,
    ImageTooLarge,
    UnsupportedRendition,
)
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

#: Content-Security-Policy for the web UI — the runtime half of the answer to
#: "the preview renders content agents wrote". The sanitiser
#: (``web/src/views/editor/markdownRender.ts``) is the first line; this is what
#: still holds if a payload gets past it.
#:
#: Each directive, and what in the app it had to accommodate:
#:
#: * ``script-src 'self'`` — Vite emits the bundle as external module scripts
#:   and inlines nothing, so no ``'unsafe-inline'`` and no nonce plumbing. There
#:   is no ``eval`` or ``new Function`` in the build either, so no
#:   ``'unsafe-eval'``. **This is the directive that matters**; weakening it
#:   would make the rest ceremonial.
#: * ``style-src 'self' 'unsafe-inline'`` — the concession. CodeMirror injects
#:   its theme as ``<style>`` elements at runtime and mermaid inlines a
#:   ``<style>`` block into every diagram it draws; neither can be nonced from
#:   here. It costs CSS injection as a residual, not script execution.
#: * ``img-src 'self' data:`` — ``data:`` is for the inlined SVG favicon in the
#:   page head. Renditions are ordinary same-origin ``/api`` responses.
#: * ``connect-src 'self'`` — every ``fetch`` goes to this origin's ``/api``.
#: * ``default-src 'self'`` with ``object-src``, ``frame-src``, ``worker-src``
#:   and ``manifest-src`` at ``'none'`` — the app has no plugins, frames or
#:   workers, so the absence is worth stating rather than inheriting.
#: * ``base-uri 'none'`` and ``form-action 'none'`` — an injected ``<base>`` or
#:   form post is not a fetch and would otherwise be uncovered.
#: * ``frame-ancestors 'none'`` — this page must not be framed. Header-only:
#:   a ``<meta>`` CSP cannot express it, which is part of why the policy is
#:   served from here.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "manifest-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

#: Read size for spooling a multipart upload to disk — the same 1 MiB chunk
#: :mod:`nodum.assets` streams blobs with, so a large file is never held whole.
UPLOAD_CHUNK_BYTES = 1 << 20

#: Hard ceiling on a request body, enforced *before* anything buffers it
#: (:class:`RequestGuardMiddleware`). It bounds the multipart upload path, which
#: is the only route that takes real bulk, and every JSON body with it.
#:
#: The old ceiling was SQLite's 1 GB blob limit, checked inside
#: ``assets.register_asset`` — after Starlette had spooled the whole part to
#: disk *and* the handler had copied it to a second temp file. Tripping it
#: needed >2 GB of ``/tmp`` first, which makes it a disk-exhaustion primitive
#: rather than a limit. This value is what the server is willing to *read*;
#: ``AssetTooLarge`` remains the storage-layer backstop under it.
MAX_REQUEST_BYTES = 32 * 1024 * 1024

#: MIME types ``POST /api/assets`` accepts, sniffed from the bytes rather than
#: taken from the filename or the client's ``Content-Type``.
#:
#: Deliberately narrower than what ``assets.register_asset`` will store: the
#: CLI registers a local file the operator already owns and Phase-4 ingestion
#: will want documents, but the *network* surface should only accept what this
#: system can actually do something with, which today is raster images
#: (renditions, design §5.7). ``.exe``, ``.html`` and ``.iso`` were all stored
#: happily before this list existed. SVG is excluded on purpose — it is a
#: script-bearing document that Pillow cannot render anyway.
UPLOAD_MIME_ALLOWLIST = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
)

#: Methods that do not change state, and so carry no CSRF risk worth gating.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The content type every JSON ``/api`` route requires on a state-changing
#: request — including the ones that take no body at all.
#:
#: This is the structural half of the CSRF answer: ``application/json`` is not a
#: CORS-*simple* content type, so a cross-origin page cannot send it without a
#: preflight, and this app answers no preflight. A form with
#: ``enctype="text/plain"`` — which needs no preflight and used to reach
#: ``/api/review/accept`` — cannot get past this line.
JSON_CONTENT_TYPE = "application/json"

#: The one route class whose content type *is* CORS-simple, because multipart
#: is the only way to upload a file. It cannot be protected by content type, so
#: it leans entirely on the same-origin proof below.
MULTIPART_CONTENT_TYPE = "multipart/form-data"
MULTIPART_ROUTES = frozenset({"/api/assets"})

#: Header a **non-browser** client sets to say so, required on a state-changing
#: request that carries neither ``Origin`` nor ``Sec-Fetch-Site``.
#:
#: Browsers send at least one of those two on every write and cannot be talked
#: out of it — both are forbidden header names, unsettable from JavaScript — so
#: this header is never the thing that lets a browser through. A cross-origin
#: page cannot set a custom header without a preflight either. It exists so that
#: "no origin headers at all" is an explicit claim a caller makes rather than a
#: free pass anything gets by omission.
CLIENT_HEADER = "x-nodum-client"

#: ``Sec-Fetch-Site`` values that prove a browser request came from this origin.
#: ``none`` is a user-initiated navigation (typed URL, bookmark) — no page
#: initiated it, so no page can forge it.
SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})

#: Host names a loopback-bound server answers to. Ports are deliberately not
#: compared: the ``make web-dev`` proxy forwards the browser's own
#: ``Host: localhost:5173``, and a port is no part of the rebinding defence
#: anyway — an attacker who rebinds ``evil.example`` to 127.0.0.1 still has to
#: send ``Host: evil.example``.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: Addresses whose *bind* reaches only this machine. Not the same set as
#: :data:`LOOPBACK_HOSTS`: ``http://0.0.0.0:8420`` typed into a browser resolves
#: to loopback and is a fine ``Host`` value, while ``--host 0.0.0.0`` binds
#: every interface and puts the API on the network.
LOOPBACK_BIND_ADDRESSES = frozenset({"localhost", "127.0.0.1", "::1"})

#: ``--allow-host`` value that turns the ``Host``/``Origin`` check off. Explicit
#: on purpose: there is no way to reach this state by accident.
ANY_HOST = "*"

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


class PayloadTooLarge(Exception):
    """Raised from the wrapped ``receive`` when a body passes :data:`MAX_REQUEST_BYTES`.

    Raised *mid-read*, so the bytes past the limit are never buffered — the
    point of the class. ``Content-Length`` is checked first where a client
    supplies one, but it is client-supplied and cannot be the only guard.
    """


#: Exception → HTTP status. It covers every class ``cli._run`` catches — the
#: ``sqlite3`` and ``OSError`` rows are the **base** classes, so every
#: ``DatabaseError``/``IntegrityError``/``ProgrammingError``/``DataError`` lands
#: on a status instead of a generic 500 — plus the failures only a network
#: surface can meet (an oversized body, a client that hung up). A failure
#: therefore reads the same on both surfaces and the status is the HTTP
#: translation of the CLI's exit 1.
#:
#: Lookup is by MRO, so the subclasses listed here win over the base they
#: inherit from: ``InvalidTransition``/``UndoNotPossible`` over ``ValueError``,
#: and ``sqlite3.OperationalError`` over ``sqlite3.Error``.
EXCEPTION_STATUS: dict[type[Exception], int] = {
    # 404 — an id, name, or seq that resolves to nothing. RecordNotFound covers
    # node/edge/version ids and the transitions that accept all three.
    RecordNotFound: 404,
    TypeNotFound: 404,
    EventNotFound: 404,
    PolicyNotFound: 404,
    AssetNotFound: 404,
    # 400 — the request itself is wrong: a bad value, an impossible transition,
    # an asset that cannot be stored or rendered. OverflowError is a caller's
    # integer that no SQLite parameter can hold (`?limit=9999…`), which reached
    # the driver as a Python bignum and surfaced as a 500 before it was mapped.
    ValueError: 400,
    InvalidTransition: 400,
    AssetTooLarge: 400,
    AssetSourceChanged: 400,
    UnsupportedRendition: 400,
    ImageTooLarge: 400,
    OverflowError: 400,
    # 403 — the human tier refused a non-human actor. Unreachable from this
    # surface by construction; mapped so it could never surface as a 500.
    ReviewNotPermitted: 403,
    # 409 — the graph has grown past the event being undone.
    UndoNotPossible: 409,
    # 413 — the body passed the ceiling this server is willing to read.
    PayloadTooLarge: 413,
    # 499 — the client hung up mid-body (a cancelled upload). Nothing will read
    # this status; it exists so the case is a mapped outcome rather than an
    # unhandled traceback per cancelled upload in the server log.
    ClientDisconnect: 499,
    # 500 — the server's own storage failed. `sqlite3.Error` is the base class,
    # so `DatabaseError` ("file is not a database"), `IntegrityError`,
    # `ProgrammingError` and `DataError` all land here rather than on the
    # generic 500, and carry the CLI's `database error: …` line with them.
    sqlite3.Error: 500,
    # `cli._run` catches OSError too (an unreadable database file, a full
    # disk). Its message is rewritten without the filename: on the CLI that
    # path is the operator's own, here it is a stranger's.
    OSError: 500,
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


def _failure_message(exc: Exception) -> str:
    """Render one exception as the single line both surfaces report it with.

    ``database error: …`` is what the CLI prints for a SQLite failure. An
    ``OSError`` is the one deliberate divergence: ``cli._run`` appends the
    filename, and this surface must not — the path is the operator's on a
    terminal and a stranger's over a socket.
    """
    if isinstance(exc, sqlite3.Error):
        return f"database error: {exc}"
    if isinstance(exc, OSError):
        return f"storage error: {exc.strerror or type(exc).__name__}"
    return str(exc)


def _exception_handler(status_code: int) -> Any:
    """Build the handler installed for one mapped exception class."""

    async def handler(request: Request, exc: Exception) -> Response:
        return _error(status_code, type(exc).__name__, _failure_message(exc))

    return handler


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """Render Starlette's own errors (404, 405, the 401 below) in the error envelope."""
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", str(exc))
    response = _error(status_code, HTTPStatus(status_code).phrase.replace(" ", ""), detail)
    # A 405 is only useful with the Allow header that says which verbs the path
    # does take, so headers set on the exception survive into the envelope.
    for name, value in (getattr(exc, "headers", None) or {}).items():
        response.headers[name] = value
    return response


async def _server_error_handler(request: Request, exc: Exception) -> Response:
    """Return the generic 500 body; Starlette re-raises so the server logs the traceback."""
    return EnvelopeResponse({"error": INTERNAL_ERROR}, status_code=500)


# ── Origin control, body limits, and auth ─────────────────────────────────────


def _is_api_path(path: str) -> bool:
    """Is this the path of an ``/api`` route?

    The one place that answers the question, so the gate and the gated cannot
    key on different things. Paths are normalised (:func:`_normalise_path`)
    before anything asks, so ``//api/nodes`` is an API path here *and* at the
    router — previously it was neither and the two disagreed for the wrong
    reason.
    """
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


def _normalise_path(scope: Scope) -> None:
    """Collapse repeated slashes in a request path, in place.

    ``//api/nodes`` reached the router as a non-route (falling through to the
    SPA) while the auth middleware's ``startswith("/api")`` counted it as an API
    path. Nothing leaked, but a gate and a router that disagree about what they
    are talking about is a bug waiting for its second half; normalising once, at
    the outermost layer, removes the disagreement rather than patching one side.
    """
    path = scope.get("path", "")
    if "//" in path:
        collapsed = re.sub(r"/{2,}", "/", path)
        scope["path"] = collapsed
        if "raw_path" in scope:
            scope["raw_path"] = collapsed.encode("latin-1")


def bare_host(value: str) -> str:
    """Return the bare host name of a ``Host``-style header value, lowercased.

    Strips a port and the brackets of an IPv6 literal, so ``[::1]:8420`` and
    ``::1`` compare equal.
    """
    host = value.strip().lower()
    if host.startswith("["):
        return host.partition("]")[0].removeprefix("[")
    return host.rpartition(":")[0] if host.count(":") == 1 else host


def _origin_host(value: str) -> str | None:
    """Return the host name of an ``Origin`` header, or ``None`` if it has none.

    ``null`` (a sandboxed iframe, a ``data:`` document) has no host and is never
    this server's origin, so it comes back as the empty string and fails the
    allowlist rather than being read as "absent".
    """
    origin = value.strip()
    if not origin or origin == "null":
        return ""
    _, _, remainder = origin.partition("://")
    return bare_host(remainder or origin)


def resolve_allowed_hosts(host: str, extra: Iterable[str] | None = None) -> frozenset[str]:
    """Build the host allowlist for a server bound to ``host``.

    A loopback bind answers to every loopback spelling (and to
    ``make web-dev``'s proxied ``Host: localhost:5173``, since ports are not
    compared). A non-loopback bind answers to the address it was given, and
    anything else — a DNS name in front of it, a reverse proxy — has to be named
    with ``--allow-host``.

    Args:
        host: The address ``nodum serve`` binds. Empty means "the default
            bind", which is loopback.
        extra: Additional accepted host names; ``"*"`` disables the check.

    Returns:
        The lowercased host names this server will answer to, or a set
        containing :data:`ANY_HOST` when the check is disabled.
    """
    extras = {name.strip().lower() for name in extra or () if name.strip()}
    if ANY_HOST in extras:
        return frozenset({ANY_HOST})
    bound = bare_host(host)
    allowed = set(LOOPBACK_HOSTS) if not bound or bound in LOOPBACK_HOSTS else {bound}
    return frozenset(allowed | extras)


class RequestGuardMiddleware:
    """Reject requests a browser on another origin could have made, and cap body size.

    ``nodum serve`` binds loopback with no token by default, and **loopback is
    reachable from every page the user visits**. Nothing about the bind stops
    ``https://evil.example`` from posting to ``http://127.0.0.1:8420``; the
    absence of CORS headers only stops it *reading the reply*, which a CSRF
    attacker does not need. This middleware is what stops the write landing.

    Four checks, outermost-first, each closing a different door:

    1. **``Host``** (every request, every method). The header's host name must
       be one this server answers to. This is the DNS-rebinding defence and the
       only one that matters for *reads*: after a rebind the attacker's page is
       same-origin by every other measure, so ``Origin`` and ``Sec-Fetch-Site``
       both say "same-origin" and mean it. Only the name in ``Host`` still says
       ``evil.example``.
    2. **Same-origin proof** (state-changing methods). The request must carry
       ``Sec-Fetch-Site`` in :data:`SAME_ORIGIN_FETCH_SITES`, *or* an ``Origin``
       whose host is allowed, *or* :data:`CLIENT_HEADER`. The first two are
       forbidden header names — script cannot set or suppress them — and the
       third needs a preflight this app never answers. A mismatched ``Origin``
       or a cross-site ``Sec-Fetch-Site`` is refused outright even if another
       signal would have passed it.
    3. **Content type** (state-changing methods on ``/api``). JSON routes
       require ``application/json``, which is not CORS-simple, so a form post
       cannot reach them at all — including the bodyless ones, where the check
       is the whole defence in depth. :data:`MULTIPART_ROUTES` require
       multipart, which *is* CORS-simple and therefore rests on check 2 alone.
    4. **Body size**. ``Content-Length`` is checked before a byte is read, and
       the stream is then capped mid-read regardless of what that header
       claimed.

    What it does **not** do: authenticate. Any local process can satisfy every
    check above with three ``curl`` headers. That is what ``--token`` is for,
    and ``nodum serve`` says so at startup.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str], max_body_bytes: int) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.max_body_bytes = max_body_bytes

    def _host_allowed(self, host: str) -> bool:
        return ANY_HOST in self.allowed_hosts or host in self.allowed_hosts

    def _refuse(self, scope: Scope, headers: Headers) -> Response | None:
        """Return the refusal for this request, or ``None`` to let it through."""
        host = bare_host(headers.get("host", ""))
        if not self._host_allowed(host):
            return _error(
                400,
                "UntrustedHost",
                f"this server does not answer to host {host or '(missing)'!r} — "
                "pass --allow-host if it should",
            )

        if scope["method"] in SAFE_METHODS:
            return None

        origin = headers.get("origin")
        fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
        if fetch_site and fetch_site not in SAME_ORIGIN_FETCH_SITES:
            return _error(403, "CrossOriginRequest", f"refused a {fetch_site} write request")
        if origin is not None and not self._host_allowed(_origin_host(origin) or ""):
            return _error(403, "CrossOriginRequest", f"refused a write from origin {origin!r}")
        if not fetch_site and origin is None and not headers.get(CLIENT_HEADER):
            return _error(
                403,
                "CrossOriginRequest",
                "a write with no 'Origin' and no 'Sec-Fetch-Site' must say it is not a "
                f"browser: send the '{CLIENT_HEADER}' header",
            )

        return self._refuse_content_type(scope, headers)

    def _refuse_content_type(self, scope: Scope, headers: Headers) -> Response | None:
        """Refuse a state-changing ``/api`` request whose content type is wrong."""
        if not _is_api_path(scope["path"]):
            return None
        expected = (
            MULTIPART_CONTENT_TYPE if scope["path"] in MULTIPART_ROUTES else JSON_CONTENT_TYPE
        )
        presented = headers.get("content-type", "").partition(";")[0].strip().lower()
        if presented != expected:
            return _error(
                415,
                "UnsupportedMediaType",
                f"{scope['method']} {scope['path']} requires 'Content-Type: {expected}', "
                f"got {presented or '(none)'!r}",
            )
        return None

    def _limited(self, receive: Receive) -> Receive:
        """Wrap ``receive`` so the body cannot exceed :attr:`max_body_bytes`.

        The cap has to bite here rather than in the handler: by the time a
        handler sees an ``UploadFile``, Starlette has already spooled every byte
        of it to disk.
        """
        read = 0

        async def limited_receive() -> Message:
            nonlocal read
            message = await receive()
            if message["type"] == "http.request":
                read += len(message.get("body", b""))
                if read > self.max_body_bytes:
                    raise PayloadTooLarge(
                        f"request body exceeds the {self.max_body_bytes}-byte limit"
                    )
            return message

        return limited_receive

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Normalise the path, refuse what must be refused, cap the rest."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        _normalise_path(scope)
        headers = Headers(scope=scope)
        refusal = self._refuse(scope, headers)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
            # Cheap and early, but client-supplied: the streaming cap below is
            # what actually holds, and it does not trust this number.
            response = _error(
                413,
                "PayloadTooLarge",
                f"request body exceeds the {self.max_body_bytes}-byte limit",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, self._limited(receive), send)


class BearerTokenMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``/api/*``.

    Installed only when a token is configured (``nodum serve --token``), which
    is the LAN case and the only defence against *local* processes; the default
    loopback bind has no auth at all. ``/healthz`` and the static UI stay open
    either way — a liveness probe that needs credentials is not a liveness
    probe, and the page that *holds* the token cannot itself require it.

    The comparison is over bytes. Starlette decodes headers as latin-1, and
    ``secrets.compare_digest`` refuses two ``str`` values unless both are ASCII,
    so ``Authorization: Bearer café`` used to raise ``TypeError`` — an
    unauthenticated, endlessly repeatable 500 with a traceback per request, on
    exactly the deployment that configured a token.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject unauthenticated API requests; pass everything else through."""
        if scope["type"] == "http" and _is_api_path(scope["path"]):
            presented = Headers(scope=scope).get("authorization", "").encode("latin-1")
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


def _bounded_int(value: int, label: str) -> int:
    """Return an integer SQLite can actually bind.

    Python integers are unbounded and SQLite's are 64-bit, so ``?limit=9`` × 23
    parsed fine and then failed inside the driver as an ``OverflowError`` — a
    500 on four endpoints for what is plainly a bad parameter. Rejecting the
    value where it is read makes it the 400 it always was.

    Raises:
        ValueError: If the value does not fit in a signed 64-bit integer.
    """
    if not -(2**63) <= value < 2**63:
        raise ValueError(f"{label} is out of range: {value}")
    return value


def _int_param(params: QueryParams, *names: str, default: int | None = None) -> Any:
    """Parse an integer query parameter, falling back to ``default``.

    Raises:
        ValueError: If the value is not an integer, or does not fit in 64 bits.
    """
    raw = _param(params, *names)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{names[0]} must be an integer, got {raw!r}") from exc
    return _bounded_int(value, names[0])


def _float_param(params: QueryParams, name: str) -> float | None:
    """Parse an optional float query parameter.

    ``inf`` and ``nan`` are refused: SQLite stores them, and a ``nan``
    comparison silently matches nothing rather than filtering.

    Raises:
        ValueError: If the value is not a finite number.
    """
    raw = params.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return value


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


# ── Upload policy ─────────────────────────────────────────────────────────────


def _refuse_unsupported_upload(spooled: Path, original_name: str) -> None:
    """Refuse an upload this system cannot store or render, before it is stored.

    The type is decided by the *bytes*, never by ``original_name`` or the
    client's declared ``Content-Type``: both are attacker-chosen, and
    ``register_asset`` records the MIME that ``mimetypes.guess_type`` derives
    from the name — so a renamed executable used to be stored as ``image/png``.

    Args:
        spooled: The temp file holding the uploaded bytes.
        original_name: The client's filename, used only in the error message.

    Raises:
        UnsupportedRendition: If the bytes are not one of
            :data:`UPLOAD_MIME_ALLOWLIST`.
        ImageTooLarge: If the image's pixel count is above what a rendition may
            decode.
    """
    sniffed = assets.sniff_image_mime(spooled)
    if sniffed not in UPLOAD_MIME_ALLOWLIST:
        raise UnsupportedRendition(
            f"{original_name!r} is {sniffed or 'not a recognised image'}; this API stores "
            f"images only ({', '.join(sorted(UPLOAD_MIME_ALLOWLIST))})"
        )
    assets.check_image_pixel_budget(spooled)


# ── Routing ───────────────────────────────────────────────────────────────────


def _partial_match_methods(routes: Iterable[Route], scope: Scope) -> set[str]:
    """Return the methods real routes accept on this path, if any accept the path.

    A :class:`~starlette.routing.Match` of ``PARTIAL`` is Starlette's way of
    saying "the path is mine, the method is not" — exactly the set the catch-all
    needs to turn a misleading 404 into a 405 plus an ``Allow`` header.
    """
    allowed: set[str] = set()
    for route in routes:
        match, _ = route.matches(scope)
        if match is Match.PARTIAL:
            allowed |= route.methods or set()
    return allowed


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


def create_app(
    *,
    db_path: str | Path | None = None,
    token: str | None = None,
    allowed_hosts: Sequence[str] | frozenset[str] | None = None,
    max_body_bytes: int | None = None,
) -> Starlette:
    """Build the nodum HTTP app: the JSON API plus the human web UI.

    Args:
        db_path: Explicit database path; defaults to ``NODUM_DB`` resolution,
            exactly as every other adapter resolves it.
        token: Optional static bearer token. When set, every ``/api/*`` request
            must carry ``Authorization: Bearer <token>``; when ``None`` there is
            no auth at all, which is the loopback default.
        allowed_hosts: Host names this server answers to (see
            :func:`resolve_allowed_hosts`). Defaults to the loopback set, which
            is what the default bind serves.
        max_body_bytes: Ceiling on a request body; defaults to
            :data:`MAX_REQUEST_BYTES`.

    Returns:
        The configured Starlette application. Bind it to loopback unless a
        token is set: there are no accounts, sessions, or roles here, by design
        (single-user, design §9), and origin control keeps *browsers* out, not
        other processes on the machine.
    """
    hosts = frozenset(allowed_hosts) if allowed_hosts is not None else resolve_allowed_hosts("")
    body_limit = MAX_REQUEST_BYTES if max_body_bytes is None else max_body_bytes

    # ── Health and catalog ────────────────────────────────────────────────

    async def healthz(request: Request) -> Response:
        """Liveness probe — open even when a bearer token is configured.

        Reports liveness and nothing else. It used to report the absolute
        database path, which is unauthenticated disclosure of a username and a
        filesystem layout on exactly the deployment that set ``--token`` to
        avoid disclosing anything. ``nodum serve`` prints the path at startup,
        where the operator is the only reader.
        """
        return EnvelopeResponse({"status": "ok", "version": VERSION})

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
        """A bounded, filtered neighborhood — node and edge caps both applied while walking."""
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

        Three checks happen before the bytes are offered to the store, and each
        one exists because the version without it was exploitable:

        * **Size** is bounded by :class:`RequestGuardMiddleware` *before*
          Starlette buffers the part. ``AssetTooLarge`` was the only limit
          before, and it fired after the whole body had been spooled once by
          the parser and copied a second time by this handler — a 400 MB upload
          measured 839 MB of ``/tmp``, and tripping the real 1 GB limit needed
          more than 2 GB of it.
        * **Type** is sniffed from the bytes (:func:`assets.sniff_image_mime`),
          not read off the filename, so a renamed ``.exe`` is refused rather
          than stored under ``image/png``.
        * **Pixel count** is read from the image header, so a 612 KB PNG that
          decodes to 14000×14000 is refused here rather than raising
          ``DecompressionBombError`` out of the rendition endpoint as a 500.
        """
        async with request.form(max_files=1, max_fields=1) as form:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise ValueError("expected a multipart form with a 'file' part")
            original_name = upload.filename or "upload"
            with tempfile.TemporaryDirectory(prefix="nodum-upload-") as directory:
                spooled = Path(directory) / (Path(original_name).name or "upload")
                with spooled.open("wb") as handle:
                    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                        handle.write(chunk)
                _refuse_unsupported_upload(spooled, original_name)
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
        if seq is not None:
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise ValueError(f"'seq' must be an event seq (integer), got {seq!r}")
            _bounded_int(seq, "seq")
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
        """Every unmatched ``/api`` path is a JSON 404 — but a wrong verb is a 405.

        This route exists so a ``fetch`` never gets HTML back, and it has to
        claim every method to do that. Claiming every method also means it
        *fully* matches a real route's path under the wrong verb, and a full
        match beats the partial one the real route offers — so ``DELETE
        /api/nodes`` and ``GET /api/undo`` both came back "no such API route",
        which is not true and sends a client looking for a typo that is not
        there. Asking the real routes what they would have matched restores the
        405, with the ``Allow`` header that makes it useful.
        """
        allowed = _partial_match_methods(api_routes, request.scope)
        if allowed:
            raise HTTPException(
                405,
                f"{request.method} is not allowed on {request.url.path}",
                headers={"allow": ", ".join(sorted(allowed))},
            )
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

        Every response from this route carries
        :data:`CONTENT_SECURITY_POLICY` — the document itself, and the bundle's
        own assets, which a browser will happily treat as documents if a client
        navigates straight to one.
        """
        asset_file = _web_file(request.path_params.get("path", ""))
        return FileResponse(
            asset_file if asset_file is not None else _web_entry_point(),
            headers={"content-security-policy": CONTENT_SECURITY_POLICY},
        )

    api_routes = [
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
    ]

    routes = [
        Route("/healthz", healthz),
        *api_routes,
        # Order matters from here: an unmatched /api path is a JSON 404 (or a
        # 405 when only the verb was wrong), then /favicon.ico is answered as an
        # icon rather than as a document, and only then does everything else
        # fall through to the single-page app.
        Route("/api/{path:path}", api_not_found, methods=ALL_METHODS),
        Route("/favicon.ico", favicon),
        Route("/{path:path}", web_app),
    ]

    exception_handlers: dict[Any, Any] = {
        exception: _exception_handler(status) for exception, status in EXCEPTION_STATUS.items()
    }
    exception_handlers[HTTPException] = _http_exception_handler
    exception_handlers[Exception] = _server_error_handler

    # Outermost first: the guard normalises the path every inner layer keys on,
    # and refuses cross-origin and oversized requests before auth even looks at
    # them. An unauthenticated attacker learning "wrong origin" instead of
    # "wrong token" tells them nothing they did not already know.
    middleware = [
        Middleware(RequestGuardMiddleware, allowed_hosts=hosts, max_body_bytes=body_limit)
    ]
    if token is not None:
        middleware.append(Middleware(BearerTokenMiddleware, token=token))

    return Starlette(routes=routes, middleware=middleware, exception_handlers=exception_handlers)
