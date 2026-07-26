"""The HTTP adapter (design §9) — a thin Starlette front over the service layer.

This surface is for the **human**, and it is the exact inverse of
:mod:`nodum.mcp_server`. The MCP server authenticates an agent token and
forces an ``agent:<name>`` principal; here the identity is the human behind
an authenticated **session** — and *nothing a request carries can change
that*.

**How the identity boundary is structural.** There is exactly one expression
in this module that binds a service function's ``principal`` argument for a
write, and it lives in :func:`_write`; read handlers bind theirs through the
same :func:`_session_principal`. That function reads only what
:class:`SessionMiddleware` verified into the request scope and raises when
it is absent — so there is no principal without a verified session, and no
route handler can read an identity from a body, a header, or a query string,
because none of them mentions the concept. Request data is never forwarded
wholesale either: every handler picks the fields it uses by name, and a new
endpoint inherits the guarantee by construction.
``tests/test_http_api.py`` enforces it from two directions: an adversarial
sweep that drives **every route in the live route table** with
actor-carrying bodies behind a second human's session and then asserts the
event log and every row written during the sweep name that human — which
catches a rogue endpoint however it reaches the service — and AST properties
over this module (every ``principal=`` mints through
``_session_principal(request)``, no import of a write service function under
any name, no ``getattr`` on an adapter module, no unreviewed ``**`` unpack,
no trusted-local ``auth`` entry point). The service layer's own grant
enforcement then applies as it does to every other surface — this module
re-implements none of it.

Everything else is convention, shared rather than re-stated:

* **Envelope** — :mod:`nodum.envelope`, the same helper the CLI prints
  through, so ``GET /api/nodes/{id}`` and ``nodum node get <id>`` emit the
  same bytes.
* **Errors** — one :data:`EXCEPTION_STATUS` table installed as Starlette
  exception handlers. It covers every class ``cli._run`` catches (``sqlite3``
  failures and ``OSError`` included, by base class) plus the ones only a
  network surface can meet, and echoes the CLI's one-line message as
  ``{"error": {"type", "message"}}``. Anything unmapped is a 500 with a
  generic message; the traceback goes to the server log, never into a
  response body.
* **Auth is not the same thing as origin control.** Password login
  (``POST /api/login``) verifies an argon2id hash, creates a server-side
  session row (30-day sliding expiry) and sets an ``HttpOnly;
  SameSite=Strict`` cookie; :class:`SessionMiddleware` resolves that cookie
  to the human's principal on every ``/api`` request, and only ``/healthz``,
  ``/api/login`` and the static UI stay open. Sessions stop *processes*
  without the password; they do nothing against a *browser* on another
  origin reaching for this port, which is what
  :class:`RequestGuardMiddleware` is: it validates the ``Host`` header
  against the names this server answers to (DNS rebinding), proves a
  state-changing request is same-origin before it can reach a handler
  (CSRF), and enforces the content type each route class accepts, so that no
  cross-origin request a browser can make without a preflight — and this app
  answers no preflight — can reach a write. See the class docstring for the
  full rule and what it deliberately does not cover.
* **The two capability-URL routes are the one thing on this surface that is
  not a session** (:func:`_is_capability_path`, and note the *why* written
  there before touching either gate). ``GET /api/download/{token}`` and
  ``PUT /api/uploads/{token}`` are redeemed by an agent host that has no
  filesystem in common with this server and no account here at all: the
  unguessable token in the path *is* the authorisation, minted by
  :mod:`nodum.urls` against a principal the session gate already checked,
  single-use, and expiring in minutes. They therefore sit outside the
  session gate *and* outside the origin and content-type gates, while the
  ``Host`` check and the body ceiling still apply to them exactly as to
  everything else. Neither of them ever calls :func:`_session_principal`,
  and neither writes to the graph: the identity a redemption is recorded
  under is the token row's own ``created_by``, applied inside
  :func:`nodum.urls.consume` where it is stored state rather than anything
  the request said.
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

**What this surface still does not defend against, on purpose.** Login is
the whole boundary: any process that can open a socket on this port may
*attempt* one, so the strength of the human's password is the strength of
the defence. ``nodum serve`` says so at startup rather than leaving it
implicit.

Handlers call the service inline rather than through a thread pool: the service
opens one short-lived connection per call and SQLite has a single writer
anyway, so a local single-user server gains nothing from concurrency here and
stays much easier to reason about.
"""

from __future__ import annotations

import http.cookies
import json
import re
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Iterable, Sequence
from http import HTTPStatus
from importlib import metadata
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import Headers, QueryParams, UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nodum import assets, auth, db, ingest, service, urls
from nodum import search as search_module
from nodum.assets import (
    AssetNotFound,
    AssetSourceChanged,
    AssetTooLarge,
    ImageTooLarge,
    UnsupportedRendition,
)
from nodum.envelope import envelope, list_envelope, render_json
from nodum.principal import Principal
from nodum.service import (
    AccountExists,
    EventNotFound,
    GrantNotPermitted,
    InvalidTransition,
    RecordNotFound,
    TypeNotFound,
    UndoNotPossible,
)

#: The session cookie's name: an opaque id for a server-side ``sessions`` row
#: (30-day sliding expiry), set by ``POST /api/login`` as ``HttpOnly;
#: SameSite=Strict; Path=/``.
SESSION_COOKIE = "nodum_session"

#: Scope key :class:`SessionMiddleware` stores the verified principal under.
#: The *only* source of identity on this surface — :func:`_session_principal`
#: reads it and nothing else, so request data can never become an identity.
SESSION_SCOPE_KEY = "nodum.session_principal"

#: The one ``/api`` route outside the session gate that takes a password (it
#: *makes* sessions). ``/healthz`` sits outside ``/api`` entirely: a liveness
#: probe that needs credentials is not a liveness probe.
LOGIN_PATH = "/api/login"

