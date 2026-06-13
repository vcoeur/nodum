"""FastAPI HTTP adapter over the nodum data-service layer.

A thin transport with no domain logic of its own: each route parses its request
body or query params, calls the matching :mod:`nodum.service` function, and
returns the model serialised exactly as the CLI emits it — ``model_dump(mode=
"json")`` wrapped in a ``JSONResponse``, with no ``response_model`` so keys are
neither added, dropped, nor reordered. Identical data therefore yields
byte-identical JSON across the CLI and the HTTP API. Domain errors raised by the
service are mapped to clean JSON: a missing node/edge is a 404, bad input a 422.

Authentication (see :mod:`nodum.auth`): every data route is gated by
:func:`require_auth`, which accepts the session **cookie first, then a Bearer
token** and returns 503 until a main password is set, 401 otherwise. ``/healthz``
and the ``/auth/*`` routes stay open; the browser-facing ``/`` and ``/login`` are
handled by :mod:`nodum.web`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import nodum
from nodum import auth, service, web
from nodum.models import (
    AddEdgeIn,
    AddNodeIn,
    EdgeKindIn,
    EdgeKindPatch,
    LoginIn,
    NodeKindIn,
    NodeKindPatch,
    UpdateEdgeIn,
    UpdateNodeIn,
)
from nodum.service import EdgeNotFound, KindInUse, KindNotFound, NodeNotFound
from nodum.settings import load_settings

app = FastAPI(title="nodum", version=nodum.__version__)
_settings = load_settings()


# ── Security headers (defence in depth) ───────────────────────────────────────


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Attach CSP / nosniff / frame-deny headers to every response.

    All scripts and styles are same-origin static assets, so ``default-src
    'self'`` holds without inline exceptions. Complements the HttpOnly session
    cookie against XSS and clickjacking.
    """
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# ── Error mapping ─────────────────────────────────────────────────────────────


@app.exception_handler(NodeNotFound)
@app.exception_handler(EdgeNotFound)
@app.exception_handler(KindNotFound)
async def _handle_not_found(request: Request, exc: Exception) -> JSONResponse:
    """Map a missing node/edge/kind error to a 404 with a clean JSON detail body."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(KindInUse)
async def _handle_kind_in_use(request: Request, exc: KindInUse) -> JSONResponse:
    """Map a still-referenced kind (delete refused without ``into``) to a 409."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def _handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
    """Map a service input-validation error (incl. ValidationError) to a 422."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# ── Authentication ────────────────────────────────────────────────────────────


def _token_from(authorization: str | None, cookie: str | None) -> str | None:
    """Extract the session token — the cookie first, then a Bearer header."""
    if cookie:
        return cookie
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return None


def require_auth(
    authorization: str | None = Header(default=None),  # noqa: B008 — FastAPI param sentinel
    nodum_session: str | None = Cookie(default=None),  # noqa: B008 — FastAPI param sentinel
) -> None:
    """Gate a route on a valid session cookie or Bearer token.

    Raises:
        HTTPException: 503 until a main password is configured; 401 when the
            credential is missing or its signature/expiry fails. argon2 is never
            run here — only the token's cheap HMAC signature is verified.
    """
    if not auth.is_configured():
        raise HTTPException(
            status_code=503, detail="auth not configured — run `nodum auth set-password`"
        )
    token = _token_from(authorization, nodum_session)
    if token is None or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="authentication required")


@app.post("/auth/login")
def auth_login(body: LoginIn) -> JSONResponse:
    """Verify the main password; return a session token and set the session cookie.

    The body carries ``{token, expires_in}`` for API/CLI clients; the same token
    is also set as an HttpOnly, SameSite=Strict cookie for the browser. Wrong
    password → 401; no password configured → 503.
    """
    try:
        token = auth.login(body.password)
    except auth.AuthNotConfigured as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except auth.BadPassword:
        return JSONResponse(status_code=401, content={"detail": "invalid password"})
    response = JSONResponse(content={"token": token, "expires_in": auth.TOKEN_MAX_AGE_SECONDS})
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=auth.TOKEN_MAX_AGE_SECONDS,
        httponly=True,
        secure=_settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/auth/logout")
def auth_logout() -> JSONResponse:
    """Clear the session cookie. Idempotent; always returns ``{"ok": true}``."""
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/auth/session")
def auth_session(
    authorization: str | None = Header(default=None),  # noqa: B008 — FastAPI param sentinel
    nodum_session: str | None = Cookie(default=None),  # noqa: B008 — FastAPI param sentinel
) -> JSONResponse:
    """Report ``{configured, authenticated}`` for the SPA (open route).

    The session cookie is HttpOnly, so the browser cannot read it; the SPA calls
    this to decide between the setup hint, the sign-in view, and the app.
    """
    configured = auth.is_configured()
    authenticated = False
    if configured:
        token = _token_from(authorization, nodum_session)
        authenticated = token is not None and auth.verify_token(token)
    return JSONResponse(content={"configured": configured, "authenticated": authenticated})


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe returning a static ``{"status": "ok"}`` payload (open)."""
    return JSONResponse(content={"status": "ok"})


# ── Data routes (all gated by require_auth) ───────────────────────────────────

router = APIRouter(dependencies=[Depends(require_auth)])


# ── Nodes ─────────────────────────────────────────────────────────────────────


