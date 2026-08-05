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
  response body. One failure carries more than those two keys, and only
  one: a refused rollback adds ``conflicts``, because decision C4 is that it
  *names* what is in the way rather than saying it failed
  (:func:`_rollback_conflict_handler`).
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

* **The nightly cycle** — this app owns one optional background task
  (:mod:`nodum.scheduler`), created by the lifespan and cancelled by it. It is
  the only thing on this surface that writes without a request, it is **off**
  unless ``NODUM_CONSOLIDATE_AT`` names a time, and shutdown is bounded rather
  than blocked on a cycle in flight. Its writes are the in-process gardener's,
  exactly as ``POST /api/cycles`` produces on demand: the identity boundary
  above is about what a *request* can claim, and neither of these takes one.

**What this surface still does not defend against, on purpose.** Login is
the boundary: any process that can open a socket on this port may *attempt*
one, so the strength of the human's password is the strength of the defence.
The one throttle is the failed-login lockout (M5): five failed attempts for
a name inside fifteen minutes refuse further attempts for that name with a
429 until the window slides past them — a guesser is limited to a handful
of tries per name per quarter-hour, and a name that does not exist locks
exactly like a real one, so the lockout cannot be used to probe the file.
Every attempt **that reaches a password check** is written to the event log
(``human.login`` / ``human.login_failed`` / ``human.logout``) — the auth
half of the audit trail — so who tried the password is on record even though
the password itself never is. The qualifier is exact and load-bearing: the two
refusals that never reach a check write nothing at all. A name already under
lockout is refused with a 429 and no row, deliberately (see :func:`login`: an
unauthenticated caller must not be able to append to an append-only log at
will, and a guesser must not be able to hold the real human out by re-arming
the window forever); and a body whose name or password is over the service's
length cap is the ordinary 400 a malformed request gets, ahead of the lockout
query and ahead of the log. Seven attempts against one name therefore leave
five rows. What is on record is every attempt that cost an argon2 verification,
which is the set the audit trail is for. ``nodum serve`` says so at startup
rather than leaving it implicit.

Most handlers call the service inline: the service opens one short-lived
connection per call and SQLite has a single writer anyway, so a local
single-user server gains nothing from concurrency for a read of a row or a
single-row write, and the inline calls stay much easier to reason about. The
exceptions are the work a single request can make the loop wait on for a
perceptible time — one 20.8 MB ingest was measured holding it for 20.8 s and
680 MB of RSS — and every one of them runs off the loop: through
:func:`~starlette.concurrency.run_in_threadpool`, or through
:func:`anyio.to_thread.run_sync` for the two hops that need a limiter of their
own, which is the only thing that call spells and ``run_in_threadpool`` does
not:

* **``POST /api/login``** — argon2id is ~100 ms of deliberate work, spent on
  unknown names too so the failure path costs what the success path costs.
  This is the one route reachable without a session, so inline it was a
  denial-of-service anybody with a socket could run at ten requests a second.
  It is also one of the two hops that needs a **bound of its own**.
  The default thread limiter admits 40 blocking calls at once and argon2id's
  default profile reserves 64 MiB for each, so moving the verification off the
  loop and stopping there swapped a stalled loop for ~2.5 GiB of resident
  memory an unauthenticated caller may ask for — and the failed-login lockout
  bounds none of it, because it counts per attempted *name* and rotating the
  name never trips it. So the login's blocking half runs under
  :data:`ARGON2_CONCURRENCY` tokens of :data:`_ARGON2_LIMITER`, and the excess
  **queues**. *Half*, not *hash*: what holds a token is
  :func:`_verify_login`, which deliberately bundles the lockout query, the
  argon2 verification and the failure event into one hop, so the token is held
  for all three — the same span :data:`_ARGON2_LIMITER`'s other caller holds it
  for, and the reason the figures below are per attempt rather than per hash. Queueing is a *trade*,
  not a free lunch, and the honest statement of it is this: the queue is FIFO
  and unbounded, so a flood does not fail — it waits in front of the human.
  Measured against this handler, the owner's own correct login answered in
  0.11 s idle, 3.4 s behind 64 queued attempts, and 13.4 s behind 256. A
  sustained flood therefore still degrades the login the route exists for; what
  the limiter bought is *which* denial-of-service that is. The memory one takes
  the process — and with it the graph, the SPA and every other tab — and does
  not come back on its own; the latency one is proportional to the flood, ends
  when the flood does, and still lets the human in. Refusing above a threshold
  instead would turn that wait into a 503, which is the same caller's switch
  for denying the login outright. Every other blocking route here keeps the
  default limiter — none of them runs argon2, and lowering the global one would
  slow the whole app to bound two routes;
* **``POST /api/humans/{id}/password``** — the second argon2 caller and the
  second hop under :data:`_ARGON2_LIMITER`. It ran **inline** until the review
  that wrote this bullet: ~100 ms of hashing on the single-threaded loop, at
  the same 64 MiB profile, under no bound at all. Being behind the session gate
  answers *who* may call it, not *how resident* the process may become while
  they do — 40 default threads of it is the same ~2.5 GiB the limiter exists to
  refuse — so an authenticated caller is not a reason to leave a second door
  open onto the memory the first one is bounded for;
* the **read-heavy routes** — ``GET /api/search`` (a 400-term query held the
  loop for 126 ms), ``POST /api/ask`` and ``POST /api/summarize``, each a model
  call on top of graph work;
* every **blocking write** — ``POST /api/assets`` and
  ``PUT /api/uploads/{token}`` (registration streams up to a 1 GB blob),
  ``POST /api/ingest`` (a fetch, then register/extract/describe),
  ``GET /api/assets/{id}/rendition/{profile}`` (Pillow decode or pypdfium2
  rasterisation on a miss), and the original download's spool; and
* ``POST /api/cycles``, which runs a whole consolidation cycle — every job
  over every node in scope — measured at 3.75 s on 450 nodes with no embedding
  provider, and minutes on a real graph with one.