#: Path prefixes of the capability-URL routes, taken from
#: :data:`nodum.urls.TOKEN_PATHS` rather than spelled again here — the minted
#: URL and the route that redeems it must not be able to drift apart.
#:
#: The trailing slash is load-bearing: it is what separates
#: ``PUT /api/uploads/{token}`` (redeeming a grant, no session) from
#: ``POST /api/uploads`` (*minting* one, session required like every other
#: write). One character is all that stands between the two, so it is a
#: constant with a reason rather than an inline literal.
TOKEN_PATH_PREFIXES = tuple(sorted(f"{prefix}/" for prefix in urls.TOKEN_PATHS.values()))


def _session_principal(request: Request) -> Principal:
    """The principal the session middleware verified for this request.

    Every ``principal=`` binding in this module is a call to this (an AST
    property in ``tests/test_http_api.py`` keeps it so), and this reads only
    what :class:`SessionMiddleware` put in the scope — never the request's
    own data. Absence is a programming error (a route reached without the
    middleware), not an authentication failure, so it raises rather than
    minting anything: no principal without a verified session.
    """
    principal = request.scope.get(SESSION_SCOPE_KEY)
    if principal is None:
        raise RuntimeError(
            "no session principal in scope: this route ran outside SessionMiddleware"
        )
    return principal


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

#: Read size for every streamed transfer on this surface — spooling a multipart
#: upload to disk, and reading an original back out of its blob. The same 1 MiB
#: chunk :mod:`nodum.assets` streams blobs with, so a large file is never held
#: whole in either direction.
UPLOAD_CHUNK_BYTES = 1 << 20

#: Content type every downloaded original is served as, whatever the stored
#: ``assets.mime`` says.
#:
#: The bytes came from outside: a stranger's upload, or a URL an agent asked
#: this server to fetch. Echoing their MIME back is how a file host turns into
#: a stored-XSS vector — one ``text/html`` original, served from this origin,
#: runs script *on the origin that may write to this API*, and
#: :data:`CONTENT_SECURITY_POLICY` does not cover it: that header is set by the
#: static-hosting route only, and adding it here would be a second-order fix
#: for a problem this line removes outright.
#:
#: The headers :func:`_original_response` sends with it matter as much as the
#: type. ``nosniff`` stops a browser deciding for itself that an
#: ``application/octet-stream`` body looks like HTML, and
#: ``Content-Disposition: attachment`` makes the response a download rather
#: than a document. The caller that minted the URL already knows what it asked
#: for, so nothing truthful is lost.
DOWNLOAD_CONTENT_TYPE = "application/octet-stream"

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
#: ``Host: localhost:5700``, and a port is no part of the rebinding defence
#: anyway — an attacker who rebinds ``evil.example`` to 127.0.0.1 still has to
#: send ``Host: evil.example``.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: Addresses whose *bind* reaches only this machine. Not the same set as
#: :data:`LOOPBACK_HOSTS`: ``http://0.0.0.0:8600`` typed into a browser resolves
#: to loopback and is a fine ``Host`` value, while ``--host 0.0.0.0`` binds
#: every interface and puts the API on the network. ``nodum serve`` uses this
#: to decide the session cookie's ``Secure`` flag: loopback is plain HTTP (a
#: ``Secure`` cookie would never be stored), a LAN bind fronts TLS.
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
    AssetNotFound: 404,
    # 400 — the request itself is wrong: a bad value, an impossible transition,
    # an asset that cannot be stored or rendered. OverflowError is a caller's
    # integer that no SQLite parameter can hold (`?limit=9999…`), which reached
    # the driver as a Python bignum and surfaced as a 500 before it was mapped.
    ValueError: 400,
    InvalidTransition: 400,
    # Both derive from ValueError and would land on 400 through it; they are
    # named so the table shows the two failures a Phase-4 caller actually
    # meets. `TokenInvalid` deliberately carries one message for unknown,
    # expired, spent and wrong-kind alike — the status must not tell them
    # apart either, which is why it is not split into 404/410.
    urls.TokenInvalid: 400,
    ingest.IngestError: 400,
    AssetTooLarge: 400,
    AssetSourceChanged: 400,
    UnsupportedRendition: 400,
    ImageTooLarge: 400,
    OverflowError: 400,
    # 401 — the login itself failed (bad name or password). The *absence* of
    # a session on any other route is answered by SessionMiddleware directly.
    # Listed explicitly because InvalidCredentials derives from OSError via
    # PermissionError and would otherwise inherit the 500 below.
    auth.InvalidCredentials: 401,
    # 403 — the grant model refused a write. Sessions mint human principals
    # only, and humans are unfiltered, so this is unreachable from this
    # surface by construction; mapped so it could never surface as a 500.
    GrantNotPermitted: 403,
    # 409 — the graph has grown past the event being undone, or the account
    # name is taken. AccountExists derives from ValueError; the more specific
    # entry wins (Starlette walks the exception's MRO).
    UndoNotPossible: 409,
    AccountExists: 409,
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


def _write(request: Request, operation: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call a service write as the session's principal — the only one this surface has.

    Every write, review, archive, and undo handler goes through here,
    and this is the **one** place in the module that binds ``principal`` for
    a write at all — to whatever :class:`SessionMiddleware` verified into
    the request's scope. A caller cannot supply one: a ``principal`` (or
    legacy ``actor``) keyword arriving here would mean a handler forwarded
    request data wholesale, so it is refused rather than honoured and no
    request field can ever reach the service as an identity.

    Args:
        request: The incoming request, carrying the verified session principal.
        operation: The :mod:`nodum.service` (or :mod:`nodum.ingest` /
            :mod:`nodum.urls`) function to invoke — anything that takes a
            ``principal`` and writes.
        *args: Positional arguments for it.
        **kwargs: Keyword arguments for it, never including ``principal``.

    Returns:
        Whatever the service function returns.

    Raises:
        RuntimeError: If a caller tried to supply a principal.
    """
    if "principal" in kwargs or "actor" in kwargs:
        raise RuntimeError(
            "the HTTP surface never takes a principal from a caller: "
            "identity comes from the authenticated session, never the request"
        )
    return operation(*args, principal=_session_principal(request), **kwargs)


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
    terminal and a stranger's over a socket. ``auth.InvalidCredentials``
    derives from ``OSError`` (via ``PermissionError``) and is checked first:
    it is a credential failure, never a storage one.
    """
    if isinstance(exc, auth.InvalidCredentials):
        return str(exc)
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