@router.post("/nodes")
def create_node(body: AddNodeIn) -> JSONResponse:
    """Create a typed node and return it, serialised exactly as the CLI emits it."""
    node = service.add_node(body.kind, body.content, body.data)
    return JSONResponse(content=node.model_dump(mode="json"))


@router.get("/nodes/{uuid}")
def get_node(uuid: UUID) -> JSONResponse:
    """Fetch a node with every edge incident on it (either direction)."""
    result = service.get(str(uuid))
    return JSONResponse(content=result.model_dump(mode="json"))


@router.patch("/nodes/{uuid}")
def patch_node(uuid: UUID, body: UpdateNodeIn) -> JSONResponse:
    """Merge new content/payload into a node, re-validate, and return it."""
    node = service.update_node(str(uuid), content=body.content, data=body.data)
    return JSONResponse(content=node.model_dump(mode="json"))


@router.delete("/nodes/{uuid}")
def delete_node(uuid: UUID) -> JSONResponse:
    """Delete a node; its incident edges cascade. Returns the cascade count."""
    result = service.delete_node(str(uuid))
    return JSONResponse(content=result.model_dump(mode="json"))


# ── Edges ─────────────────────────────────────────────────────────────────────


@router.post("/edges")
def create_edge(body: AddEdgeIn) -> JSONResponse:
    """Create a typed, directed edge between two existing nodes and return it."""
    edge = service.add_edge(body.kind, str(body.from_uuid), str(body.to_uuid), body.data)
    return JSONResponse(content=edge.model_dump(mode="json"))


@router.patch("/edges/{uuid}")
def patch_edge(uuid: UUID, body: UpdateEdgeIn) -> JSONResponse:
    """Merge new payload into an edge (kind and endpoints fixed) and return it."""
    edge = service.update_edge(str(uuid), data=body.data)
    return JSONResponse(content=edge.model_dump(mode="json"))


@router.delete("/edges/{uuid}")
def delete_edge(uuid: UUID) -> JSONResponse:
    """Delete a single edge and return the delete count."""
    result = service.delete_edge(str(uuid))
    return JSONResponse(content=result.model_dump(mode="json"))


# ── Query ─────────────────────────────────────────────────────────────────────


@router.get("/search")
def search(q: str, kind: str | None = None, limit: int = 20) -> JSONResponse:
    """Full-text search over node text, ranked best-first, optionally by kind."""
    result = service.search(q, kind=kind, limit=limit)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/expand")
def expand(
    seed: UUID,
    depth: int = 1,
    edge_kind: list[str] | None = Query(None),  # noqa: B008 — FastAPI query-param sentinel
) -> JSONResponse:
    """Expand a seed node into its connected subgraph, following edges outward.

    Args:
        seed: The seed node UUID.
        depth: Maximum number of hops to follow (>= 1).
        edge_kind: Repeatable query param; restricts traversal to these edge kinds.
    """
    result = service.expand(str(seed), depth, edge_kinds=edge_kind)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/schema")
def schema() -> JSONResponse:
    """Return the live schema (node kinds + edge kinds + signatures)."""
    return JSONResponse(content=service.schema())


# ── Kind administration (the evolvable schema) ────────────────────────────────


@router.post("/node-kinds")
def create_node_kind(body: NodeKindIn) -> JSONResponse:
    """Register a new node kind and return its schema entry."""
    result = service.add_node_kind(
        body.name, group=body.group, content_label=body.content_label, fields=body.fields
    )
    return JSONResponse(content=result)


@router.patch("/node-kinds/{name}")
def patch_node_kind(name: str, body: NodeKindPatch) -> JSONResponse:
    """Edit a node kind (only the provided attributes change) and return its entry."""
    result = service.update_node_kind(
        name, group=body.group, content_label=body.content_label, fields=body.fields
    )
    return JSONResponse(content=result)


@router.delete("/node-kinds/{name}")
def delete_node_kind(name: str, into: str | None = None) -> JSONResponse:
    """Delete a node kind; refuses (409) when in use unless ``into`` reassigns it."""
    result = service.delete_node_kind(name, into=into)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.post("/edge-kinds")
def create_edge_kind(body: EdgeKindIn) -> JSONResponse:
    """Register a new edge kind (its from→to signature) and return its schema entry."""
    result = service.add_edge_kind(
        body.name, body.from_kinds, body.to_kinds, symmetric=body.symmetric, fields=body.fields
    )
    return JSONResponse(content=result)


@router.patch("/edge-kinds/{name}")
def patch_edge_kind(name: str, body: EdgeKindPatch) -> JSONResponse:
    """Edit an edge kind (only the provided attributes change) and return its entry."""
    result = service.update_edge_kind(
        name,
        from_kinds=body.from_kinds,
        to_kinds=body.to_kinds,
        symmetric=body.symmetric,
        fields=body.fields,
    )
    return JSONResponse(content=result)


@router.delete("/edge-kinds/{name}")
def delete_edge_kind(name: str, into: str | None = None) -> JSONResponse:
    """Delete an edge kind; refuses (409) when edges use it unless ``into`` reassigns them."""
    result = service.delete_edge_kind(name, into=into)
    return JSONResponse(content=result.model_dump(mode="json"))


app.include_router(router)

# The full-CRUD web view (GET / + /login + /static assets) rides the same app.
web.register(app)