Inline, any of those is not a slow request, it is a **stopped server**: the
event loop is single-threaded, so ``/healthz``, the SPA and every other tab
stall for exactly as long as the work runs. :mod:`nodum.scheduler` already made
this argument for the nightly half and answered it with
:func:`asyncio.to_thread`; the on-demand half is the one a human is actually
watching, so it runs through
:func:`~starlette.concurrency.run_in_threadpool` for the same reason. The
identity boundary is untouched: what goes to the thread is :func:`_write`
itself, so the principal is still bound in the one place this module binds one.
"""

from __future__ import annotations

import contextlib
import functools
import http.cookies
import json
import logging
import re
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import time
from http import HTTPStatus
from importlib import metadata
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, QueryParams, UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nodum import (
    answers,
    assets,
    auth,
    consolidate,
    db,
    ingest,
    mcp_server,
    scheduler,
    service,
    urls,
)
from nodum import search as search_module
from nodum.assets import (
    AssetNotFound,
    AssetSourceChanged,
    AssetTooLarge,
    ImageTooLarge,
    UnsupportedRendition,
)
from nodum.envelope import envelope, list_envelope, render_json
from nodum.models import CycleDetailOut, EdgeCreateIn, NodeCreateIn, NodeUpdateIn
from nodum.principal import Principal
from nodum.service import (
    AccountExists,
    EventNotFound,
    GrantNotPermitted,
    InvalidTransition,
    RecordNotFound,
    RollbackConflict,
    SpaceNameTaken,
    TypeNotFound,
    UndoNotPossible,
)
from nodum.urls import PayloadTooLarge
from nodum.vocab import GRANT_LEVEL_NAMES, PROPOSAL_KINDS, STATES, NodeState

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

#: How many argon2id hashes may be in flight at once, process-wide.
#:
#: argon2id's default profile reserves 64 MiB for the duration of every hash,
#: and one of the two callers — ``POST /api/login`` — is the one route
#: reachable without a session, so whatever number bounds this bounds how
#: resident an unauthenticated caller can make the server. Unbounded (the
#: default thread limiter's 40) that is ~2.5 GiB. Measured, concurrent
#: unauthenticated attempts against a fresh process, peak RSS above the idle
#: baseline:
#:
#: =========  =========  ========
#: attempts   unbounded  bounded
#: =========  =========  ========
#: 16         +1032 MiB  +130 MiB
#: 64         +2573 MiB  +131 MiB
#: =========  =========  ========
#:
#: The second column stops moving, which is the whole property: the cost is a
#: function of this number and not of how hard the route is hit. The
#: failed-login lockout is no substitute for it — the lockout counts failures
#: per attempted *name*, so rotating the name never trips it, and every attempt
#: in the table above claimed a different one.
#:
#: Two, because the memory is the scarce thing and the latency is what this
#: surface can afford to spend: the worst case stays ~128 MiB of argon2 rather
#: than gigabytes, and a login sharing the process with a *single* password
#: being set still finds the second token free.
#:
#: It does not make the login independent of that other caller, and this used to
#: claim it did. The queue is one FIFO over both routes, and the password hop
#: holds its token for the whole of :func:`_write` — the hash, the human-only
#: check, the session delete, the event and the commit — not for the hash alone.
#: Measured: 8 concurrent ``POST /api/humans/{id}/password`` put the owner's own
#: correct login at **0.572 s**, against 0.10 s idle. That is the same queueing a
#: flood of logins produces, from an authenticated caller instead of an anonymous
#: one; what this number bounds is the memory, not the wait. What it does not buy
#: is a queue-free login under load; that cost is stated where the limiter is.
#: Raising it buys throughput on two routes nobody should be calling in bulk.
ARGON2_CONCURRENCY = 2

#: The limiter itself, over the two calls in this module that run argon2 —
#: :func:`_verify_login` and the ``POST /api/humans/{id}/password`` write — and
#: nothing else. Every other blocking route on this surface keeps the default
#: thread limiter: none of them reserves 64 MiB to run, and lowering the global
#: one would slow the whole app to bound two routes.
#:
#: **Running argon2 is what selects for this limiter, not being unauthenticated
#: — and not being outside the session gate**, which two of the blocking routes
#: also are: ``PUT /api/uploads/{token}`` and ``GET /api/download/{token}``
#: redeem a capability URL, where the single-use token in the path *is* the
#: credential. Both keep the default limiter, because what bounds them is the
#: grant they redeem. The password-set route is the other way round — session
#: gate, authenticated human, and still here, because the gate says who may
#: hash, never how much memory the process holds while they do.
#:
#: Excess *queues* here rather than being refused. That buys a bounded,
#: self-healing cost in place of an unbounded, permanent one, and it is not
#: free: the queue is FIFO and unbounded, so 64 queued attempts put the owner's
#: own correct login 3.4 s behind them and 256 put it 13.4 s behind, against
#: 0.11 s idle. A sustained flood still degrades the login — the trade taken
#: here is that a slow login the human completes beats a memory exhaustion that
#: takes the process for good, and beats the 503 a bounded queue would answer
#: with, which is that same caller's switch for denying the login outright.
#:
#: Constructed at import, outside any event loop, which is precisely when anyio
#: hands back an adapter that materialises the real limiter on first use — so
#: this needs no lifespan hook and belongs to no single app instance, which is
#: right, because the memory it bounds belongs to the process.
_ARGON2_LIMITER = anyio.CapacityLimiter(ARGON2_CONCURRENCY)

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

#: What ``POST /api/assets`` admits: the rasters a note can inline and this
#: server can render.
#:
#: That route registers bytes and writes no describing node, so the thing that
#: describes them is the note carrying
#: ``![alt](/api/assets/<hash>/rendition/preview)`` — which only means anything
#: for an image. "Raster" is therefore a contract here rather than an accident
#: of history, and it is narrower than what ``assets.register_asset`` will
#: store, because the CLI registers a local file the operator already owns while
#: this one takes a file from a stranger. SVG is excluded with the rest — it is
#: a script-bearing document Pillow cannot render anyway.
INLINE_IMAGE_MIMES = assets.RECOGNISED_IMAGE_MIMES

#: What ``PUT /api/uploads/{token}`` admits: everything the sniffer can name.
#:
#: Those bytes become a subgraph — ``asset_ref`` + ``source`` +
#: ``derived_from`` + one ``block`` per page — so the right question is not "can
#: this be rendered" but "can this system act on it at all", which is exactly
#: what :data:`assets.RECOGNISED_MIMES` answers. Derived from the sniffer rather
#: than listed here, so the policy cannot drift from what the sniffer knows.
#:
#: Availability is deliberately *not* part of it: an install without the ``pdf``
#: extra still admits a PDF and reports in ``detail`` that no text came out,
#: because refusing at the door is a worse answer than the honest empty one.
INGESTIBLE_MIMES = assets.RECOGNISED_MIMES

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

#: How many of a cycle's events ``GET /api/cycles/{id}`` returns by default. A
#: cycle's diff *is* its event list, and a nightly run over a large graph can
#: emit thousands, so the journal reads a bounded window and says when the
#: bound bit (``events_truncated``) rather than presenting a short list as the
#: whole of it.
CYCLE_EVENT_LIMIT = 500

#: Methods the ``/api`` catch-all answers, so a wrong verb on an unknown route
#: is a JSON 404 rather than a bare 405 from the router.
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

#: Query-string values accepted as booleans (``?expand=1``, ``?expand=true``).
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

#: Characters kept when an id is echoed into a ``Content-Disposition`` filename.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


#: Exception → HTTP status. It covers every class ``cli._run`` catches — the
#: ``sqlite3`` and ``OSError`` rows are the **base** classes, so every
#: ``DatabaseError``/``IntegrityError``/``ProgrammingError``/``DataError`` lands
#: on a status instead of a generic 500 — plus the failures only a network
#: surface can meet (an oversized body, a client that hung up, a login name
#: under the failed-login lockout). A failure
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
    # 404 too: a named account that resolves to nothing. It is a `LookupError`
    # rather than a `ValueError`, so it inherited neither of the rows above and
    # escaped as a traceback and a generic 500 — the shape a consolidation
    # cycle meets, since the runner re-mints whoever asked for it from stored
    # state. The one flavour that is not a caller's bad name is a file holding
    # no internal agent at all, which cannot happen behind a migration runner
    # and whose message names migration `0014` rather than hiding behind a 500.
    auth.UnknownPrincipal: 404,
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
    # 429 — the login lockout refused the attempt (M5): a name with five
    # failed attempts inside fifteen minutes is refused up front, correct
    # password or not, until the window slides past the failures. The status
    # is about the *attempt* — too many of them — not the account, so it
    # applies identically to a name that does not exist (the lockout must not
    # be an existence oracle). It derives from PermissionError like the 401
    # above, so it needs this row or it would inherit OSError's 500.
    auth.LoginLocked: 429,
    # 403 — the grant model refused. Sessions mint human principals only and
    # humans are unfiltered, so this was once unreachable here by construction —
    # `POST /api/cycles` ended that: the runner's writes are the *gardener's*,
    # and migration `0014` grants it `main` and `meta` alone, so a cycle scoped
    # to any space created since is refused with the space's name and the `nodum
    # grant builtin-gardener <space> edit` that fixes it. Getting that sentence
    # to the browser intact is what `_failure_message` is scoped for.
    GrantNotPermitted: 403,
    # 403 too, and reachable a second way: `PUT /api/uploads/{token}` re-mints
    # the grant's principal inside `ingest.ingest_upload`, so a capability
    # outliving the account that authorised it fails there. Like
    # `GrantNotPermitted` it derives from OSError (via PermissionError) and
    # inherited the 500 below, which rewrote it as `storage error:
    # PrincipalDisabled` — a sentence a browser shows a human, and not a storage
    # failure at all.
    auth.PrincipalDisabled: 403,
    # 409 — the graph has grown past the event being undone, or a name is
    # taken: an account's, or a space's (including one an archived space still
    # reserves — `service._require_space_name_free`). All three derive from
    # ValueError; the more specific entries win (Starlette walks the
    # exception's MRO).
    UndoNotPossible: 409,
    # The cycle-sized version of the same 409: rows the cycle wrote have been
    # changed since, so the rollback refused rather than clobbering them. Listed
    # although it derives from `UndoNotPossible` and would inherit the status,
    # because its `conflicts` are what a UI renders and the row is where a
    # reader looks for the code that gets rendered under.
    RollbackConflict: 409,
    AccountExists: 409,
    SpaceNameTaken: 409,
    # 409 too, and the class says so itself: a cycle was asked for while one is
    # already running. It derives from `ValueError`, so it already rendered as a
    # clean 400 with the right message — but "a cycle is in progress" is a
    # conflict with current state, exactly `RollbackConflict`'s shape, and not a
    # malformed request. A client that retries on 409 and gives up on 400 was
    # being told the wrong thing.
    consolidate.CycleInProgress: 409,
    # 413 — the body passed the ceiling this server is willing to read, whether
    # it was declared at mint time (`urls.mint_upload`) or delivered here. Both
    # raise `urls.PayloadTooLarge`, which derives from ValueError; this row is
    # more specific, and Starlette walks the MRO, so it wins over the 400.
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

#: Where this module says the things nobody asked for: a misconfigured nightly
#: schedule at startup. Requests report failures in their own response body.
logger = logging.getLogger(__name__)

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


def _run_consolidation(
    *,
    scope: str | None,
    dry_run: bool,
    principal: Principal,
    path: str | Path | None,
) -> consolidate.ConsolidationOut:
    """Run a consolidation cycle on behalf of a principal :func:`_write` bound.

    The runner is the one domain entry point this surface reaches that takes
    *who asked* as a string rather than as a :class:`Principal`, and that is the
    runner's shape rather than a convenience here: the nightly scheduler calls
    the same function with no principal at all — nobody asked, the clock did —
    so the parameter has to be able to say ``scheduler``.

    Nothing about the round trip weakens the boundary, and two things make that
    true. The string comes from the principal :class:`SessionMiddleware`
    verified into this request's scope, which is the only identity this module
    can reach at all; and the runner re-mints it from *stored* state
    (``nodum.auth.principal_from_actor``), so a session whose account was
    disabled since login cannot start a cycle. The writes the cycle then makes
    are the in-process gardener's, because the gardener made them, and the
    journal row records the human who asked beside them — two questions, two
    answers, which is the whole of design decision G4.

    Args:
        scope: A space to confine the cycle to, or ``None`` for the whole file.
        dry_run: Rehearse it — every job computed, the report written, no graph
            event emitted.
        principal: Bound by :func:`_write`; never supplied by a caller.
        path: Explicit database path.

    Returns:
        The closed cycle and its typed report.
    """
    return consolidate.consolidate(
        scope=scope,
        dry_run=dry_run,
        triggered_by=principal.actor_string,
        path=path,
    )


def _consolidation_scheduler(
    at: time | None, db_path: str | Path | None
) -> scheduler.ConsolidationScheduler | None:
    """Build the nightly scheduler for this app, or ``None`` when it is off.

    ``at`` given wins; otherwise the environment decides
    (:data:`nodum.scheduler.ENV_CONSOLIDATE_AT`), and unset means off — a
    background process that writes to the graph without being asked is not
    something to enable by surprise (design decision J1).

    A value that is set but unparseable is **reported and then ignored**. The
    alternative is a server that refuses to start over a stray character in an
    optional schedule, and this one is announced on the console beside the two
    banners ``nodum serve`` already prints, so it is not the silent-disable that
    nobody notices for a month.
    """
    if at is None:
        try:
            at = scheduler.configured_time()
        except ValueError as exc:
            logger.warning("%s — the nightly consolidation cycle is off", exc)
            return None
    if at is None:
        return None
    return scheduler.ConsolidationScheduler(at=at, db_path=db_path)


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


#: This package's own name, read off ``__name__`` so it cannot drift from where
#: this module actually lives. It is the root of every module whose exceptions
#: are decisions rather than storage failures.
_PACKAGE_ROOT = __name__.partition(".")[0]


def _is_domain_failure(exc: Exception) -> bool:
    """Whether this exception is one ``nodum`` raised deliberately.

    The test is where the class was **defined**, which is the whole of the rule:
    a class this package declares is a decision this package made and its message
    is written for a human to read, while an ``OSError`` from ``builtins``, the
    OS, or a library is a failure whose text nobody here chose.
    """
    return type(exc).__module__.partition(".")[0] == _PACKAGE_ROOT


def _failure_message(exc: Exception) -> str:
    """Render one exception as the single line both surfaces report it with.

    ``database error: …`` is what the CLI prints for a SQLite failure. An
    ``OSError`` is the one deliberate divergence: ``cli._run`` appends the
    filename, and this surface must not — the path is the operator's on a
    terminal and a stranger's over a socket.

    That rewrite is scoped by :func:`_is_domain_failure`, and the scoping is the
    fix for a defect found twice. Four of this package's exceptions are
    ``PermissionError`` subclasses — ``auth.InvalidCredentials``,
    ``auth.PrincipalDisabled``, ``auth.LoginLocked`` and
    ``store.GrantNotPermitted`` — so all four
    fell into the ``OSError`` net, and the exemption used to be a literal tuple
    that nothing audited. ``PrincipalDisabled`` joined it when a live pass caught
    ``storage error: PrincipalDisabled`` in a browser; ``GrantNotPermitted`` was
    still missing, so the gardener's "you hold no grant on space 'research', run
    ``nodum grant builtin-gardener research edit``" reached the journal's toast
    as ``storage error: GrantNotPermitted`` — the space and the remedy both
    dropped, on the exact click the message was written for. A per-class
    exemption list is the defect; naming the *domain* instead means the next
    such class is exempt the day it is written.
    ``test_no_exception_this_package_defines_is_rewritten_as_a_storage_failure``
    enumerates the subtree by walking the package rather than restating a list.
    """
    if isinstance(exc, sqlite3.Error):
        return f"database error: {exc}"
    if isinstance(exc, OSError) and not _is_domain_failure(exc):
        return f"storage error: {exc.strerror or type(exc).__name__}"
    return str(exc)


def _exception_handler(status_code: int) -> Any:
    """Build the handler installed for one mapped exception class."""

    async def handler(request: Request, exc: Exception) -> Response:
        return _error(status_code, type(exc).__name__, _failure_message(exc))

    return handler


async def _rollback_conflict_handler(request: Request, exc: Exception) -> Response:
    """Render a refused rollback together with the rows that refused it.

    The one failure on this surface whose body carries more than ``type`` and
    ``message``, and the reason is decision C4: a rollback that cannot run
    **names what is in the way** instead of reporting that it failed, because a
    human told which four rows are blocking it can act and one told "rollback
    failed" cannot. ``conflicts`` is the
    :class:`~nodum.models.RollbackConflictOut` list verbatim — row id, kind,
    both event seqs and ops, who made the conflicting write, and the cycle it
    belonged to when it was another cycle's — which is exactly what the journal
    renders. Parsing that back out of a sentence is the alternative this avoids.

    The status still comes from :data:`EXCEPTION_STATUS`, so the code and the
    body cannot drift; only the body is richer.
    """
    conflicts = exc.conflicts if isinstance(exc, RollbackConflict) else []
    return EnvelopeResponse(
        {
            "error": {
                "type": type(exc).__name__,
                "message": _failure_message(exc),
                "conflicts": [conflict.model_dump(mode="json") for conflict in conflicts],
            }
        },
        status_code=EXCEPTION_STATUS[RollbackConflict],
    )


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


def _is_mcp_path(path: str) -> bool:
    """Is this the agent surface, whose credential is a header it must present?

    Exempt from the same two checks as :func:`_is_capability_path`, for the
    same reason and no wider a one: **a bearer token is not an ambient
    credential.** No browser attaches ``Authorization`` on its own, so there is
    nothing here for a cross-origin page to spend — and a page that has somehow
    been given an agent token could equally well have used ``curl``, which the
    origin gate was never able to stop anyway. Requiring a same-origin proof of
    a client that is not a browser would refuse every legitimate caller
    (an MCP client sends no ``Origin`` and no ``Sec-Fetch-Site``) while adding
    nothing an attacker has to defeat.

    The exemption is deliberately *this narrow*. Do not widen it to "requests
    with an ``Authorization`` header" — that would let any route opt out of CSRF
    protection by attaching a header a hostile page can make the browser send in
    some other flow. It is one path, and the path is a constant
    (:data:`nodum.mcp_server.MCP_PATH`) rather than a prefix, so nothing nests
    underneath it.

    Not exempt, on purpose, and exactly as for a capability URL:

    * the ``Host`` check — DNS rebinding is about which *server* was reached,
      which the credential's shape changes nothing about; and
    * the body ceiling.

    What replaces the skipped checks is not nothing:
    :class:`nodum.mcp_server.BearerGuard` refuses the request outright unless
    it presents an enabled agent's token, before the transport answers at all.

    Args:
        path: The normalised request path.

    Returns:
        Whether the path is the MCP transport.
    """
    return path == mcp_server.MCP_PATH


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

    Checks 2 and 3 are skipped for exactly two kinds of route, and only those
    two checks: the capability URLs (:func:`_is_capability_path`) and the MCP
    transport (:func:`_is_mcp_path`). Both are the checks that assume an
    ambient credential the request would be spending, and neither route has
    one — a capability URL's token is the whole authorisation, and an agent's
    bearer header is not something a browser attaches on its own. Checks 1 and
    4 apply to every request this server answers, those routes included.

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

        if _is_capability_path(scope["path"]) or _is_mcp_path(scope["path"]):
            # The CSRF checks below and the content-type check under them both
            # assume the request would arrive carrying an ambient credential.
            # Neither of these two has one. A capability URL's token *is* the
            # entire authorisation, and the MCP surface's credential is a
            # bearer header no browser attaches by itself — so in both cases
            # there is nothing for a cross-origin page to ride and nothing a
            # same-origin proof would add. See `_is_capability_path` and
            # `_is_mcp_path` for the full argument, and note that the `Host`
            # check above has already run and the body cap below still runs,
            # because neither of those is about ambient credentials.
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


def _bounded_str(body: dict[str, Any], key: str, *, max_chars: int) -> str:
    """Return a required string body field, refused above ``max_chars``.

    :func:`_required_str` with a ceiling, for the fields an **unauthenticated**
    caller supplies. A length nobody states is a length the caller picks, and
    the only limit under it here is :data:`MAX_REQUEST_BYTES`: a 32 MiB name is
    a well-formed request that costs a 32 MiB row in an append-only table, and a
    32 MiB password is 32 MiB fed to argon2 at the full work factor.

    The ceilings themselves are :data:`nodum.service.MAX_HUMAN_NAME_LENGTH` and
    :data:`nodum.service.MAX_PASSWORD_LENGTH` — the service's, read from there
    rather than restated here, because they are the same numbers
    :func:`nodum.service.create_human` and
    :func:`nodum.service.set_human_password` refuse a *write* above. An adapter
    that owned its own copy is how the caps came to be one-sided: a 300-char
    name and a 5000-char password were both storable, and both then met this
    function's 400 on the way back in.

    The refusal never echoes the value — a message quoting a 200 kB name puts
    it in the response body and the server log instead of the events table.

    Raises:
        ValueError: If the key is missing, null, not a string, or longer than
            ``max_chars``.
    """
    value = _required_str(body, key)
    if len(value) > max_chars:
        raise ValueError(f"field {key!r} must be at most {max_chars} characters")
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


def _optional_int(body: dict[str, Any], key: str, *, default: int) -> int:
    """Return an optional integer body field, defaulting when absent or null.

    :func:`_required_int` with a default, and it refuses ``bool`` for the same
    reason: ``true`` as a result cap is a caller mistake and not a number.

    Raises:
        ValueError: If the key is present, non-null, and not an integer, or does
            not fit in a signed 64-bit integer.
    """
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"field {key!r} must be an integer")
    return _bounded_int(value, key)


def _optional_bool(body: dict[str, Any], key: str, *, default: bool = False) -> bool:
    """Return an optional boolean body field, defaulting when absent or null.

    A string ``"false"`` is refused rather than coerced: this is a JSON body,
    where ``false`` exists, and every non-empty string is truthy — silently
    reading ``"false"`` as *run for real* is the kind of coercion a rehearsal
    flag must not have.

    Raises:
        ValueError: If the key is present, non-null, and not a boolean.
    """
    value = body.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"field {key!r} must be true or false")
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


def _state_param(params: QueryParams, name: str = "state") -> NodeState | None:
    """Read a search state filter for a service call.

    Absent → the service default ``"active"``; ``"any"`` → ``None`` (the
    service's "no filter", meaning every state); any other value must be one
    of :data:`~nodum.vocab.STATES` or the request is refused with the same
    sentence the service would have refused it with. An absent parameter must
    not become ``None`` — that would silently widen a default search to every
    state, where the CLI's ``--state`` and the MCP server's ``filters.state``
    both default to ``active``.
    """
    state = params.get(name)
    if state is None:
        return "active"
    if state == "any":
        return None
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    return state


def _state_filter(params: QueryParams, name: str = "state") -> NodeState | None:
    """Read a raw state query parameter for a listing route.

    The listing routes pass the parameter through unchanged (absent → the
    service's "no filter"), so ``"any"`` is refused here exactly as the
    service refuses it — a listing has no pseudo-value for every state, and
    the refusal is the service's own sentence.
    """
    state = params.get(name)
    if state is None:
        return None
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    return state


def _edge_states_param(params: QueryParams) -> list[NodeState] | None:
    """Repeatable edge-state filters, each narrowed against :data:`STATES`.

    The sentence is the service's own, so a refused value renders identically
    whether the route or the service raises it.
    """
    edge_states = _list_param(params, "edge_state", "edge_states")
    if edge_states is None:
        return None
    narrowed: list[NodeState] = []
    for edge_state in edge_states:
        if edge_state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {edge_state!r}")
        narrowed.append(edge_state)
    return narrowed


def _proposal_filters(source: Any) -> dict[str, Any]:
    """Pick the review-queue filter keys out of a body or a query string.

    An allowlist by construction: keys outside :data:`PROPOSAL_FILTERS` are
    never read, so nothing a caller invents reaches a service argument. The
    ``kind`` value is narrowed against :data:`PROPOSAL_KINDS` here, with the
    service's own sentence, so a refused value renders identically whether
    this helper or the service raises it.
    """
    filters = {key: source.get(key) for key in PROPOSAL_FILTERS}
    # `agent` is the review UI's word for the proposing author.
    if filters["created_by"] is None:
        filters["created_by"] = source.get("agent")
    kind = filters["kind"]
    if kind is not None and kind not in PROPOSAL_KINDS:
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    return filters


def _search_filters(params: QueryParams) -> dict[str, Any]:
    """Everything ``GET /api/search`` narrows a query with, except the query.

    An allowlist by construction, like :func:`_proposal_filters`: the keys are
    written here and nothing a caller invents becomes an argument. It exists
    because ``?nl=1`` sends the identical filters to a different function
    (``answers.natural_search`` rather than ``search.search``), and two
    hand-written argument lists that must stay identical are two argument lists
    that will not.
    """
    return {
        "k": _int_param(params, "limit", "k", default=10),
        "state": _state_param(params),
        "type": params.get("type"),
        "created_by": params.get("created_by"),
        "created_after": params.get("created_after"),
        "created_before": params.get("created_before"),
        "include_meta": _bool_param(params, "include_meta"),
        "space": params.get("space"),
        "expand": _bool_param(params, "expand"),
        "as_of": params.get("as_of"),
    }


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


#: What a refusal on the widest network route points at instead of itself.
#:
#: ``PUT /api/uploads/{token}`` admits everything the sniffer can name, so there
#: is no wider *network* route to redirect to: the remaining way in for a
#: ``.docx``, a ``.zip``, or anything else carrying NULs and no signature is the
#: CLI, where an operator registers a file they already own. That tolerance is
#: the pipeline's, unchanged — a file no handler claims is still registered and
#: described. ``POST /api/assets`` gets no such line, because the type it just
#: refused is very likely ingestible one route over.
INGEST_CLI_HINT = (
    " — 'nodum ingest file <path>' is the way in for a type this surface will not take"
)


def _refuse_unsupported_upload(
    spooled: Path,
    original_name: str,
    *,
    admits: frozenset[str],
    pixel_limit: int | None,
    cli_hint: bool,
) -> None:
    """Refuse an upload the requested route cannot act on, before it is stored.

    One policy with the route's capability as its only parameter (Phase 4 note
    01 D2). The type is decided by the *bytes*, never by ``original_name`` or the
    client's declared ``Content-Type``: both are chosen by whoever sent the file,
    so a renamed executable used to be stored as ``image/png`` on one route and
    ingested unexamined on the other.

    Where the bytes turn out to be an image, the header is read in the same pass.
    What that read *refuses* is the caller's to say, because two different
    questions are asked of it: a decompression bomb is dangerous on any route,
    while the 40 MP rendition ceiling is a statement about what this server can
    render and belongs only to the route whose whole purpose is a rendition
    (review F9 — capability must not gate admission, exactly as an install
    without the ``pdf`` extra still admits a PDF).

    Args:
        spooled: The temp file holding the uploaded bytes.
        original_name: The client's filename, used only in the error message —
            including the image refusal's, which must not name the spool path.
        admits: The MIME types this route can act on —
            :data:`INLINE_IMAGE_MIMES` or :data:`INGESTIBLE_MIMES`.
        pixel_limit: Pixel ceiling for an image, or ``None`` to keep only the
            bomb guard (see :func:`assets.check_image_pixel_budget`).
        cli_hint: Whether the refusal should name ``nodum ingest file``. True on
            the widest *network* route, which has nowhere wider to point at —
            passed rather than inferred from ``admits``, because a set-value
            comparison standing in for "is this the widest route" reads as an
            accident and breaks the moment two routes admit the same set.

    Raises:
        UnsupportedRendition: If the bytes are not one of ``admits``, or are an
            image whose header Pillow cannot read.
        ImageTooLarge: If the image is a bomb, or is above ``pixel_limit``.
    """
    sniffed = assets.sniff_mime(spooled)
    if sniffed not in admits:
        raise UnsupportedRendition(
            f"{original_name!r} is {sniffed or 'not a type this API recognises'}; "
            f"this route accepts {', '.join(sorted(admits))}"
            f"{INGEST_CLI_HINT if cli_hint else ''}"
        )
    if sniffed.startswith("image/"):
        assets.check_image_pixel_budget(spooled, limit=pixel_limit, name=original_name)


# ── Serving original bytes ────────────────────────────────────────────────────


async def _spooled_chunks(spool: Path) -> AsyncIterator[bytes]:
    """Yield a spooled original's bytes in bounded chunks, unlinking the file after.

    The point of the generator: an original may be a gigabyte, and reading it
    into a ``bytes`` to hand to a ``Response`` would put all of it in the
    server's memory to send a copy the client reads at its own pace. The file
    outlives the handler that spooled it — :func:`_original_response` wrote it
    and closed its handle before returning — so this owns the deletion,
    including the one that matters, when a client hangs up mid-transfer and
    Starlette closes the generator instead of draining it.

    Args:
        spool: The temporary file holding the original's bytes.

    Yields:
        Successive chunks of at most :data:`UPLOAD_CHUNK_BYTES`.
    """
    try:
        with spool.open("rb") as handle:
            while chunk := handle.read(UPLOAD_CHUNK_BYTES):
                yield chunk
    finally:
        spool.unlink(missing_ok=True)


def _original_response(asset_hash: str, path: str | Path | None) -> Response:
    """Stream one asset's stored original bytes as a download.

    Everything about the response says "bytes, saved to disk, interpreted by
    nobody": :data:`DOWNLOAD_CONTENT_TYPE` rather than the stored MIME,
    ``nosniff`` so a browser does not overrule it, and ``attachment`` so it is
    never rendered as a document in this origin (see the constant for why that
    is the whole game on a file host). The filename is the content address run
    through :data:`_SAFE_FILENAME_RE` — the one name attached to these bytes
    that no stranger chose.

    The bytes are **spooled to a temporary file before anything streams**: the
    blob copy is one tight loop on a short-lived connection, and the
    client-paced response then serves a plain file with no database handle
    open. Streaming the blob directly would hold its open read transaction —
    and with it the WAL snapshot, since ``conn.in_transaction`` stays False —
    for the whole transfer, which a stalled client could pin for the life of
    the connection. The spool is disk, not memory, and the connection is
    closed before the response is returned (:func:`_spooled_chunks` owns the
    file's deletion, client disconnect included).

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
    # A NamedTemporaryFile rather than the TemporaryDirectory the upload
    # routes use: this file must outlive the handler that created it, so its
    # deletion is the streaming generator's job, not a ``with`` block's.
    # (SIM115: the close below is a real ``with spool:``, just not on this line —
    # a plain context manager would delete the file at block exit.)
    spool = tempfile.NamedTemporaryFile(prefix="nodum-download-", suffix=".tmp", delete=False)  # noqa: SIM115
    spool_path = Path(spool.name)
    try:
        with spool:
            conn = db.connect(path)
            try:
                blob = assets.open_original(conn, asset_hash)
                with blob:
                    while chunk := blob.read(UPLOAD_CHUNK_BYTES):
                        spool.write(chunk)
                size = spool.tell()
            finally:
                conn.close()
    except Exception:
        # The spool outlives this call, so a failure before the response
        # exists must not leave it behind.
        spool_path.unlink(missing_ok=True)
        raise
    filename = f"nodum-{_SAFE_FILENAME_RE.sub('-', asset_hash)[:64]}"
    return StreamingResponse(
        _spooled_chunks(spool_path),
        media_type=DOWNLOAD_CONTENT_TYPE,
        headers={
            "content-length": str(size),
            "content-disposition": f'attachment; filename="{filename}"',
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


# ── Routing ───────────────────────────────────────────────────────────────────


class _NoHeadRoute(Route):
    """A :class:`~starlette.routing.Route` that never answers HEAD.

    Starlette adds ``HEAD`` to every route whose methods include ``GET`` —
    even when ``methods=["GET"]`` was passed explicitly — and answers a HEAD
    request by running the handler with the body suppressed. For an ordinary
    read that is a feature. For the download route it is not: the handler
    **spends the single-use token**, so a HEAD probe would burn the
    capability and the real GET behind it would come back refused (M6).
    Removing HEAD from the method set sends the request on to the ``/api``
    catch-all, which answers 405 with ``Allow: GET``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.methods:
            self.methods = {method for method in self.methods if method != "HEAD"}


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
    consolidate_at: time | None = None,
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
        consolidate_at: Local wall-clock time to run the nightly consolidation
            cycle at. ``None`` reads :data:`nodum.scheduler.ENV_CONSOLIDATE_AT`,
            which is unset by default — so the schedule is off unless somebody
            asked for it, and ``nodum serve`` needs no flag to keep it that way.

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

    def _verify_login(name: str, password: str) -> Principal:
        """The blocking half of :func:`login`: lockout, argon2, failure event.

        Split out so the whole of it runs in one thread-pool hop rather than
        three — the lockout query, the password verification and the failure
        event are each a synchronous database or CPU call, and interleaving
        them with the loop would put the argon2 work back on it.

        Raises:
            LoginLocked: If the name is under the failed-attempt lockout. No
                event is written for this: see :func:`login`.
            InvalidCredentials: If the name or password does not verify. This
                one *is* written, as ``human.login_failed``.
        """
        if service.login_is_locked(name, path=db_path):
            raise auth.LoginLocked(
                f"login for {name!r} is locked: too many failed attempts, try again later"
            )
        try:
            return auth.verify_login(name, password, path=db_path)
        except auth.InvalidCredentials:
            service.record_auth_event(
                "human.login_failed",
                {"name": name, "reason": "invalid credentials"},
                path=db_path,
            )
            raise

    def _complete_login(principal: Principal) -> str:
        """The blocking tail of :func:`login`: the session row, then the event.

        One hop for the same reason :func:`_verify_login` is one, and for one
        more: :func:`service.record_auth_event` opens a connection, inserts and
        commits, and it used to do all three **on the loop** after both
        threadpool hops — under a docstring here that said the whole handler
        ran off it. Either the write moves or the claim does; this is the write
        moving.
        """
        session_id = auth.create_session(principal.id, path=db_path)
        service.record_auth_event("human.login", {"human_id": principal.id}, path=db_path)
        return session_id

    async def login(request: Request) -> Response:
        """Password login — the one ``/api`` route outside the session gate.

        Verifies name + password through :func:`auth.verify_login` (argon2id,
        constant-time on failure), creates a server-side session row (30-day
        sliding expiry), and sets the cookie ``HttpOnly; SameSite=Strict`` —
        JavaScript cannot read it and a cross-site request never carries it,
        which is what lets the origin guard and the session gate each do
        their own job. Failure is a 401 with no cookie, indistinguishable
        between "no such name" and "wrong password".

        Every attempt that reaches a password check is event-logged: a success
        writes ``human.login``, a refused credential ``human.login_failed``
        (the auth half of the audit trail, via
        :func:`service.record_auth_event`). The failed-login **lockout** (M5)
        throttles brute force: a name with five failed attempts inside fifteen
        minutes is refused up front with a 429, correct password or not, until
        the window slides past the failures. The lockout keys on the attempted
        name, so it applies identically to names that do not exist.

        **A refusal by the lockout writes no event of its own** (M2). It used
        to, on the reasoning that a guesser who keeps trying should keep the
        lockout fresh — which is true and is also two defects. The lockout is
        the one ``/api`` route outside the session gate, so that made an
        unauthenticated request an unbounded append to the append-only event
        log; and it handed any local process a permanent lockout of the real
        human, by re-arming the window every fifteen minutes forever. The five
        failures that *caused* the lockout are on the record, which is what the
        audit trail needs; the refusals they earn are a rate limit, and a rate
        limit that logs is a rate limit that can be turned around.

        **Both fields are length-capped before anything else happens**, at
        :data:`service.MAX_HUMAN_NAME_LENGTH` and
        :data:`service.MAX_PASSWORD_LENGTH` — the *service's* ceilings, which
        are also the ones :func:`service.create_human` and
        :func:`service.set_human_password` refuse a write above, so a name or a
        password any surface stores is one this route will still look at. The
        refusal is the ordinary 400 a malformed body gets, and it lands ahead of
        the lockout query, ahead of argon2, and ahead of the point where a
        failure would have appended the claimed name to the event log — which is
        the whole reason the cap has to be here *as well*, and not inside the
        thread.

        **Every blocking call this handler makes runs off the event loop, and
        the argon2 one is bounded.** Argon2id is ~100 ms of deliberate work —
        the constant-time path spends it on names that do not exist too — and
        this is the one route an unauthenticated caller can reach, so inline it
        is a stalled server at ten requests a second (see the module docstring's
        list, which this route belongs on). Off the loop and *unbounded* it is
        that same caller's memory-exhaustion primitive instead: the default
        thread limiter admits 40 at once and each verification reserves 64 MiB
        (measurements under :data:`ARGON2_CONCURRENCY`). So
        :func:`_verify_login` runs under :data:`_ARGON2_LIMITER` — the excess
        queues rather than being refused, which costs this route latency under
        load and is argued through where the limiter is defined — and
        :func:`_complete_login` takes the session row and the ``human.login``
        event in one further hop on the default limiter.
        """
        body = await _json_body(request)
        name = _bounded_str(body, "name", max_chars=service.MAX_HUMAN_NAME_LENGTH)
        password = _bounded_str(body, "password", max_chars=service.MAX_PASSWORD_LENGTH)
        principal = await anyio.to_thread.run_sync(
            _verify_login, name, password, limiter=_ARGON2_LIMITER
        )
        session_id = await run_in_threadpool(_complete_login, principal)
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
        moment ago; deleting is idempotent regardless. The verified session's
        human is who the ``human.logout`` event records — the last entry a
        session writes, on the auth half of the audit trail.
        """
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id is not None:
            auth.delete_session(session_id, path=db_path)
        service.record_auth_event(
            "human.logout", {"human_id": _session_principal(request).id}, path=db_path
        )
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
            state=_state_filter(params),
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
        payload = NodeCreateIn.model_validate(body)
        node = _write(
            request,
            service.create_node,
            type=payload.type,
            title=payload.title,
            content=payload.content,
            parent_id=payload.parent_id,
            props=payload.props,
            space=payload.space,
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
        """Update the named fields of a node, and only those.

        Absent and null are distinct: pydantic records which fields were
        actually sent, and the handler reads that. ``title: null`` clears the
        title (a documented web affordance); ``content: null`` and
        ``props: null`` are refused, because those fields are non-nullable in
        the read models and a null would corrupt read-back.
        """
        body = await _json_body(request)
        payload = NodeUpdateIn.model_validate(body)
        fields: dict[str, Any] = {}
        for name in ("title", "content", "props"):
            if name not in payload.model_fields_set:
                continue
            value = getattr(payload, name)
            if value is None and name != "title":
                raise ValueError(f"{name} cannot be null")
            fields[name] = value
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
        """List edges, optionally filtered by incident node, type, state, or validity window."""
        params = request.query_params
        edges = service.list_edges(
            node_id=_param(params, "node_id", "node"),
            type=params.get("type"),
            state=_state_filter(params),
            as_of=params.get("as_of"),
            principal=_session_principal(request),
            limit=_int_param(params, "limit", default=500),
            path=db_path,
        )
        return EnvelopeResponse(list_envelope("edges", edges))

    async def create_edge(request: Request) -> Response:
        """Create a typed, directed edge between two nodes."""
        body = await _json_body(request)
        payload = EdgeCreateIn.model_validate(body)
        edge = _write(
            request,
            service.create_edge,
            payload.src_id,
            payload.dst_id,
            payload.type,
            props=payload.props,
            confidence=payload.confidence,
            path=db_path,
        )
        return EnvelopeResponse(envelope(edge))

    # ── Search and link suggestions ───────────────────────────────────────

    async def search(request: Request) -> Response:
        """Hybrid search (BM25 + vector, RRF-fused) with optional graph expansion.

        ``?nl=1`` layers a model-written query on top (design E3): the model
        contributes search *terms*, and every ranked signal, filter and cap
        below them is unchanged. The response then carries the ordinary result
        plus a ``rewrite`` object saying what was asked on the human's behalf —
        an **additive** shape, so a request without ``nl`` is byte-identical to
        what it has always been and to what ``nodum search`` prints.

        With no provider the rewrite is a no-op that says so and the search
        still runs, which is E3's second reason for layering rather than
        replacing: search must work without a model. The rewrite runs off the
        event loop for the reason ``POST /api/cycles`` does — a model call is
        seconds of work on this hardware and the loop is single-threaded.

        **Both branches go through the thread pool**, not just the rewrite. The
        ordinary branch is not a row read either: it catches two projectors up
        and probes the index once per query term, and one 400-term ``GET``
        (a 4 KB query string, nothing exotic) was measured holding the loop for
        126 ms — long enough that ``/healthz``, the SPA and every other tab
        waited behind a single search box.
        """
        params = request.query_params
        query = _required_param(params, "q", "query")
        if _bool_param(params, "nl"):
            natural = await run_in_threadpool(
                answers.natural_search,
                query,
                **_search_filters(params),
                principal=_session_principal(request),
                path=db_path,
            )
            return EnvelopeResponse(envelope(natural))
        result = await run_in_threadpool(
            search_module.search,
            query,
            **_search_filters(params),
            principal=_session_principal(request),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    async def ask(request: Request) -> Response:
        """Answer a question from the graph, with citations, or say it could not.

        **Nothing writes** (design E1), and ``answered`` is computed from
        citations that resolve to nodes this session can read — never from the
        model's own claim to have answered, which was measured returning
        ``true`` for a question its context could not answer.

        **``answered: true`` is four deterministic checks and not a claim that
        the answer is true** — ``nodum.answers.ask`` states each one and what it
        is worth. A client rendering this must not stop at the boolean: a note
        can reach the model **in part** (``truncated_notes``, and ``truncated``
        on every citation), notes the retrieval found can be missing altogether
        (``dropped``), and ``considered`` is empty whenever no call was made, so
        it never claims a note reached a model that was never called.

        Every failure is a 200 carrying ``answered: false`` and a ``refusal``
        sentence rather than a 5xx: a provider that is not configured, one that
        cannot be reached, an output ceiling, a filled context, an exhausted
        budget. The request was well formed and the install could not answer it,
        which is an outcome and not an error — so one response shape covers
        every way this can end and a client reads one field. A malformed
        *request* (no question, a ``k`` below 1, a space that does not resolve)
        is still the ordinary 400, because saying "the model could not answer"
        about a client bug would hide the bug.

        It runs off the event loop for the reason ``POST /api/cycles`` does.
        """
        body = await _json_body(request)
        result = await run_in_threadpool(
            answers.ask,
            _required_str(body, "question"),
            k=_optional_int(body, "k", default=answers.DEFAULT_ASK_K),
            space=_optional_str(body, "space"),
            principal=_session_principal(request),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    async def summarize(request: Request) -> Response:
        """Summarise a node and its neighbourhood. Reads only (design E1).

        The subgraph read is the bound, and it happens whether or not a provider
        is configured, so a node that does not resolve is a 404 rather than
        "no LLM provider configured" — the wrong answer to the wrong question.

        **What may be sent is narrower than what this session may read.** The
        walk returns archived, proposed and meta-space nodes — ``subgraph``
        filters edges by state and nodes not at all — and none of them go to the
        provider, because ``/ask`` cannot reach any of them at any ``k`` and two
        endpoints on one install must not disagree about what leaves the
        machine. They are named in ``withheld``, and every note carries its
        ``state``.

        Design E1 sketches an opt-in ``propose=true`` that files the summary as
        a reviewable ``proposed`` version. It is deliberately absent: 5b-i is
        cut exactly at the line where a model call causes a write, and this
        route is on the read side of it.
        """
        body = await _json_body(request)
        result = await run_in_threadpool(
            answers.summarize,
            _required_str(body, "node_id"),
            depth=_optional_int(body, "depth", default=answers.DEFAULT_SUMMARY_DEPTH),
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
            edge_states=_edge_states_param(params),
            min_confidence=_float_param(params, "min_confidence"),
            created_by=params.get("created_by"),
            node_types=_list_param(params, "node_type", "node_types"),
            as_of=params.get("as_of"),
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
        second registration path with weaker guarantees. The registration
        streams the whole file and runs off the event loop; the admission
        checks below read only the file's header and stay inline.

        Three checks happen before the bytes are offered to the store, and each
        one exists because the version without it was exploitable:

        * **Size** is bounded by :class:`RequestGuardMiddleware` *before*
          Starlette buffers the part. ``AssetTooLarge`` was the only limit
          before, and it fired after the whole body had been spooled once by
          the parser and copied a second time by this handler — a 400 MB upload
          measured 839 MB of ``/tmp``, and tripping the real 1 GB limit needed
          more than 2 GB of it.
        * **Type** is sniffed from the bytes (:func:`assets.sniff_mime`), not
          read off the filename, so a renamed ``.exe`` is refused rather than
          stored under ``image/png``. This route admits
          :data:`INLINE_IMAGE_MIMES` — the rasters a note can inline — and a
          document belongs on the capability route, which ingests it.
        * **Pixel count** is read from the image header, so a 612 KB PNG that
          decodes to 14000×14000 is refused here rather than raising
          ``DecompressionBombError`` out of the rendition endpoint as a 500.
          This is the one route where ``assets.MAX_IMAGE_PIXELS`` gates
          *admission*: everything it takes is here to be rendered, so bytes
          above the rendition ceiling have no purpose it can serve. The
          capability route beside it keeps the bomb guard and drops the ceiling.
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
                _refuse_unsupported_upload(
                    spooled,
                    original_name,
                    admits=INLINE_IMAGE_MIMES,
                    # This route exists to produce renditions, so the rendition
                    # ceiling is an admission rule here and nowhere else.
                    pixel_limit=assets.MAX_IMAGE_PIXELS,
                    cli_hint=False,
                )
                # register_asset streams the whole file (up to the 1 GB blob
                # limit) into the store, hashing and copying it — the M22
                # measured case — so it runs off the event loop. The refusal
                # above reads only the header and stays inline.
                asset = await run_in_threadpool(
                    assets.register_asset, spooled, name=original_name, path=db_path
                )
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

        A cache miss renders the image — Pillow decode, or pypdfium2
        rasterisation for a PDF page, which is seconds on a big document — so
        the render runs off the event loop (M22). The stored-bytes read that
        follows is a bounded WebP and stays inline.
        """
        rendition = await run_in_threadpool(
            assets.get_rendition,
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

        The ingest itself — fetch, register, extract, describe — is the M22
        measured case (20.8 s for one 20.8 MB PDF, holding the loop the whole
        time) and runs off the event loop through
        :func:`~starlette.concurrency.run_in_threadpool`; :func:`_write` is
        what goes to the thread, so the principal is still bound in the one
        place this module binds one.
        """
        body = await _json_body(request)
        by_url = body.get("url") is not None
        if by_url == (body.get("path") is not None):
            raise ValueError("ingest takes exactly one of 'path' and 'url'")
        operation = ingest.ingest_url if by_url else ingest.ingest_file
        # An ingest fetches (url branch), registers up to a gigabyte, extracts
        # and describes — the M22 measured case — so, like POST /api/cycles,
        # what goes to the thread is _write itself and the principal is still
        # bound in the one place this module binds one.
        result = await run_in_threadpool(
            _write,
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

        The bytes are spooled to a temp file and streamed back rather than
        held in memory or pinned to the database for the client-paced
        transfer, and served as an opaque attachment nothing will render: see
        :data:`DOWNLOAD_CONTENT_TYPE`. The spool copies the whole original (up
        to the 1 GB blob limit) and runs off the event loop (M22); only the
        streaming back is client-paced.
        """
        row = urls.consume(request.path_params["token"], kind="download", path=db_path)
        # _original_response copies the whole original (up to the 1 GB blob
        # limit) into a spool file before anything streams — the same big-read
        # class as the M22 routes — so the copy runs off the event loop.
        return await run_in_threadpool(_original_response, row["asset_hash"], db_path)

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

        **The type policy applies here too**, and it is the same one
        ``POST /api/assets`` runs — :func:`_refuse_unsupported_upload` with this
        route's own admitted set, :data:`INGESTIBLE_MIMES`. It cannot run before
        the body has arrived, because it reads the bytes; it runs before
        anything is stored or described. The grant's declared ``mime`` is not
        consulted: it was a promise made at mint time about a file that had not
        moved yet, and the bytes are the only evidence. A refusal still spends
        the token — ``urls.consume`` ran first, by design — so the client
        re-mints to retry.

        What it does **not** apply is the 40 MP rendition ceiling. These bytes
        become knowledge rather than a thumbnail, and a 600 dpi A3 scan is ~70 MP
        — refusing it here would make capability gate admission, which this
        policy refuses to do everywhere else (an install without the ``pdf``
        extra still admits a PDF). The decompression-bomb guard is about danger
        rather than capability, so it applies here as on every route.

        **A client that hangs up mid-upload is answered with 499, and the graph
        is not modified for it.** The disconnect rule: the token is spent by
        the attempt — ``urls.consume`` ran first — but the ingest only runs
        for a client still listening. The body may have arrived in full (all
        its chunks reached the spool file) while the client that sent them is
        already gone, so the route checks :meth:`Request.is_disconnected`
        twice: once the body stream has finished, and once more before
        :func:`nodum.ingest.ingest_upload`, because the type policy between
        them reads and analyses the file and a client can vanish during that
        window too. An ingest nobody is listening for is a graph change with
        no party able to read its outcome — and the retry, which must re-mint
        anyway, would find its document already described.

        The ingest itself registers up to a gigabyte and describes it — the M22
        measured class — so it runs off the event loop through
        :func:`~starlette.concurrency.run_in_threadpool`.
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
            if await request.is_disconnected():
                raise ClientDisconnect()
            _refuse_unsupported_upload(
                spooled,
                original_name,
                admits=INGESTIBLE_MIMES,
                # No rendition ceiling: these bytes are being turned into
                # knowledge, and a 600 dpi A3 scan (~70 MP) is an ordinary
                # document. The bomb guard still runs.
                pixel_limit=None,
                cli_hint=True,
            )
            if await request.is_disconnected():
                raise ClientDisconnect()
            # ingest_upload registers up to a gigabyte and describes it — the
            # M22 measured class — so it runs off the event loop.
            result = await run_in_threadpool(ingest.ingest_upload, row, spooled, path=db_path)
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

    # ── Consolidation cycles: the dream journal (design §8.4) ─────────────

    async def list_cycles(request: Request) -> Response:
        """The consolidation journal, newest first.

        Human-only in the service for the reason the event log is: a journal
        entry says what the gardener did across every space in the file.
        """
        limit = _int_param(request.query_params, "limit", default=50)
        cycles = service.list_cycles(
            limit=limit, principal=_session_principal(request), path=db_path
        )
        return EnvelopeResponse(list_envelope("cycles", cycles))

    async def run_cycle(request: Request) -> Response:
        """Run a consolidation cycle now and answer with its journal entry.

        The on-demand half of "a cycle runs, on demand and on a schedule". The
        schedule is off unless configured, so without this the human surface
        could never produce a journal entry at all — a dream journal that can
        only fill itself up overnight, on an install that has not opted into
        overnight, shows an empty table forever.

        ``scope`` confines the cycle to one space and ``dry_run`` rehearses it:
        every job computed, the report written, and **no graph event emitted**,
        which is the checkable form of "it changed nothing". Both are the
        runner's own parameters; this route invents neither.

        The response is the closed cycle plus its report typed — the same two
        things ``GET /api/cycles/{id}`` returns, so a caller that just ran one
        needs no second request to render it.

        **It runs off the event loop.** It joins the other read-heavy and
        blocking handlers on the thread pool for the same reason: a cycle is
        every job over every node in scope, and inline it would hold the
        single-threaded loop for its whole length — 3.75 s measured on 450
        nodes without embeddings, minutes with them — so ``/healthz`` and the
        SPA would freeze with it. What is handed to the thread is
        :func:`_write`, not the runner, so the principal is still bound where
        this module binds every principal.
        """
        body = await _json_body(request)
        result = await run_in_threadpool(
            _write,
            request,
            _run_consolidation,
            scope=_optional_str(body, "scope"),
            dry_run=_optional_bool(body, "dry_run"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

    async def get_cycle(request: Request) -> Response:
        """One journal entry: the row, its metrics, and the events it wrote.

        The diff a journal renders is ``list_events`` narrowed to this cycle —
        the same append-only log every other read comes from — so the entry
        cannot become a second record that disagrees with what happened. The
        cycle row stores no diff of its own, and this route builds none: it
        composes two reads into one round trip and nothing else. ``?limit=``
        bounds the event window and ``events_truncated`` says when it bit.
        """
        cycle_id = request.path_params["id"]
        limit = _int_param(request.query_params, "limit", default=CYCLE_EVENT_LIMIT)
        cycle = service.get_cycle(cycle_id, principal=_session_principal(request), path=db_path)
        events = service.list_events(
            _session_principal(request), limit=limit, cycle_id=cycle_id, path=db_path
        )
        metrics = (cycle.report or {}).get("metrics")
        return EnvelopeResponse(
            envelope(
                CycleDetailOut(
                    cycle=cycle,
                    metrics=metrics if isinstance(metrics, dict) else {},
                    events=events,
                    events_truncated=len(events) >= limit,
                )
            )
        )

    async def abandon_cycle(request: Request) -> Response:
        """Close an interrupted cycle as ``failed`` — the door out of a stuck run.

        A cycle left ``running`` by a ``SIGKILL``, a power cut, or a shutdown
        that cancelled the nightly task in flight makes its own writes
        irreversible: ``rollback`` refuses a cycle that has not closed and
        ``undo`` refuses every event a cycle stamped. Without this the advice
        ("close it first") named an operation no surface offered.

        Human-only in the service, which sessions satisfy by construction here.
        It refuses a cycle that is not ``running`` (400): one that has said how
        it ended is not abandoned, and re-closing it would overwrite that
        record. What the run already wrote is untouched — taking that back is
        ``POST /api/cycles/{id}/rollback``, which this is what unlocks.
        """
        cycle = _write(request, service.abandon_cycle, request.path_params["id"], path=db_path)
        return EnvelopeResponse(envelope(cycle))

    async def stop_cycle(request: Request) -> Response:
        """Ask a ``running`` cycle to stop, and record who asked (design K1–K3).

        The kill switch, and the one write on this surface that closes nothing:
        it stamps ``stop_requested_by``/``stop_requested_at`` and returns the row
        still ``running``. The run notices at its next check and closes its own
        entry ``failed``, so the journal says the operator stopped that night.

        **Deliberately not ``/abandon``, and not built on it.** Abandoning is a
        repair — a human closing a dead process's entry from outside so its
        writes become reversible — while a stop is an instruction to a live run.
        A human reading a ``failed`` cycle at 09:00 needs to know which happened,
        and one route serving both would erase exactly that.

        It reverses nothing either: what the run already wrote stays, stamped
        with the cycle, and ``POST /api/cycles/{id}/rollback`` is what takes it
        back once the entry has closed. Human-only in the service, which sessions
        satisfy by construction here.

        It refuses a cycle that is not ``running`` (400) — nothing is left to obey
        it — and answers **200** to a second stop rather than refusing, keeping
        the first asker: a switch that raised on the second press would make a
        human doubt whether the first one worked.
        """
        cycle = _write(request, service.request_stop, request.path_params["id"], path=db_path)
        return EnvelopeResponse(envelope(cycle))

    async def roll_cycle_back(request: Request) -> Response:
        """Take a whole cycle back — all of it, or none of it (design D7).

        Human-only in the service, for a stronger version of ``undo``'s own
        reason: it writes recorded payloads back verbatim, ``state = 'active'``
        included, across spaces, for a whole cycle at once. Sessions on this
        surface mint human principals and nothing else, so the gate is met here
        by construction and enforced there regardless.

        ``dry_run`` is the "would this succeed?" a confirm dialog needs: it
        opens no cycle, writes nothing, and returns any conflicts in
        ``conflicts`` instead of raising. A real rollback that meets one refuses
        with **409** and the same list in the error body
        (:func:`_rollback_conflict_handler`) — the graph moved on, which is a
        conflict with current state and not a bad request.
        """
        body = await _json_body(request)
        result = _write(
            request,
            service.rollback_cycle,
            request.path_params["id"],
            dry_run=_optional_bool(body, "dry_run"),
            path=db_path,
        )
        return EnvelopeResponse(envelope(result))

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
        """Set or change a human's password; the hash never leaves the service.

        **The second argon2 caller on this surface, and it runs where the first
        one does**: off the loop, under :data:`_ARGON2_LIMITER`. It ran inline
        until the review that wrote this — ~100 ms of hashing on the
        single-threaded event loop at argon2id's 64 MiB profile, with no
        limiter of any kind, because the session gate was read as answer enough.
        It is not the same question: the gate says *who* may hash, and the
        limiter says how much of the process's memory may be argon2's while they
        do. Under the default limiter that is 40 × 64 MiB — the same ~2.5 GiB
        ``POST /api/login`` is bounded away from, reachable here by an
        authenticated human instead of an anonymous one, which changes who to
        blame and nothing about the memory.

        The password's own ceiling (:data:`service.MAX_PASSWORD_LENGTH`) is the
        service's and is enforced there, so this route cannot store one the
        login route would later refuse.
        """
        body = await _json_body(request)
        human_id = request.path_params["id"]
        await anyio.to_thread.run_sync(
            functools.partial(
                _write,
                request,
                service.set_human_password,
                human_id,
                _required_str(body, "password"),
                path=db_path,
            ),
            limiter=_ARGON2_LIMITER,
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
        """Disable an agent — its token dies immediately on HTTP; a running MCP
        server stops at its next call."""
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
        level = _required_str(body, "level")
        if level not in GRANT_LEVEL_NAMES:
            raise ValueError(f"level must be one of {GRANT_LEVEL_NAMES}, got {level!r}")
        granted = _write(
            request,
            service.grant,
            _required_str(body, "agent"),
            _required_str(body, "space"),
            level,
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
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/summarize", summarize, methods=["POST"]),
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
        # `methods=["GET"]` alone does not exclude HEAD — Starlette adds HEAD
        # to any route whose methods include GET — and running the download
        # handler on a HEAD request would spend the single-use token (M6), so
        # this route is the one `_NoHeadRoute` exists for.
        _NoHeadRoute(f"{urls.TOKEN_PATHS['download']}/{{token}}", download_original),
        Route(f"{urls.TOKEN_PATHS['upload']}/{{token}}", upload_original, methods=["PUT"]),
        Route("/api/events", list_events),
        Route("/api/undo", undo, methods=["POST"]),
        Route("/api/export/node/{id}", export_node),
        # The dream journal. A cycle is history rather than knowledge (decision
        # C1), so it is its own collection and not a node listing, and its diff
        # is the event log narrowed to it rather than anything stored twice.
        Route("/api/cycles", list_cycles),
        Route("/api/cycles", run_cycle, methods=["POST"]),
        Route("/api/cycles/{id}", get_cycle),
        Route("/api/cycles/{id}/abandon", abandon_cycle, methods=["POST"]),
        Route("/api/cycles/{id}/stop", stop_cycle, methods=["POST"]),
        Route("/api/cycles/{id}/rollback", roll_cycle_back, methods=["POST"]),
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

    # The agent surface, on the same origin as the human one. It is built here
    # rather than imported as a module-level app because it needs this server's
    # database path and host list — the same list `RequestGuardMiddleware` uses,
    # so the SDK's own DNS-rebinding check and nodum's cannot disagree.
    mcp_surface = mcp_server.http_surface(db_path=db_path, allowed_hosts=hosts)

    routes = [
        Route("/healthz", healthz),
        *api_routes,
        # Order matters from here: an unmatched /api path is a JSON 404 (or a
        # 405 when only the verb was wrong), then the MCP transport claims its
        # own path, then /favicon.ico is answered as an icon rather than as a
        # document, and only then does everything else fall through to the
        # single-page app. `/mcp` has to precede that catch-all or the SPA
        # swallows it and an agent gets HTML.
        Route("/api/{path:path}", api_not_found, methods=ALL_METHODS),
        mcp_surface.route,
        Route("/favicon.ico", favicon),
        Route("/{path:path}", web_app),
    ]

    exception_handlers: dict[Any, Any] = {
        exception: _exception_handler(status) for exception, status in EXCEPTION_STATUS.items()
    }
    # The one failure whose body says more than type and message; its status is
    # still the table's, so this replaces the rendering and not the code.
    exception_handlers[RollbackConflict] = _rollback_conflict_handler
    exception_handlers[HTTPException] = _http_exception_handler
    exception_handlers[Exception] = _server_error_handler

    consolidation = _consolidation_scheduler(consolidate_at, db_path)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Own the nightly consolidation task for the life of the server.

        The whole of the schedule (design decision J1): one asyncio task in the
        server that is already running, started here and cancelled here, with
        no second process and no dependency. It is ``None`` unless the schedule
        was configured, which is the default — so an ordinary ``nodum serve``
        creates no background writer at all.

        Shutdown is bounded by :meth:`~nodum.scheduler.ConsolidationScheduler.stop`
        rather than by the cycle: a server must not take minutes to stop because
        it happened to be tidying up when the signal arrived.

        It also runs the MCP transport's session manager. That is not optional
        and not cosmetic: Starlette does **not** run a sub-application's
        lifespan, and the MCP route's own app carries its task group there — so
        without this line ``/mcp`` answers 500 on every call, while the route
        table still looks perfectly wired.
        """
        async with mcp_surface.run():
            if consolidation is not None:
                consolidation.start()
            try:
                yield
            finally:
                if consolidation is not None:
                    await consolidation.stop()

    # Outermost first: the guard normalises the path every inner layer keys on,
    # and refuses cross-origin and oversized requests before auth even looks at
    # them. An unauthenticated attacker learning "wrong origin" instead of
    # "wrong password" tells them nothing they did not already know. The
    # session gate runs second and resolves the identity every handler reads.
    middleware = [
        Middleware(RequestGuardMiddleware, allowed_hosts=hosts, max_body_bytes=body_limit),
        Middleware(SessionMiddleware, db_path=db_path),
    ]

    return Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers=exception_handlers,
        lifespan=lifespan,
    )