def _is_capability_path(path: str) -> bool:
    """Is this a route whose *URL itself* is the credential?

    **Read this before "tidying up" either gate.** The session gate and the
    origin/content-type gate exist for one specific reason: a browser attaches
    the session cookie to any request it is talked into making, so a page on
    another origin can spend the human's identity without ever seeing the
    reply. That is what CSRF *is*, and both gates are built around it.

    A capability URL carries no ambient credential. The token in the path is
    the whole authorisation: :mod:`nodum.urls` minted it against a principal
    that had already passed the session gate, it is single-use, it expires in
    minutes, and nothing about a browser ever attaches it — a cross-origin
    page that does not already hold the URL has nothing to ride, and one that
    *does* hold it could equally well have used ``curl``. Requiring
    ``Content-Type: application/json`` on a raw-bytes upload is incoherent on
    top of that, and requiring a session would defeat the point of the hatch:
    it exists precisely for an agent host that has no account here and no
    filesystem in common with this server.

    Two things that are **not** exempt, on purpose:

    * the ``Host`` check — DNS rebinding is about which *server* a request
      reached, which a capability changes nothing about, and a rebound page
      that guessed a token would otherwise be handed the bytes; and
    * the body ceiling — :data:`nodum.urls.MAX_UPLOAD_BYTES` is deliberately
      equal to :data:`MAX_REQUEST_BYTES`, so a grant can never promise more
      than this server is willing to read.

    Args:
        path: The normalised request path.

    Returns:
        Whether the path redeems a capability token.
    """
    return path.startswith(TOKEN_PATH_PREFIXES)


def _needs_a_session(path: str) -> bool:
    """Does this path require the session gate to have verified a human?

    The single expression of "every ``/api`` route but the exemptions", so a
    second exemption is a name in one predicate rather than a string compared
    in three places. The exemptions are the route that *makes* a session and
    the routes that carry their own credential — nothing else, which
    ``tests/test_http_api.py`` asserts over the live route table.

    Args:
        path: The normalised request path.

    Returns:
        Whether a request for this path must present a valid session.
    """
    return _is_api_path(path) and path != LOGIN_PATH and not _is_capability_path(path)


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

    Strips a port and the brackets of an IPv6 literal, so ``[::1]:8600`` and
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
    ``make web-dev``'s proxied ``Host: localhost:5700``, since ports are not
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

    ``nodum serve`` binds loopback by default, and **loopback is reachable
    from every page the user visits**. Nothing about the bind stops
    ``https://evil.example`` from posting to ``http://127.0.0.1:8600``; the
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

    Checks 2 and 3 are skipped for the capability-URL routes, and only those
    two: they are the checks that assume an ambient credential the request
    would be spending, and a capability URL has none — see
    :func:`_is_capability_path`. Checks 1 and 4 apply to every request this
    server answers, those routes included.

    What it does **not** do: authenticate. Any local process can satisfy every
    check above with three ``curl`` headers. That is what the password session
    is for — this guard decides which requests may *reach* the credential
    check, not who passes it.
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

        if _is_capability_path(scope["path"]):
            # The CSRF checks below and the content-type check under them both
            # assume the request would arrive carrying an ambient credential.
            # A capability URL has none: the token in the path is the entire
            # authorisation, so there is nothing for a cross-origin page to
            # ride and nothing a same-origin proof would add. See
            # `_is_capability_path` for the full argument — and note that the
            # `Host` check above has already run, and the body cap below still
            # runs, because neither of those is about ambient credentials.
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


def _cookie_value(scope: Scope, name: str) -> str | None:
    """Read one cookie out of the request headers, or ``None``.

    Malformed cookie headers parse to nothing rather than raising — a garbage
    ``Cookie`` line is an unauthenticated request, not a 500.
    """
    header = Headers(scope=scope).get("cookie")
    if not header:
        return None
    jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
    try:
        jar.load(header)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


class SessionMiddleware:
    """Resolve the session cookie to a verified principal on every ``/api`` request.

    The gate is simple because the model is: every ``/api`` route
    :func:`_needs_a_session` claims needs a valid session — reads included
    (single-human app; one rule, no per-route memory). ``/healthz`` is outside
    ``/api`` and the static UI is not an API path, so the probe and the login
    page's bundle stay open. The two exemptions inside ``/api`` are
    :data:`LOGIN_PATH`, which *makes* sessions, and the capability-URL routes,
    which carry their own single-use credential in the path
    (:func:`_is_capability_path`) and would be pointless behind a session gate
    — their whole reason to exist is an agent host with no account here.

    Resolution goes through :func:`auth.principal_for_session`, which checks
    the row exists, has not expired (sliding the expiry forward on success),
    and belongs to an enabled human — so logout, expiry, and ``human
    disable`` all kill a cookie at the next request, with no cache in
    between. The verified principal is stored in the scope under
    :data:`SESSION_SCOPE_KEY`, the one place :func:`_session_principal`
    reads; the middleware never looks at anything else a request carries.
    """

    def __init__(self, app: ASGIApp, *, db_path: str | Path | None = None) -> None:
        self.app = app
        self.db_path = db_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Gate ``/api`` on a valid session; pass everything else through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if not _needs_a_session(path):
            await self.app(scope, receive, send)
            return
        session_id = _cookie_value(scope, SESSION_COOKIE)
        principal: Principal | None = None
        if session_id is not None:
            try:
                principal = auth.principal_for_session(session_id, path=self.db_path)
            except auth.InvalidCredentials:
                principal = None
        if principal is None:
            response = _error(
                401, "Unauthorized", "a valid session is required: POST /api/login first"
            )
            if session_id is not None:
                # A presented-but-dead cookie is cleared, so the client stops
                # offering it instead of being 401'd on every request.
                response.delete_cookie(SESSION_COOKIE, path="/")
            await response(scope, receive, send)
            return
        scope[SESSION_SCOPE_KEY] = principal
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


