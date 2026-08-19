"""The MCP server adapter (design §8) — a thin FastMCP front over the service layer.

This surface is for **external agents**, and external agents may only *grow*
the graph. It exposes the design §8.1 v1 tool contract's **read tier**
(``get_node``, ``get_children``, ``search``, ``traverse``, ``list_types``,
``get_schema``, ``find_path``, ``history``, ``diff``, ``get_asset``,
``get_download_url``) and **additive tier** (``create_node``, ``update_node``,
``link``, ``propose_edges``, ``ingest_url``, ``request_upload_url``) — nothing
else.

``get_asset`` enforces the §5.7 binary policy structurally: agents receive
metadata, the text extraction, and a small derived rendition
(``preview``/``thumb``, or ``page:<n>`` for one page of a PDF — always a WebP
image block), never the stored original. ``get_download_url`` is the design's
own documented exception (§5.7 rule 4): a short-lived, single-use URL to the
original bytes, for a host that has to open the real file, with the mint and
the redemption both in the event log.

Ingestion is **by reference** (§5.7 rule 2) — the server fetches the URL
itself, and a host holding bytes the server cannot fetch asks
``request_upload_url`` for somewhere to PUT them. No base64 ever crosses MCP
in either direction, and **no server path does either**: see
:data:`FILESYSTEM_TOOLS`.

**A tool that writes a node takes a ``space``, because the SDK will not say a
word if it does not.** ``create_node`` had no such parameter while
``ingest_url``/``request_upload_url`` did, and an agent asking
for ``research`` got a 200-shaped response describing a node in ``main``: the
generated argument model ignores unknown keys (``ArgModelBase`` is an ordinary
pydantic model, so extras are dropped, and there is no per-tool hook to forbid
them short of mutating a dependency's base class). The answer is a real
parameter rather than a rejection — an agent that cannot *name* a space is not
helped by being told its spelling was wrong — but the general shape stands:
**anything an agent must be able to say has to be in a signature here**, since a
keyword this module does not declare is silently discarded rather than refused.
Every write result carries the ``space_id`` it actually landed in, which is the
other half of that: it can be checked rather than assumed.

**Four tiers are never registered, and each one is a named absence.** The
review tools (``accept``, ``reject`` — :data:`REVIEW_TOOLS`) are the §8.1
review tier: the service gates them with ``Store.require_review`` — a human,
or ``edit`` on the item's space — and retiring the live structure an accept
replaces is the human tier, but either way they do not belong on an agent's
surface, so the CLI and the review API are where they live. The curative tools
(``merge_nodes``,
``retype``, ``supersede_edge``, ``bulk_relink``, ``consolidate`` —
:data:`CURATIVE_TOOLS`) are §8.2. Reversal plus the journal that records it
(``undo``, ``rollback``, ``abandon_cycle``, ``get_cycle``, ``list_cycles`` —
:data:`HUMAN_ONLY_TOOLS`) is the third: reversal writes recorded payloads back
verbatim and no grant delegates that, while a journal entry says what the
gardener did across every space in the file. And the fourth is
:data:`FILESYSTEM_TOOLS` — anything that lets a caller name a path on the
server's own disk. All four are enforced the same
way: they simply do not exist here, so there is no runtime check to argue
around — and :mod:`nodum.service` refuses a non-human regardless of surface.
The lists exist so ``tests/test_mcp_server.py`` can assert the registry stays
disjoint from :data:`UNREGISTERED_TOOLS`; adding an operation to any of those
tiers means adding its name to a list, never to the registry.

**Transport is HTTP, and only HTTP.** This surface is a route on the same
Starlette app ``nodum serve`` builds — ``POST /mcp`` beside ``/api`` and the
web UI — so one deployed instance serves the human and every agent from one
origin. The stdio transport was removed with the ``nodum mcp serve`` command
it existed for: a subprocess on the caller's machine cannot be deployed
anywhere, which is the whole point of the change.

Identity is therefore **per request, not per process**. The agent's bearer
token arrives in ``Authorization: Bearer ndm_…`` on every call, and
:class:`BearerGuard` refuses the request outright when it is missing or
verifies no enabled agent — before ``initialize`` or ``tools/list`` answers,
so an unauthenticated peer cannot even enumerate this surface. Inside a tool,
:func:`_principal` re-reads that same header off the SDK's own per-request
context and re-mints the principal, so disabling the agent or its owner, or
archiving a space it holds a grant on, bites at the **next call** rather than
at the next restart (revocation is verification-time, R3).

That is two verifications per call and both are wanted: the guard decides
whether the request may *reach* this surface, ``_principal`` decides *who is
speaking*. Neither is expensive — agent tokens are stored as sha-256 (see
:mod:`nodum.auth`), so each is one indexed SELECT, not an argon2 round.

**One process now serves many agents**, which is the property the old design
never had to hold: the token is read from the live request every time, never
cached in a closure, so two agents on one server cannot see each other's
grants. ``tests/test_mcp_server.py`` asserts exactly that with two agents
whose grant sets do not overlap.

Every tool delegates to :mod:`nodum.service`, :mod:`nodum.search`,
:mod:`nodum.assets`, :mod:`nodum.ingest` or :mod:`nodum.urls`; there is no
logic here beyond argument mapping and JSON shaping.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, NamedTuple, overload

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.server.lowlevel.server import request_ctx
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from nodum import assets, auth, ingest, service, urls
from nodum import search as search_module
from nodum.principal import Principal
from nodum.vocab import DIRECTIONS, STATES, NodeState

#: Tool annotations per registered tier (design §8). Reads are read-only.
#: Additive writes only ever *add* state — a node, an edge, a proposed
#: version — whatever grant the agent holds, so ``destructiveHint=False``
#: stays true under ``edit`` as well as ``suggest``.
#:
#: ``update_node`` is the exception (Q13 review S15): under an ``edit`` grant
#: it overwrites the node's fields in place and can retire the mentions its
#: old content carried. MCP hosts auto-approve on ``destructiveHint=False``,
#: so annotating it that way was a lie told to the approval prompt — it is
#: marked destructive, and the cost is that an ``edit``-granted agent's
#: updates get a confirmation an additive tool's do not. Nothing here is
#: annotated by what the *current* agent may do: annotations are static
#: registry metadata, so each one states the worst case its grant allows.
#:
#: **The ingestion tools are additive**, and that is a claim about their
#: *writes*, not about how much they write: every graph write ingestion makes
#: is a :func:`nodum.service.create_node` or :func:`~nodum.service.create_edge`
#: — it never calls the one service function that overwrites a live node — so
#: an ``edit`` grant's worst case is a subgraph that lands ``active`` instead
#: of ``proposed``. That is *more* state, never state replaced. Re-ingesting
#: bytes a space already describes reuses the existing nodes rather than
#: rewriting them (``created: false``), and the one row ingestion does
#: overwrite is ``assets.extracted_text``, which is content-addressed base
#: state and not the graph: the same bytes re-extract to the same text.
#: ``request_upload_url`` writes a capability row and hands out somewhere to
#: PUT bytes; what arrives there is registered and described the same additive
#: way.
_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_OVERWRITING = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

#: Curative tools (design §8.2) — asserted absent from the registry in tests.
CURATIVE_TOOLS = ("merge_nodes", "retype", "supersede_edge", "bulk_relink", "consolidate")

#: Review tools (design §8.1) — gated by ``Store.require_review`` (a human,
#: or ``edit`` on the item's space) and **never registered** here: asserted
#: absent in tests. Accepting makes proposed structure live, and retiring the
#: live structure an accept replaces is the human tier — both enforced by the
#: service, not by this registry. The CLI (``nodum review …``) and the review
#: API are where they live.
REVIEW_TOOLS = ("accept", "reject")

#: The third named absence: reversal, and the journal that records it. Never
#: registered, asserted absent in tests, and listed here as **one decision**
#: rather than four omissions — which is what they were. ``rollback_cycle`` is
#: the most destructive operation in the system and was in no absence list at
#: all, so the disjointness assertions beside :data:`CURATIVE_TOOLS` would have
#: watched a future tool expose it without a word.
#:
#: Two reasons, and both are the service's own, enforced there whatever this
#: registry says:
#:
#: * **Reversal is human-only** (``undo``, ``rollback``, ``abandon_cycle``).
#:   ``undo`` writes a recorded payload back verbatim, ``state = 'active'``
#:   included, and no grant delegates that; ``rollback_cycle`` does it for a
#:   whole cycle at once, across spaces, and an operation strictly more powerful
#:   than ``undo`` cannot be gated more weakly. ``abandon_cycle`` is the door
#:   that *makes* an interrupted cycle's writes reversible — declaring somebody
#:   else's run dead — so it is gated with them, and ``request_stop`` (the kill
#:   switch, ``nodum cycle-stop``) with it: an agent able to stop the gardener's
#:   night could stop the review queue from ever being filled. Its **read** half,
#:   ``service.stop_requested``, is deliberately not human-only — a runner that
#:   cannot ask whether it was told to stop cannot obey — but it is one boolean
#:   about a run this surface's callers never have, so no tool wraps it either.
#: * **The journal spans the whole file** (``get_cycle``, ``list_cycles``). An
#:   entry says what the gardener did across every space there is, so an agent
#:   reading it would learn the shape of territory it holds no grant on — the
#:   reason the event log is not on this surface either.
#:
#: Short spellings sit beside the service's own names on purpose: this is an
#: absence list, so it has to name what a tool would plausibly be *called*.
HUMAN_ONLY_TOOLS = (
    "undo",
    "rollback",
    "rollback_cycle",
    "abandon_cycle",
    "cycle_stop",
    "request_stop",
    "get_cycle",
    "list_cycles",
)

#: The fourth named absence: **no tool here lets a caller name a path on the
#: server's disk.** ``ingest_file`` was one, and its removal is the fix for a
#: capability the grant model cannot express.
#:
#: Grants scope the *graph*. The bytes an ingestion reads come from the
#: *filesystem*, which no grant describes — so an agent holding the minimal
#: write grant (``suggest`` on one space) could name any path this server's
#: user can read, and then read it back: the ingestion writes the extracted
#: text to ``assets.extracted_text``, ``assets._readable_hashes`` deliberately
#: admits a ``proposed`` describing node, and ``get_asset`` returns that text.
#: Two calls, both auto-approved by a host (``destructiveHint=False`` and
#: ``readOnlyHint=True``), and ``name`` chooses the extraction handler, so an
#: extensionless secret reads as text by asking for it.
#:
#: Removing the *echo* was not the fix and was tried first: the tool result no
#: longer carries the text (:func:`_ingest_result`), and the capability was
#: untouched, because the second call was never the reported path. **The
#: capability is the thing to close, not the delivery vector.**
#:
#: Nothing is lost. Ingestion by reference (§5.7 rule 2) has two other doors
#: and they are the ones a remote server needs anyway: ``ingest_url`` for
#: anything this server can fetch, ``request_upload_url`` for bytes that live
#: on the caller's host. A path on the server's disk only ever made sense when
#: the agent and the server shared a machine — and when they do, the operator
#: has ``nodum ingest``, where local access is already the trust boundary.
#:
#: **What this tuple is, and what it is not.** Like the other three absence
#: lists it is a set of *names*, and a name list cannot catch a tool called
#: something else — ``ingest.ingest_file``'s own parameter is ``source``, which
#: is what a re-added tool would most plausibly be called. The rule it
#: shorthands is a sentence, and the sentence is the thing to apply at review:
#: **no tool on this surface takes an argument the server resolves against its
#: own filesystem.** What actually holds mechanically is one line up in
#: ``tests/test_mcp_server.py`` —
#: ``names == set(READ_TOOLS) | set(ADDITIVE_TOOLS)`` — so *any* new tool fails
#: the suite until somebody adds it to a tier deliberately, whatever it is
#: named. This list is what makes that moment ask the right question.
FILESYSTEM_TOOLS = ("ingest_file", "ingest_path", "read_file")

#: Every name that must never appear in the registry, in one place — what the
#: disjointness assertions ask about. A new human-only, curative, review or
#: filesystem operation joins one of the four lists above; it never joins the
#: registry.
UNREGISTERED_TOOLS = CURATIVE_TOOLS + REVIEW_TOOLS + HUMAN_ONLY_TOOLS + FILESYSTEM_TOOLS

#: The tools this server registers, by tier (documentation + test anchor).
#:
#: ``get_download_url`` sits in the read tier because the design's §8.1 table
#: puts it there, and ``readOnlyHint`` is honest for it: it writes an expiring
#: capability row and an audit entry, but it creates no node, edge, or version
#: and changes nothing another reader can see. What it *does* for its caller is
#: read — it is the §5.7 rule 4 escape hatch onto bytes the caller can already
#: reach, not a way to reach more of them.
READ_TOOLS = (
    "get_node",
    "get_children",
    "search",
    "traverse",
    "list_types",
    "get_schema",
    "find_path",
    "history",
    "diff",
    "get_asset",
    "get_download_url",
    "get_principal",
)
ADDITIVE_TOOLS = (
    "create_node",
    "update_node",
    "link",
    "propose_edges",
    "ingest_url",
    "request_upload_url",
)

#: The write tools whose worst case (under an ``edit`` grant) overwrites live
#: state rather than adding to it — annotated ``destructiveHint=True``.
OVERWRITING_TOOLS = ("update_node",)

#: Most extracted characters one :func:`get_asset` result carries.
#:
#: Deliberately the same cap the ``source`` node's own body takes
#: (:data:`nodum.ingest.SOURCE_CONTENT_CHARS`), so the two ways an agent can
#: read one document's text agree on how much of it there is rather than one
#: quietly holding more. The full text is never lost — it stays on the asset,
#: where BM25 reaches every word of it — and ``extracted_chars`` always reports
#: its real length, so a truncation is visible rather than inferred.
MAX_EXTRACTED_TEXT_CHARS = ingest.SOURCE_CONTENT_CHARS


@overload
def _dump(result: BaseModel) -> dict[str, Any]: ...


@overload
def _dump(result: Sequence[BaseModel]) -> list[dict[str, Any]]: ...


def _dump(result: BaseModel | Sequence[BaseModel]) -> dict[str, Any] | list[dict[str, Any]]:
    """Serialise service results exactly like every other adapter."""
    if isinstance(result, Sequence):
        return [item.model_dump(mode="json") for item in result]
    return result.model_dump(mode="json")


def _ingest_result(out: ingest.IngestOut) -> dict[str, Any]:
    """Serialise an ingestion result with the extracted text left behind.

    The write is the tool's job and its result is the describing subgraph —
    ids, spaces, states, extraction statistics — that tells the agent where
    its work landed. The text itself is omitted rather than blanked, so a
    missing key can never be mistaken for a document that extracted nothing;
    once a describing node is readable, ``get_asset`` returns it, scoped by
    the same grant set that confined the write.

    **This is a payload-size decision, not a security boundary, and it must
    not be mistaken for one again.** It was written as though withholding the
    text were what stopped an agent reading something it should not — and it
    was not, because ``get_asset`` hands the same text over on the very next
    call by design, from a ``proposed`` describing node, with no human in
    between.

    What this surface actually bounds is narrower than "what the caller could
    already read", and stating it precisely is the point of this paragraph:
    **no tool here can name a file** (:data:`FILESYSTEM_TOOLS`), so the
    server's *filesystem* is not reachable. Its **network position still is**.
    ``ingest_url`` fetches on the server's behalf, and :mod:`nodum.ingest`
    blocks neither loopback nor private ranges — deliberately, and argued
    there: the server is itself a loopback service. So an agent holding
    ``suggest`` can have this server fetch something only *it* can reach and
    then read the result back with ``get_asset``. That is a property of
    granting an agent ingestion at all, it predates the removal of the
    filesystem tool, and it is **not** closed by anything in this function —
    which is exactly why this docstring no longer claims otherwise.
    """
    return out.model_dump(
        mode="json",
        exclude={
            "asset": {"extracted_text"},
            "source": {"content"},
            "pages": {"__all__": {"content"}},
        },
    )


#: The HTTP path this surface answers on, relative to the server's origin.
#: One constant so the route, the guard's exemption in :mod:`nodum.http_api`,
#: and the documented URL cannot drift apart.
MCP_PATH = "/mcp"

#: Authorization scheme this surface accepts, lowercased for comparison.
BEARER_SCHEME = "bearer"


def _bearer_token(header: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer …`` header.

    Returns ``None`` for a missing header, a different scheme, or an empty
    token — every "no usable credential" case collapses to one, because the
    caller's reply is the same 401 for all of them and distinguishing them in
    the response would only tell an unauthenticated peer which half it got
    right.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.strip().lower() != BEARER_SCHEME:
        return None
    return value.strip() or None


def _presented_token() -> str | None:
    """Read the bearer token off the request the SDK is currently serving.

    The SDK sets :data:`mcp.server.lowlevel.server.request_ctx` per request and
    hands the underlying Starlette ``Request`` along on it. Reading the header
    from there — rather than from a contextvar this module sets in an ASGI
    wrapper — is deliberate: the session manager may run a tool call in a task
    it spawned, and a contextvar set on the request's task would not
    necessarily be visible there, whereas the SDK's own context is by
    construction the context of *this* call.

    Returns ``None`` outside any request, which is what makes the check in
    :func:`_principal` meaningful rather than decorative.
    """
    try:
        context = request_ctx.get()
    except LookupError:
        return None
    request = context.request
    if request is None:
        return None
    return _bearer_token(request.headers.get("authorization"))


class BearerGuard:
    """Refuse an MCP request that presents no enabled agent's token.

    This runs in front of the whole surface, so ``initialize`` and
    ``tools/list`` need a credential exactly as a tool call does: an
    unauthenticated peer cannot enumerate what this server exposes, let alone
    call it. Without it the transport would answer ``200`` with a JSON-RPC
    error body, which reads as "your request was fine, the tool failed" — the
    wrong thing to tell someone who has not authenticated at all.

    The refusal is a real ``401`` carrying ``WWW-Authenticate: Bearer``, in the
    same error envelope every other nodum surface uses, and it says nothing
    about *why* — a missing header, a malformed one, a revoked token and a
    token that never existed are one answer, because any finer grain is an
    oracle (:mod:`nodum.auth` phrases its refusals the same way).

    It does **not** decide who the caller is. :func:`_principal` does that, per
    tool call, from the same header — this is the door, not the identity.
    """

    def __init__(self, app: ASGIApp, *, db_path: str | Path | None = None) -> None:
        self.app = app
        self.db_path = db_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        token = _bearer_token(headers.get("authorization"))
        if token is None or not self._verifies(token):
            await self._refuse(send)
            return
        await self.app(scope, receive, send)

    def _verifies(self, token: str) -> bool:
        """Whether this token verifies an enabled agent right now."""
        try:
            auth.verify_agent_token(token, path=self.db_path)
        except auth.InvalidCredentials:
            return False
        return True

    async def _refuse(self, send: Send) -> None:
        """Send the 401 without touching the request body."""
        body = json.dumps(
            {
                "error": {
                    "type": "InvalidCredentials",
                    "message": (
                        "this surface authenticates with 'Authorization: Bearer ndm_…' — "
                        "mint a token with 'nodum agent create <name>'"
                    ),
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _host_patterns(hosts: Sequence[str]) -> list[str]:
    """Render nodum's host names in the shape the SDK's matcher compares against.

    The two layers spell the same policy differently, and the difference is a
    silent 421 rather than an error: :func:`nodum.http_api.bare_host` strips
    the port before comparing, so nodum's list holds ``127.0.0.1``, while the
    SDK compares the raw ``Host`` header and understands only an exact match or
    a ``name:*`` port wildcard. Feeding it the bare name alone means
    ``Host: 127.0.0.1:8600`` matches nothing and every request is refused.

    Emitting both forms is what makes "one list, two enforcement points" true
    rather than aspirational — the policy still comes from one place, and this
    is only its translation.
    """
    return [pattern for host in hosts for pattern in (host, f"{host}:*")]


class McpSurface(NamedTuple):
    """The two things :func:`nodum.http_api.create_app` has to attach.

    ``route`` goes in the router beside ``/api``; ``run`` goes in the app's
    lifespan. Both are required, and forgetting the second is the trap:
    Starlette does **not** run a sub-application's lifespan, so a route wired
    without it answers **500** on every call — a failure that looks like a bug
    in the transport rather than a missing line in ``create_app``.
    """

    route: Route
    run: Callable[[], AbstractAsyncContextManager[None]]


def http_surface(
    *,
    db_path: str | Path | None = None,
    allowed_hosts: frozenset[str] | None = None,
) -> McpSurface:
    """Build the MCP surface as a route on the human server's own app.

    Args:
        db_path: Explicit database path; defaults to ``NODUM_DB`` resolution.
        allowed_hosts: Host names the server answers to, from
            :func:`nodum.http_api.resolve_allowed_hosts`. Passed through to the
            SDK's own DNS-rebinding protection so that both layers read the
            **same** list — see below.

    Returns:
        The route to register and the lifespan context manager to run.

    **Why the host list is threaded through rather than left to the SDK.**
    FastMCP defaults ``transport_security`` to loopback-only
    (``127.0.0.1:*``/``localhost:*``/``[::1]:*``, verified against
    ``mcp 1.28.1``). Mounted as-is on a server reached at
    ``nodum.vcoeur.com`` that default refuses **every** request, and the
    failure looks like a broken deployment rather than a policy. Deriving the
    SDK's list from nodum's own means the two enforcement points cannot
    disagree: one source, checked twice. ``--allow-host '*'`` disables both
    together, for the same reason.
    """
    server = create_server(db_path=db_path)
    hosts = sorted(allowed_hosts or ())
    disabled = "*" in hosts
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=not disabled,
        allowed_hosts=_host_patterns(hosts),
        # A browser is not a client of this surface, and a bearer token is not
        # a credential a browser attaches on its own. Allowing the same names
        # here keeps a same-origin page working without widening anything: the
        # token still has to be presented explicitly.
        allowed_origins=[
            f"{scheme}://{pattern}"
            for scheme in ("http", "https")
            for pattern in _host_patterns(hosts)
        ],
    )
    server.settings.streamable_http_path = MCP_PATH
    # `stateless_http` because a deployed instance is restarted, redeployed and
    # reached by short-lived agent processes: a session id that has to survive
    # between calls is state this surface would have to keep and a client would
    # have to replay. Each request carries its own credential already.
    server.settings.stateless_http = True

    app = server.streamable_http_app()
    # `streamable_http_app()` returns a Starlette app whose single Route is the
    # transport. Lifting that route out (rather than mounting the app) is what
    # puts the endpoint at exactly `/mcp`: a `Mount` at `/mcp` would answer on
    # `/mcp/` and redirect `/mcp` to it, and a 307 on the first POST is a body
    # replay some client stacks will not do. The unpacking is deliberately
    # strict so a shape change in the SDK fails here, loudly, rather than
    # silently attaching nothing.
    (route,) = [candidate for candidate in app.routes if isinstance(candidate, Route)]
    if route.path != MCP_PATH:  # pragma: no cover — defensive against an SDK change
        raise RuntimeError(f"MCP transport route is {route.path!r}, expected {MCP_PATH!r}")
    route.app = BearerGuard(route.app, db_path=db_path)
    return McpSurface(route=route, run=server.session_manager.run)


def create_server(*, db_path: str | Path | None = None) -> FastMCP:
    """Build the nodum MCP server. Agent identity comes from the request.

    This server is **not bound to an agent**. One instance serves every agent
    that presents a valid token, because it is one route on a deployed HTTP
    app rather than a subprocess one client launched. The token is read off
    the live request on every tool call by :func:`_principal`.

    Args:
        db_path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        A FastMCP server with the read and additive tools registered. Review
        tools (§8.1 human tier), curative tools (§8.2) and reversal are never
        registered, and no tool names a path on the server's disk
        (:data:`FILESYSTEM_TOOLS`).
    """

    def _principal() -> Principal:
        """Mint the calling agent's principal from *this request's* token.

        Two properties ride on reading the header here rather than closing
        over a token at construction:

        **Isolation.** One process serves many agents, so a cached identity
        would be another agent's identity. The token is re-read from the live
        request every call; nothing about the caller survives between calls.

        **Revocation is verification-time** (:mod:`nodum.auth` R3): disabling
        the agent, disabling its owner, or archiving a space it holds a grant
        on must bite at the **next tool call**, not at the next restart.
        ``verify_agent_token`` re-reads ``agents.disabled`` and reloads the
        grant set (which drops grants on archived spaces) every time, so one
        indexed SELECT per call is the honest price of that.

        :class:`BearerGuard` has already refused this request if the header is
        absent or bad, so the raises below are the second layer rather than
        the gate — the same reasoning ``service._walk`` applies to endpoint
        rows it could have trusted the SQL for. An identity check that exists
        at one of two sites is a comment, not a check.

        Raises:
            auth.InvalidCredentials: If the request carries no usable bearer
                token, or one that verifies no enabled agent.
        """
        token = _presented_token()
        if token is None:
            raise auth.InvalidCredentials(
                "no agent token on this request: the MCP surface authenticates with "
                "'Authorization: Bearer ndm_…' (mint one with 'nodum agent create <name>')"
            )
        return auth.verify_agent_token(token, path=db_path)

    server = FastMCP(
        "nodum",
        instructions=(
            "nodum knowledge graph — read tier (get_node/get_children/search/traverse/"
            "list_types/get_schema/find_path/history/diff/get_asset/get_download_url/"
            "get_principal) and "
            "additive tier (create_node/update_node/link/propose_edges/"
            "ingest_url/request_upload_url). Writes land as proposals for human review "
            "under a 'suggest' grant and live under 'edit' — call get_principal to see "
            "which applies to you, per space, before you write. Reviewing (accept/reject) "
            "and curative operations are not available over MCP — they belong to the "
            "human. Ingest by reference: give a URL this server can fetch, or ask "
            "request_upload_url for somewhere to PUT bytes you hold — never bytes "
            "inline, and never a path on the server's disk. Assets come back as "
            "metadata, extracted text and small derived "
            "renditions; the original binary crosses this surface only through "
            "get_download_url, and that is logged (design §5.7)."
        ),
    )

    # ── Read tier ─────────────────────────────────────────────────────────

    @server.tool(annotations=_READ)
    def get_node(id: str, depth: int = 1) -> dict[str, Any]:
        """Fetch a node plus its active-edge neighborhood out to `depth` hops (0 = node alone)."""
        return _dump(
            service.get_neighborhood(id, depth=depth, principal=_principal(), path=db_path)
        )

    @server.tool(annotations=_READ)
    def get_children(id: str) -> list[dict[str, Any]]:
        """List a node's children in position order (the document tree)."""
        return _dump(service.list_children(id, principal=_principal(), path=db_path))

    @server.tool(annotations=_READ)
    def search(
        query: str,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        expand: bool = False,
    ) -> dict[str, Any]:
        """Hybrid search over node title + content: BM25 + vector, RRF-fused.

        The `vector` signal participates when an embedding provider is
        available on the server; otherwise results are BM25-only (no error).
        `signals` on each hit names the contributing signals.

        `filters` keys: `type`, `state` (default "active"; "any" for all),
        `created_by`, `created_after`, `created_before`. `expand` appends
        one-hop neighbors of the hits along active edges (graph signal).
        """
        filters = dict(filters or {})
        known = {"type", "state", "created_by", "created_after", "created_before"}
        unknown = sorted(set(filters) - known)
        if unknown:
            raise ValueError(f"unknown search filter(s): {', '.join(unknown)}")
        state = filters.pop("state", "active")
        if state in (None, "any"):
            narrowed_state: NodeState | None = None
        else:
            state = str(state)
            if state not in STATES:
                raise ValueError(f"state must be one of {STATES}, got {state!r}")
            narrowed_state = state
        result = search_module.search(
            query,
            k=k,
            state=narrowed_state,
            type=filters.pop("type", None),
            created_by=filters.pop("created_by", None),
            created_after=filters.pop("created_after", None),
            created_before=filters.pop("created_before", None),
            expand=expand,
            principal=_principal(),
            path=db_path,
        )
        return _dump(result)

    @server.tool(annotations=_READ)
    def traverse(
        start_id: str,
        edge_types: list[str] | None = None,
        depth: int = 2,
        direction: str = "both",
    ) -> dict[str, Any]:
        """Walk the subgraph reachable from `start_id` over active edges.

        `edge_types` restricts the walk (ids or names), `depth` caps hops,
        `direction` is "out" / "in" / "both".
        """
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
        return _dump(
            service.traverse(
                start_id,
                edge_types=edge_types,
                depth=depth,
                direction=direction,
                principal=_principal(),
                path=db_path,
            )
        )

    @server.tool(annotations=_READ)
    def list_types() -> dict[str, Any]:
        """List the full type catalog (node types and edge types)."""
        return _dump(service.list_types(principal=_principal(), path=db_path))

    @server.tool(annotations=_READ)
    def get_principal() -> dict[str, Any]:
        """Report your own identity and grant set — what your writes would do, before you write one.

        Each granted space carries `writes_land`: the state a create there
        would take, `active` under an `edit` grant and `proposed` under
        `suggest`. Read it instead of discovering the answer by writing
        something — under `suggest` that first write cannot be retracted from
        this surface, since accept/reject/archive belong to the human.

        Only spaces you hold a grant on are listed: this reports your own
        access and reveals nothing you could not already reach.
        """
        principal = _principal()
        return {
            "actor": principal.actor_string,
            "kind": principal.kind,
            "spaces": [
                {
                    "space": space_id,
                    "level": level,
                    "writes_land": principal.landing_on(space_id),
                }
                for space_id, level in sorted(principal.grants.items())
            ],
        }

    @server.tool(annotations=_READ)
    def get_schema(type: str) -> dict[str, Any]:
        """Fetch one node or edge type's catalog entry (id or name), incl. its JSON schema."""
        return _dump(service.get_schema(type, principal=_principal(), path=db_path))

    @server.tool(annotations=_READ)
    def find_path(a: str, b: str) -> dict[str, Any]:
        """Find the shortest path between two nodes over active edges (any type)."""
        return _dump(service.find_path(a, b, principal=_principal(), path=db_path))

    @server.tool(annotations=_READ)
    def history(node_id: str) -> list[dict[str, Any]]:
        """List a node's version history (applied snapshots and proposed/rejected updates)."""
        return _dump(service.history(node_id, principal=_principal(), path=db_path))

    @server.tool(annotations=_READ)
    def diff(a: int, b: int) -> dict[str, Any]:
        """Unified diff between two versions of one node (ids from `history`)."""
        return _dump(service.diff_versions(a, b, principal=_principal(), path=db_path))

    @server.tool(annotations=_READ, structured_output=False)
    def get_asset(id_or_hash: str, rendition: str = "preview") -> list[Any]:
        """Fetch an asset's metadata and extracted text, plus a small derived rendition.

        **Never the original bytes** — design §5.7 binary policy: LLMs receive
        derived representations only. The first block is always metadata JSON,
        carrying whatever text extraction pulled out of this asset:
        `extracted_text` (capped), `extracted_chars` (its real length) and
        `text_truncated`, so you can tell a short document from a clipped one.
        `extracted_text` is null when no handler could read the bytes — an
        asset with no text is still registered and described.

        `rendition` chooses the image that comes back with it: `preview`
        (≤1024px, the default and the size vision models want), `thumb`
        (≤256px), or `page:<n>` — a 1-based page of a PDF rasterised as an
        image, which is how you *look at* a document whose layout, tables or
        figures carry the meaning. Ask for the pages you need one at a time.

        An asset with no renderable form for the profile asked — a text file,
        or a PDF under `preview` — comes back as the metadata block alone with
        `rendition: null`. A page of something that is not a PDF, a page past
        the end of one, and any unknown profile (`full`, `original`) are all
        errors: originals never cross this surface. `get_download_url` is the
        one exception, and it is logged.
        """
        try:
            _spec, page_number = assets.resolve_profile(rendition)
        except assets.UnsupportedRendition as exc:
            raise ValueError(
                f"unsupported rendition {rendition!r}: MCP serves "
                f"{', '.join(sorted(assets.PROFILES))} and page:<n> only — originals never"
            ) from exc
        # One verification for the whole call: `_principal()` re-verifies the
        # token against the database, and this tool makes two scoped reads. Two
        # hops bought nothing — a revocation landing between them would only
        # move which of the two reads refused — and paid for it twice on the
        # one read tool that also decodes an image.
        principal = _principal()
        asset = assets.get_asset(id_or_hash, principal=principal, path=db_path)
        text = asset.extracted_text
        metadata: dict[str, Any] = {
            "asset": asset.model_dump(mode="json", exclude={"extracted_text"}),
            "extracted_text": None if text is None else text[:MAX_EXTRACTED_TEXT_CHARS],
            "extracted_chars": len(text or ""),
            "text_truncated": len(text or "") > MAX_EXTRACTED_TEXT_CHARS,
        }
        try:
            rend = assets.get_rendition(
                id_or_hash,
                profile=rendition,
                include_data=True,
                principal=principal,
                path=db_path,
            )
        except assets.UnsupportedRendition:
            # A *named page* that cannot be rendered is a failed request and says
            # so — the asset is not a PDF, or the page is past its end. The
            # metadata-only fallback belongs to the profiles a caller gets by
            # default (`preview` on a text file or a PDF): there the text block
            # is the answer, and an error would be an unhelpful way to say "this
            # is not an image".
            if page_number is not None:
                raise
            metadata["rendition"] = None
            return [metadata]
        metadata["rendition"] = rend.model_dump(mode="json", exclude={"data_base64"})
        return [metadata, Image(data=assets.read_rendition_bytes(rend), format="webp")]

    @server.tool(annotations=_READ)
    def get_download_url(
        id_or_hash: str, ttl_seconds: int = urls.DEFAULT_TTL_SECONDS
    ) -> dict[str, Any]:
        """Mint a short-lived, single-use URL to an asset's **original bytes**.

        This is the one documented exception to "LLMs never receive original
        binaries" (design §5.7 rule 4): every other path hands you metadata,
        extracted text, or a small derived rendition, and this one hands over
        the real file — for a host that has to open it in a real application,
        or hand it to a tool that reads the format itself. Both the mint and
        the later redemption are written to the event log under your identity,
        so reaching for the hatch is on the record. Prefer `get_asset` when a
        page raster or the extracted text would do.

        The URL is **single-use** — the first fetch spends it and a second one
        is refused — and **short-lived**: minutes by default, `ttl_seconds`
        raises or lowers that within an hour ceiling. It carries no credential
        of its own, so whoever holds it can fetch those bytes once before it
        expires: treat it as the secret it is, use it immediately, and do not
        park it anywhere that keeps a copy.

        The address is built from the server's `NODUM_PUBLIC_URL` (default
        `http://127.0.0.1:8600`). If you reach this server on any other
        address, that variable has to be set **on the server** or the URL you
        get back will be unreachable from where you are — the token is fine,
        the host in front of it is not.

        An asset you cannot reach answers *not found* and mints nothing: a
        download URL never widens your reach, it only spends it.
        """
        return _dump(
            urls.mint_download(
                id_or_hash,
                ttl_seconds=ttl_seconds,
                principal=_principal(),
                path=db_path,
            )
        )

    # ── Additive tier ─────────────────────────────────────────────────────

    @server.tool(annotations=_ADDITIVE)
    def create_node(
        type: str,
        title: str | None = None,
        content: str = "",
        parent: str | None = None,
        props: dict[str, Any] | None = None,
        space: str | None = None,
    ) -> dict[str, Any]:
        """Create a node. Where it lands depends on your grant on the space.

        With a `suggest` grant the node is `proposed` and waits for review;
        with `edit` it lands `active` immediately — the grant is the whole
        difference, and `get_principal` reports which you hold, per space,
        without writing anything.

        `space` is where the node goes — a space id or name, defaulting to
        `main` — and you must hold a grant on it: one you cannot write refuses,
        and one you cannot read is refused in the same words as a space that
        does not exist. The returned node carries the `space_id` it actually
        landed in, so check it rather than assuming.

        Any `[[wikilinks]]` in `content` materialise as `mentions` edges in
        the same way, and a link into a space you may only suggest in stays
        `proposed` even when the node itself is live.
        """
        return _dump(
            service.create_node(
                type=type,
                title=title,
                content=content,
                parent_id=parent,
                props=props,
                space=space,
                principal=_principal(),
                path=db_path,
            )
        )

    @server.tool(annotations=_OVERWRITING)
    def update_node(
        id: str,
        title: str | None = None,
        content: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a node — staged as a proposal, or applied in place under `edit`.

        With a `suggest` grant this stages a new `proposed` version (design
        §8.1): only the given fields are recorded, and accepting applies just
        those to the node as it stands then, so anything edited while your
        proposal waited is preserved. The node itself is untouched until a
        reviewer accepts — there is no tool on this surface that can accept.

        **With an `edit` grant the node is overwritten immediately**, its old
        content replaced and the mentions that content carried retired. That
        is why this tool is annotated destructive while the others are not.
        """
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if content is not None:
            kwargs["content"] = content
        if props is not None:
            kwargs["props"] = props
        return _dump(service.update_node(id, principal=_principal(), path=db_path, **kwargs))

    @server.tool(annotations=_ADDITIVE)
    def link(
        src: str,
        dst: str,
        edge_type: str,
        props: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """Create a typed, directed edge; `proposed` under `suggest`, live under `edit`.

        The landing state needs the matching grant on **both** endpoint
        spaces: `edit` on one and `suggest` on the other stages the edge.

        `confidence` is your own estimate and is recorded as such — it is
        indicative data for the reviewer and triggers nothing on its own.
        """
        return _dump(
            service.create_edge(
                src,
                dst,
                edge_type,
                props=props,
                confidence=confidence,
                principal=_principal(),
                path=db_path,
            )
        )

    @server.tool(annotations=_ADDITIVE)
    def propose_edges(suggestions: list[dict[str, Any]]) -> dict[str, Any]:
        """Write a batch of edges: each suggestion is {src, dst, edge_type, props?, confidence?}.

        Each edge lands exactly as `link` would — `proposed` under `suggest`,
        live under `edit` on both endpoint spaces. Bad suggestions are
        reported in `failed` by index; the rest still write.
        """
        return _dump(service.propose_edges(suggestions, principal=_principal(), path=db_path))

    @server.tool(annotations=_ADDITIVE)
    def ingest_url(
        url: str,
        name: str | None = None,
        space: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Fetch an `http`/`https` URL into the graph — bytes in, a described subgraph out.

        The **server** does the fetch — one bounded read with a timeout, and
        redirects that may not leave http/https — and records the URL on both
        written nodes as provenance. That is design §5.7's "ingestion by
        reference", and it is why there is no way to send file contents
        through this tool and never will be. **There is no way to name a path
        on the server's disk either**: for bytes that live on your own host,
        ask `request_upload_url` for somewhere to PUT them.

        One ingestion registers the bytes (content-addressed, so the same
        document twice moves nothing), extracts what text it can, and writes
        an `asset_ref` node describing the bytes in `space`, a `source` node
        carrying the extracted text, a `derived_from` edge from the source to
        the asset, and one `block` child per page for paginated formats.

        With a `suggest` grant that whole subgraph lands `proposed` and waits
        for review; with `edit` it lands `active` immediately, exactly as a
        hand-written node would — ingestion adds no authority of its own.
        Re-ingesting bytes this space already describes returns the existing
        subgraph with `created: false`: nothing is duplicated, nothing is
        overwritten, and an interrupted run is repaired by running it again.

        `extraction.handler` names what read the document and
        `extraction.detail` says why nothing came out when nothing did —
        usually a handler whose optional dependency is not installed on the
        server. An asset no handler could read is still registered and still
        described.

        **The text itself is never in this result** — `extraction.chars` (and
        `extracted_chars` on the `asset_ref`'s props) say how much there is,
        and `get_asset` returns the text once a describing node is readable.

        A page served from an extensionless path still extracts: the
        response's own content type picks the handler. `name` overrides the
        recorded filename, **and with it the extension that picks the
        handler**; `title` names the `source` node. Only the URL's bytes are
        ingested, so a link that returns a login page ingests the login page —
        check `extraction.chars` before treating the outcome as the document
        you meant.
        """
        return _ingest_result(
            ingest.ingest_url(
                url,
                name=name,
                space=space,
                title=title,
                principal=_principal(),
                path=db_path,
            )
        )

    @server.tool(annotations=_ADDITIVE)
    def request_upload_url(
        name: str,
        mime: str,
        size: int,
        sha256: str | None = None,
        space: str | None = None,
    ) -> dict[str, Any]:
        """Get a single-use URL to PUT one file to, for bytes the server cannot reach.

        Reach for this when `ingest_url` cannot do the job: the bytes sit on
        your host and no URL the server can fetch points at them. There is no
        third option — no tool on this surface reads a path on the server's
        own disk. Declare `sha256` whenever you know it —
        if the store already holds those bytes you get the existing `asset`
        back with **no grant and no transfer at all** (design §5.7 rule 4);
        otherwise you get a `grant` whose `url` accepts exactly one PUT of at
        most `size` bytes, and only for minutes.

        `space` is where the node describing those bytes will land, and you
        must be able to write it: a grant onto a space you cannot write is
        refused **now** rather than after the upload. Under `suggest` that
        describing node lands `proposed` like any other write; under `edit` it
        lands live. Either way the asset is reachable straight away — a
        describing node makes it readable in any state but `archived`, so a
        proposal you are still waiting on is enough. What `get_asset` then
        gives you is what it gives anyone: metadata, the extracted text and a
        rendition, **never the original bytes** — those come back only through
        `get_download_url`, which is logged.

        The mint is event-logged, the dedup shortcut included.
        """
        return _dump(
            urls.mint_upload(
                name,
                mime,
                size,
                sha256=sha256,
                space=space,
                principal=_principal(),
                path=db_path,
            )
        )

    # ── Review tier (§8.1) is deliberately absent ──
    # `accept`/`reject` are not registered here: they are the review tier,
    # gated by `Store.require_review` (a human, or `edit` on the item's
    # space) — and this is an agent surface, so they live on `nodum review …`
    # and the review API instead. Retiring the live structure an accept
    # replaces is the human tier, enforced by the service either way.

    # ── `ingest_file` is deliberately absent ──
    # No tool here takes a path on the server's disk (`FILESYSTEM_TOOLS`).
    # Grants scope the graph; a filesystem read is not a graph read, so the
    # grant model could not bound it — and `ingest_url` plus
    # `request_upload_url` are the two doors a server the caller does not share
    # a machine with needs anyway.

    return server


# ── There is no `serve` here ──────────────────────────────────────────────────
# The stdio transport was removed with `nodum mcp serve`. This surface is a
# route on the app `nodum serve` builds (`http_surface` above), because a
# subprocess launched by the caller can only ever reach a database on the
# caller's own machine — and reaching one that is not is the point.