def _required_str(body: dict[str, Any], key: str) -> str:
    """Return a required body field that must be a string.

    Credentials and account names reach SQLite directly, where a JSON number
    or object is a 500 rather than a 400 (Q13 review S14).

    Raises:
        ValueError: If the key is missing, null, or not a string.
    """
    value = _required(body, key)
    if not isinstance(value, str):
        raise ValueError(f"field {key!r} must be a string")
    return value


def _required_int(body: dict[str, Any], key: str) -> int:
    """Return a required body field that must be an integer.

    ``bool`` is refused because it *is* an ``int`` in Python and ``True`` as a
    byte count is a caller mistake, not a size. The declared size reaches
    SQLite and a comparison in :func:`nodum.urls.mint_upload`, where a string
    would be a ``TypeError`` (a 500) rather than the 400 it plainly is.

    Raises:
        ValueError: If the key is missing, null, not an integer, or does not
            fit in a signed 64-bit integer.
    """
    value = _required(body, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"field {key!r} must be an integer")
    return _bounded_int(value, key)


def _optional_str(body: dict[str, Any], key: str) -> str | None:
    """Return an optional body field that must be a string when it is present.

    Absent and null both read as "not given". The same reasoning as
    :func:`_required_str`, for the fields that have a default: a JSON number
    where a name or a hash belongs is a 400, not a traceback three layers
    down.

    Raises:
        ValueError: If the key is present, non-null, and not a string.
    """
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"field {key!r} must be a string")
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


# ── Serving original bytes ────────────────────────────────────────────────────


async def _blob_chunks(conn: sqlite3.Connection, blob: sqlite3.Blob) -> AsyncIterator[bytes]:
    """Yield an open blob's bytes in bounded chunks, closing both handles after.

    The point of the generator: an original may be a gigabyte, and reading it
    into a ``bytes`` to hand to a ``Response`` would put all of it in the
    server's memory to send a copy the client reads at its own pace. The
    connection outlives the handler that opened it — a blob handle is only
    valid while its connection is — so this owns the close, including the one
    that matters, when a client hangs up mid-transfer and Starlette closes the
    generator instead of draining it.

    Args:
        conn: The connection the blob was opened on; closed on the way out.
        blob: An open, readable blob handle positioned at its start.

    Yields:
        Successive chunks of at most :data:`UPLOAD_CHUNK_BYTES`.
    """
    try:
        with blob:
            while chunk := blob.read(UPLOAD_CHUNK_BYTES):
                yield chunk
    finally:
        conn.close()


def _original_response(asset_hash: str, path: str | Path | None) -> Response:
    """Stream one asset's stored original bytes as a download.

    Everything about the response says "bytes, saved to disk, interpreted by
    nobody": :data:`DOWNLOAD_CONTENT_TYPE` rather than the stored MIME,
    ``nosniff`` so a browser does not overrule it, and ``attachment`` so it is
    never rendered as a document in this origin (see the constant for why that
    is the whole game on a file host). The filename is the content address run
    through :data:`_SAFE_FILENAME_RE` — the one name attached to these bytes
    that no stranger chose.

    ``no-store`` keeps a shared proxy from retaining a private document that
    was reachable for one request under a URL that is now spent.

    Args:
        asset_hash: The asset whose original to stream.
        path: Explicit database path, as every other call here takes.

    Returns:
        The streaming response, with an accurate ``Content-Length``.

    Raises:
        AssetNotFound: If the blob store holds no bytes for that hash.
    """
    # `db.connect` rather than the migrating open every other module uses: the
    # only caller reached this through `urls.consume`, which just read and
    # updated a row in this database, so the schema is present by construction
    # and a migration pass on a download would be a second writer for nothing.
    conn = db.connect(path)
    try:
        blob = assets.open_original(conn, asset_hash)
        size = len(blob)
    except Exception:
        conn.close()
        raise
    filename = f"nodum-{_SAFE_FILENAME_RE.sub('-', asset_hash)[:64]}"
    return StreamingResponse(
        _blob_chunks(conn, blob),
        media_type=DOWNLOAD_CONTENT_TYPE,
        headers={
            "content-length": str(size),
            "content-disposition": f'attachment; filename="{filename}"',
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


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
    allowed_hosts: Sequence[str] | frozenset[str] | None = None,
    max_body_bytes: int | None = None,
    secure_cookies: bool = False,
) -> Starlette:
    """Build the nodum HTTP app: the JSON API plus the human web UI.

    Args:
        db_path: Explicit database path; defaults to ``NODUM_DB`` resolution,
            exactly as every other adapter resolves it.
        allowed_hosts: Host names this server answers to (see
            :func:`resolve_allowed_hosts`). Defaults to the loopback set, which
            is what the default bind serves.
        max_body_bytes: Ceiling on a request body; defaults to
            :data:`MAX_REQUEST_BYTES`.
        secure_cookies: Mark the session cookie ``Secure``. ``nodum serve``
            sets it for a non-loopback bind (a LAN hostname will be served
            over TLS in front); loopback stays plain HTTP, where a ``Secure``
            cookie would never be stored at all.

    Returns:
        The configured Starlette application. Every ``/api`` route but
        ``POST /api/login`` requires a valid session (``/healthz`` and the
        static UI stay open); origin control keeps *browsers* out, and the
        human's password is what keeps other local *processes* out.
    """
    hosts = frozenset(allowed_hosts) if allowed_hosts is not None else resolve_allowed_hosts("")
    body_limit = MAX_REQUEST_BYTES if max_body_bytes is None else max_body_bytes

    # ── Health, login, and catalog ────────────────────────────────────────

    async def healthz(request: Request) -> Response:
        """Liveness probe — open even with the session gate on.

        Reports liveness and nothing else. It used to report the absolute
        database path, which is unauthenticated disclosure of a username and a
        filesystem layout. ``nodum serve`` prints the path at startup, where
        the operator is the only reader.
        """
        return EnvelopeResponse({"status": "ok", "version": VERSION})

    async def login(request: Request) -> Response:
        """Password login — the one ``/api`` route outside the session gate.

        Verifies name + password through :func:`auth.verify_login` (argon2id,
        constant-time on failure), creates a server-side session row (30-day
        sliding expiry), and sets the cookie ``HttpOnly; SameSite=Strict`` —
        JavaScript cannot read it and a cross-site request never carries it,
        which is what lets the origin guard and the session gate each do
        their own job. Failure is a 401 with no cookie, indistinguishable
        between "no such name" and "wrong password".
        """
        body = await _json_body(request)
        name = _required_str(body, "name")
        password = _required_str(body, "password")
        principal = auth.verify_login(name, password, path=db_path)
        session_id = auth.create_session(principal.id, path=db_path)
        response = EnvelopeResponse({"human": principal.id})
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            path="/",
            httponly=True,
            samesite="strict",
            secure=secure_cookies,
        )
        return response

    async def logout(request: Request) -> Response:
        """Log out: drop the server-side session row and clear the cookie.

        The session gate ran first, so the cookie this resolves was valid a
        moment ago; deleting is idempotent regardless.
        """
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id is not None:
            auth.delete_session(session_id, path=db_path)
        response = EnvelopeResponse({"status": "ok"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def get_types(request: Request) -> Response:
        """The live type catalog (node types and edge types)."""
        return EnvelopeResponse(
            envelope(service.list_types(principal=_session_principal(request), path=db_path))
        )

    async def get_schema(request: Request) -> Response:
        """One node or edge type's catalog entry, including its JSON schema."""
        return EnvelopeResponse(
            envelope(
                service.get_schema(
                    request.path_params["type"], principal=_session_principal(request), path=db_path
                )
            )
        )

    # ── Nodes ─────────────────────────────────────────────────────────────

    async def list_nodes(request: Request) -> Response:
        """List nodes in creation order, optionally filtered.

        ``space`` narrows the listing to one space and ``include_meta`` opts
        into the meta space; both default to the whole file minus meta, which
        is what every view showed before spaces reached the UI.
        """
        params = request.query_params
        nodes = service.list_nodes(
            type=params.get("type"),
            state=params.get("state"),
            parent_id=_param(params, "parent_id", "parent"),
            space=params.get("space"),
            include_meta=_bool_param(params, "include_meta"),
            principal=_session_principal(request),
            limit=_int_param(params, "limit", default=500),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("nodes", nodes))

    async def create_node(request: Request) -> Response:
        """Create a node. It lands ``active``: this is the human surface.

        ``space`` is the write target — optional, and ``main`` when absent,
        exactly as the service defaults it. It names *where the node goes*,
        never *who wrote it*: the writer is the session's human and no body
        field can say otherwise.
        """
        body = await _json_body(request)
        node = _write(
            request,
            service.create_node,
            type=_required(body, "type"),
            title=body.get("title"),
            content=body.get("content") or "",
            parent_id=body.get("parent_id"),
            props=body.get("props"),
            space=_optional_str(body, "space"),
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
            result = service.get_neighborhood(
                node_id, depth=depth, principal=_session_principal(request), path=db_path
            )
        else:
            result = service.get_node(node_id, principal=_session_principal(request), path=db_path)
        return EnvelopeResponse(envelope(result))

    async def update_node(request: Request) -> Response:
        """Update the named fields of a node, and only those."""
        body = await _json_body(request)
        fields = {name: body[name] for name in PATCHABLE_FIELDS if name in body}
        if not fields:
            raise ValueError(
                f"nothing to update: send one of {', '.join(repr(f) for f in PATCHABLE_FIELDS)}"
            )
        node = _write(
            request, service.update_node, request.path_params["id"], path=db_path, **fields
        )
        return EnvelopeResponse(envelope(node))

    async def list_children(request: Request) -> Response:
        """A node's children in ``position`` order (the document tree)."""
        nodes = service.list_children(
            request.path_params["id"], principal=_session_principal(request), path=db_path
        )
        return EnvelopeResponse(list_envelope("nodes", nodes))

    async def node_history(request: Request) -> Response:
        """A node's version snapshots, chronological."""
        versions = service.history(
            request.path_params["id"], principal=_session_principal(request), path=db_path
        )
        return EnvelopeResponse(list_envelope("versions", versions))

    async def archive_node(request: Request) -> Response:
        """Retire a node (``active`` → ``archived``) — the service's human tier."""
        node = _write(
            request, service.transition, request.path_params["id"], "archive", path=db_path
        )
        return EnvelopeResponse(envelope(node))

    # ── Edges ─────────────────────────────────────────────────────────────

    async def list_edges(request: Request) -> Response:
        """List edges, optionally filtered by incident node, type, or state."""
        params = request.query_params
        edges = service.list_edges(
            node_id=_param(params, "node_id", "node"),
            type=params.get("type"),
            state=params.get("state"),
            principal=_session_principal(request),
            limit=_int_param(params, "limit", default=500),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("edges", edges))

    async def create_edge(request: Request) -> Response:
        """Create a typed, directed edge between two nodes."""
        body = await _json_body(request)
        edge = _write(
            request,
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
            include_meta=_bool_param(params, "include_meta"),
            space=params.get("space"),
            expand=_bool_param(params, "expand"),
            principal=_session_principal(request),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    async def suggest_links(request: Request) -> Response:
        """Title-prefix candidates for the editor's ``[[`` autocomplete."""
        params = request.query_params
        nodes = service.suggest_links(
            params.get("prefix", ""),
            principal=_session_principal(request),
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
            principal=_session_principal(request),
            limit=_int_param(params, "limit", default=200),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    async def get_path(request: Request) -> Response:
        """The shortest active-edge path between two nodes."""
        params = request.query_params
        result = service.find_path(
            _required_param(params, "a"),
            _required_param(params, "b"),
            principal=_session_principal(request),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    # ── Review queue (the human tier) ─────────────────────────────────────

    async def review_queue(request: Request) -> Response:
        """Pending proposals with reviewer context, oldest first."""
        params = request.query_params
        proposals = service.list_proposals(
            **_proposal_filters(params),
            principal=_session_principal(request),
            limit=_int_param(params, "limit", default=500),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("proposals", proposals))

    async def review_accept(request: Request) -> Response:
        """Accept proposals by id, or every proposal matching a filter."""
        body = await _json_body(request)
        if "ids" in body:
            result = _write(request, service.accept_proposals, _id_list(body["ids"]), path=db_path)
        else:
            result = _write(
                request, service.accept_matching, **_selective_filters(body, "accept"), path=db_path
            )
        return EnvelopeResponse(envelope(result))

    async def review_reject(request: Request) -> Response:
        """Reject proposals by id, or by filter. The reason is mandatory."""
        body = await _json_body(request)
        reason = _reject_reason(body)
        if "ids" in body:
            result = _write(
                request,
                service.reject_proposals,
                _id_list(body["ids"]),
                reason=reason,
                path=db_path,
            )
        else:
            result = _write(
                request,
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
        diff = service.diff_versions(a, b, principal=_session_principal(request), path=db_path)
        return EnvelopeResponse(envelope(diff))

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
        return EnvelopeResponse(
            list_envelope(
                "assets",
                assets.list_assets(principal=_session_principal(request), path=db_path),
            )
        )

    async def get_asset(request: Request) -> Response:
        """One asset's metadata, by hash or by asset-reference node id."""
        asset = assets.get_asset(
            request.path_params["id"], principal=_session_principal(request), path=db_path
        )
        return EnvelopeResponse(envelope(asset))

    async def get_rendition(request: Request) -> Response:
        """The WebP bytes of an image rendition — generated lazily, cached in the DB.

        Renditions only (design §5.7): ``thumb`` and ``preview`` are the whole
        vocabulary, and originals are never served — here or anywhere else.
        """
        rendition = assets.get_rendition(
            request.path_params["id"],
            profile=request.path_params["profile"],
            principal=_session_principal(request),
            path=db_path,
        )
        payload = assets.read_rendition_bytes(rendition, path=db_path)
        return Response(payload, media_type=assets.RENDITION_MIME)

    # ── Ingestion ─────────────────────────────────────────────────────────

    async def ingest_source(request: Request) -> Response:
        """Ingest one local file **or** one URL: register, extract, describe, propose.

        Exactly one of ``path`` and ``url``. Both named, or neither, is a 400
        — a precedence rule between the two would be a coin flip nobody
        remembers reading. ``name``, ``space`` and ``title`` are optional and
        mean what they mean everywhere else.

        Two things this route hands the session's human, deliberately, and
        worth saying out loud rather than discovering: ``path`` is read *by
        the server*, so it reaches any file the server's own user can read
        (design §5.7's ingestion by reference — the reason no base64 ever
        crosses a surface), and ``url`` is fetched *by the server*, from
        wherever this machine can reach; :mod:`nodum.ingest` states plainly
        that it blocks neither loopback nor private ranges, because the server
        is itself a loopback service. Both are properties of a human-only
        surface behind a password, and they are exactly why this route is
        inside the session gate while the two token routes below are not.
        """
        body = await _json_body(request)
        by_url = body.get("url") is not None
        if by_url == (body.get("path") is not None):
            raise ValueError("ingest takes exactly one of 'path' and 'url'")
        operation = ingest.ingest_url if by_url else ingest.ingest_file
        result = _write(
            request,
            operation,
            _required_str(body, "url" if by_url else "path"),
            name=_optional_str(body, "name"),
            space=_optional_str(body, "space"),
            title=_optional_str(body, "title"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    # ── Capability URLs: minting (session) and redeeming (token) ──────────

    async def mint_asset_download_url(request: Request) -> Response:
        """Mint a single-use, short-lived URL for one asset's original bytes.

        ``POST`` because minting is a state change: it writes a token row and
        an event, and doing that on a ``GET`` would mean a link preview could
        spend a grant. It takes no body — the lifetime is
        :data:`nodum.urls.DEFAULT_TTL_SECONDS`, and the address the URL is
        built on is the server's configured public one (``NODUM_PUBLIC_URL``)
        rather than the ``Host`` this request happened to arrive with: a
        minted URL outlives its request, so it is configuration, not an echo
        of a header.

        Refuses, as *not found*, any asset this session cannot already reach
        through a describing node: minting resolves the asset through the same
        scoped accessor every other read uses, so a token can never widen
        anyone's reach.
        """
        grant = _write(request, urls.mint_download, request.path_params["id"], path=db_path)
        return EnvelopeResponse(envelope(grant))

    async def request_upload_url(request: Request) -> Response:
        """Mint a single-use URL to PUT one file to — or answer with a dedup hit.

        ``name``, ``mime`` and ``size`` are required; ``sha256`` and ``space``
        are optional. A declared ``sha256`` this file already holds comes back
        as the existing asset with **no grant at all** and no bytes moved
        (design §5.7 rule 4). ``size`` is the ceiling the redemption route
        then enforces on the body, and the service refuses one above
        :data:`nodum.urls.MAX_UPLOAD_BYTES`, which is deliberately equal to
        :data:`MAX_REQUEST_BYTES` — a grant promising more than this server
        will read is a grant that fails halfway through a transfer.

        Refuses a ``space`` the session cannot write, at mint time rather than
        after the bytes have crossed the network.
        """
        body = await _json_body(request)
        grant = _write(
            request,
            urls.mint_upload,
            _required_str(body, "name"),
            _required_str(body, "mime"),
            _required_int(body, "size"),
            sha256=_optional_str(body, "sha256"),
            space=_optional_str(body, "space"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(grant))

    async def download_original(request: Request) -> Response:
        """Spend a download token and stream that asset's original bytes.

        **Outside the session gate and outside the origin/content-type gate,
        on purpose** — :func:`_is_capability_path` carries the argument, and
        this handler is the reason it exists: no session is consulted and none
        is required, because the unguessable token in the path *is* the
        authorisation. It is spent exactly once here, and
        :func:`nodum.urls.consume` records the redemption against the identity
        stored on the token row, which is the only truthful one available —
        this request carries no identity of its own and this module may not
        invent one.

        The ``Host`` check and the body ceiling still apply, like everywhere
        else: neither has anything to do with ambient credentials.

        Refuses — one status, one message, no way to tell the cases apart — a
        token that is unknown, expired, already spent, or minted for the
        upload route. Distinguishing them would tell whoever is guessing which
        of the four they just achieved.

        The bytes are streamed out of the blob rather than read whole, and
        served as an opaque attachment nothing will render: see
        :data:`DOWNLOAD_CONTENT_TYPE`.
        """
        row = urls.consume(request.path_params["token"], kind="download", path=db_path)
        return _original_response(row["asset_hash"], db_path)

    async def upload_original(request: Request) -> Response:
        """Spend an upload token and store the raw request body as an asset.

        Outside both gates for the same reason as the download route above,
        and with the same two exceptions: the ``Host`` check ran before this,
        and the body is capped twice — by :data:`MAX_REQUEST_BYTES` in the
        middleware, and by the grant's own ``max_bytes`` here. The second cap
        is enforced **while the body streams**, at the byte that passes it,
        not after the whole thing has been spooled to disk: a grant that
        promised 4 KB must not cost 32 MB of ``/tmp`` to refuse.

        The stored name is the one the grant recorded, reduced to its last
        path segment, so nothing a name says can place a file outside the
        temporary directory it is spooled into.

        **The describing nodes are written by the domain, not by this route.**
        Design §5.7 rule 4 ends "normal ingestion runs after the PUT", and
        without that step the hatch dead-ends: bytes no surface could turn into
        a subgraph, since ``ingest_file`` takes a path on the *server*. But a
        graph write needs a live principal, and this request has none by
        construction — it presented a URL, not a credential, and this module
        may not load one from stored state (``tests/test_principal_guards.py``
        pins every adapter's identity source, and this one's is the session).
        So the route hands the spooled file and the token row to
        :func:`nodum.ingest.ingest_upload`, which re-mints the principal that
        *authorised* the grant from the row's own ``created_by``. The identity
        never passes through this module, and a grant whose account has since
        been disabled fails there rather than here.
        """
        row = urls.consume(request.path_params["token"], kind="upload", path=db_path)
        max_bytes = row["max_bytes"]
        original_name = row["original_name"] or "upload"
        with tempfile.TemporaryDirectory(prefix="nodum-upload-") as directory:
            spooled = Path(directory) / (Path(original_name).name or "upload")
            received = 0
            with spooled.open("wb") as handle:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > max_bytes:
                        raise PayloadTooLarge(
                            f"this upload grant is for {max_bytes} bytes and the body is larger"
                        )
                    handle.write(chunk)
            result = ingest.ingest_upload(row, spooled, path=db_path)
        return EnvelopeResponse(envelope(result))

    # ── Event log, undo, export ───────────────────────────────────────────

    async def list_events(request: Request) -> Response:
        """The append-only event log, newest first."""
        limit = _int_param(request.query_params, "limit", default=50)
        events = service.list_events(_session_principal(request), limit=limit, path=db_path)
        return EnvelopeResponse(list_envelope("events", events))

    async def undo(request: Request) -> Response:
        """Reverse one event (default: the latest reversible one) — human tier."""
        body = await _json_body(request)
        seq = body.get("seq")
        if seq is not None:
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise ValueError(f"'seq' must be an event seq (integer), got {seq!r}")
            _bounded_int(seq, "seq")
        result = _write(request, service.undo, seq, path=db_path)
        return EnvelopeResponse(envelope(result))

    async def export_node(request: Request) -> Response:
        """Download a node — and optionally its neighborhood — as a JSON file."""
        node_id = request.path_params["id"]
        depth = _int_param(request.query_params, "depth", default=0)
        result = service.get_neighborhood(
            node_id, depth=depth, principal=_session_principal(request), path=db_path
        )
        response = EnvelopeResponse(envelope(result))
        safe_id = _SAFE_FILENAME_RE.sub("-", node_id)[:64] or "node"
        response.headers["content-disposition"] = f'attachment; filename="nodum-{safe_id}.json"'
        return response

    # ── Accounts and grants (the human administering their file) ──────────

    async def get_me(request: Request) -> Response:
        """The session's own human account (id, name, credential state)."""
        principal = _session_principal(request)
        me = next(
            human
            for human in service.list_humans(principal=_session_principal(request), path=db_path)
            if human.id == principal.id
        )
        return EnvelopeResponse(envelope(me))

    async def list_humans(request: Request) -> Response:
        """Every human account."""
        humans = service.list_humans(principal=_session_principal(request), path=db_path)
        return EnvelopeResponse(list_envelope("humans", humans))

    async def create_human(request: Request) -> Response:
        """Create a human account (passwordless until its password is set)."""
        body = await _json_body(request)
        human = _write(request, service.create_human, _required_str(body, "name"), path=db_path)
        return EnvelopeResponse(envelope(human))

    async def set_human_password(request: Request) -> Response:
        """Set or change a human's password; the hash never leaves the service."""
        body = await _json_body(request)
        human_id = request.path_params["id"]
        _write(
            request,
            service.set_human_password,
            human_id,
            _required_str(body, "password"),
            path=db_path,
        )
        return EnvelopeResponse({"ok": True, "human_id": human_id})

    async def disable_human(request: Request) -> Response:
        """Disable a human — its sessions die, and its agents' tokens with them."""
        human_id = request.path_params["id"]
        _write(request, service.disable_human, human_id, path=db_path)
        return EnvelopeResponse({"ok": True, "human_id": human_id, "disabled": True})

    async def enable_human(request: Request) -> Response:
        """Re-enable a disabled human."""
        human_id = request.path_params["id"]
        _write(request, service.enable_human, human_id, path=db_path)
        return EnvelopeResponse({"ok": True, "human_id": human_id, "disabled": False})

    async def list_agents(request: Request) -> Response:
        """Every agent account."""
        agents = service.list_agents(principal=_session_principal(request), path=db_path)
        return EnvelopeResponse(list_envelope("agents", agents))

    async def create_agent(request: Request) -> Response:
        """Create an external agent owned by the session's human.

        The token comes back in this body — the one and only place it is ever
        shown (HTTP has no stderr to print it to, as the CLI does).
        """
        body = await _json_body(request)
        created = _write(
            request,
            service.create_agent,
            _required_str(body, "name"),
            kind="external",
            owner_human_id=_session_principal(request).id,
            path=db_path,
        )
        return EnvelopeResponse(envelope(created))

    async def rotate_agent_token(request: Request) -> Response:
        """Replace an agent's token; the new one is in this body and nowhere else."""
        agent_id = request.path_params["id"]
        token = _write(request, service.rotate_agent_token, agent_id, path=db_path)
        return EnvelopeResponse({"agent_id": agent_id, "token": token})

    async def disable_agent(request: Request) -> Response:
        """Disable an agent — its token dies immediately."""
        agent_id = request.path_params["id"]
        _write(request, service.disable_agent, agent_id, path=db_path)
        return EnvelopeResponse({"ok": True, "agent_id": agent_id, "disabled": True})

    async def enable_agent(request: Request) -> Response:
        """Re-enable a disabled agent."""
        agent_id = request.path_params["id"]
        _write(request, service.enable_agent, agent_id, path=db_path)
        return EnvelopeResponse({"ok": True, "agent_id": agent_id, "disabled": False})

    async def list_grants(request: Request) -> Response:
        """Grant rows, optionally one agent's (``?agent=``)."""
        grants = service.list_grants(
            request.query_params.get("agent"),
            principal=_session_principal(request),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("grants", grants))

    async def set_grant(request: Request) -> Response:
        """Grant (or re-level) an agent's access to a space."""
        body = await _json_body(request)
        granted = _write(
            request,
            service.grant,
            _required_str(body, "agent"),
            _required_str(body, "space"),
            _required_str(body, "level"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(granted))

    async def revoke_grant(request: Request) -> Response:
        """Revoke an agent's grant on a space."""
        body = await _json_body(request)
        agent_id = _required_str(body, "agent")
        space = _required_str(body, "space")
        _write(request, service.revoke, agent_id, space, path=db_path)
        return EnvelopeResponse({"ok": True, "agent": agent_id, "space": space})

    async def list_spaces(request: Request) -> Response:
        """Every active space, with its live node count and grant holders.

        Spaces are nodes of builtin type ``space`` in the meta space, which the
        everyday node listing excludes (``include_meta``), so ``/api/nodes``
        cannot serve them. This is the CLI's ``space-list`` read verbatim — the
        grant-admin picker's vocabulary, and the ``/spaces`` screen's answer to
        "what territory exists, how much is in it, and who else may touch it".
        """
        spaces = service.list_spaces(principal=_session_principal(request), path=db_path)
        return EnvelopeResponse(list_envelope("spaces", spaces))

    async def create_space(request: Request) -> Response:
        """Create a space (a node of builtin type ``space``, living in meta)."""
        body = await _json_body(request)
        space = _write(request, service.create_space, _required_str(body, "name"), path=db_path)
        return EnvelopeResponse(envelope(space))

    async def rename_space(request: Request) -> Response:
        """Rename a space — a space is a node, so this is a node-title update.

        The path segment is a space id or name, resolved as a *space*: a node
        of any other type does not resolve here, so this route cannot be used
        to rename something that is not a space.
        """
        body = await _json_body(request)
        space = _write(
            request,
            service.rename_space,
            request.path_params["id"],
            _required_str(body, "name"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(space))

    async def archive_space(request: Request) -> Response:
        """Archive a space; its nodes keep their ``space_id`` and grants go inert."""
        space = _write(request, service.archive_space, request.path_params["id"], path=db_path)
        return EnvelopeResponse(envelope(space))

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
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
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
        Route("/api/assets", list_assets),
        Route("/api/assets", upload_asset, methods=["POST"]),
        Route("/api/assets/{id}", get_asset),
        Route("/api/assets/{id}/rendition/{profile}", get_rendition),
        Route("/api/assets/{id}/download-url", mint_asset_download_url, methods=["POST"]),
        Route("/api/ingest", ingest_source, methods=["POST"]),
        Route("/api/uploads", request_upload_url, methods=["POST"]),
        # The two capability-URL routes, and the only ``/api`` paths outside
        # the session gate other than login: the token in the path is the
        # credential (`_is_capability_path`), so neither a session nor a
        # same-origin proof nor a content type is required — while the `Host`
        # check and the body ceiling apply to them exactly as to the rest.
        # Their paths come from `nodum.urls.TOKEN_PATHS`, which is also what
        # the minted URLs are built from, so the two cannot drift apart.
        Route(f"{urls.TOKEN_PATHS['download']}/{{token}}", download_original),
        Route(f"{urls.TOKEN_PATHS['upload']}/{{token}}", upload_original, methods=["PUT"]),
        Route("/api/events", list_events),
        Route("/api/undo", undo, methods=["POST"]),
        Route("/api/export/node/{id}", export_node),
        Route("/api/me", get_me),
        Route("/api/humans", list_humans),
        Route("/api/humans", create_human, methods=["POST"]),
        Route("/api/humans/{id}/password", set_human_password, methods=["POST"]),
        Route("/api/humans/{id}/disable", disable_human, methods=["POST"]),
        Route("/api/humans/{id}/enable", enable_human, methods=["POST"]),
        Route("/api/agents", list_agents),
        Route("/api/agents", create_agent, methods=["POST"]),
        Route("/api/agents/{id}/token-rotate", rotate_agent_token, methods=["POST"]),
        Route("/api/agents/{id}/disable", disable_agent, methods=["POST"]),
        Route("/api/agents/{id}/enable", enable_agent, methods=["POST"]),
        Route("/api/grants", list_grants),
        Route("/api/grants", set_grant, methods=["POST"]),
        Route("/api/grants/revoke", revoke_grant, methods=["POST"]),
        Route("/api/spaces", list_spaces),
        Route("/api/spaces", create_space, methods=["POST"]),
        Route("/api/spaces/{id}/rename", rename_space, methods=["POST"]),
        Route("/api/spaces/{id}/archive", archive_space, methods=["POST"]),
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
    # "wrong password" tells them nothing they did not already know. The
    # session gate runs second and resolves the identity every handler reads.
    middleware = [
        Middleware(RequestGuardMiddleware, allowed_hosts=hosts, max_body_bytes=body_limit),
        Middleware(SessionMiddleware, db_path=db_path),
    ]

    return Starlette(routes=routes, middleware=middleware, exception_handlers=exception_handlers)
