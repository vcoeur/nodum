"""FastAPI HTTP adapter over the nodum data-service layer.

A thin transport with no logic of its own: each route parses its request body or
query params, calls the matching :mod:`nodum.service` function, and returns the
model serialised exactly as the CLI emits it — ``model_dump(mode="json")``
wrapped in a ``JSONResponse``, with no ``response_model`` so keys are neither
added, dropped, nor reordered. Identical data therefore yields byte-identical
JSON across the CLI and the HTTP API. Domain errors raised by the service are
mapped to clean JSON: a missing node/edge is a 404, bad input a 422.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

import nodum
from nodum import service, web
from nodum.models import AddEdgeIn, AddNodeIn, UpdateEdgeIn, UpdateNodeIn
from nodum.service import EdgeNotFound, NodeNotFound

app = FastAPI(title="nodum", version=nodum.__version__)


# ── Error mapping ─────────────────────────────────────────────────────────────


@app.exception_handler(NodeNotFound)
@app.exception_handler(EdgeNotFound)
async def _handle_not_found(request: Request, exc: Exception) -> JSONResponse:
    """Map a missing node/edge error to a 404 with a clean JSON detail body."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def _handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
    """Map a service input-validation error (incl. ValidationError) to a 422."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# ── Nodes ─────────────────────────────────────────────────────────────────────


@app.post("/nodes")
def create_node(body: AddNodeIn) -> JSONResponse:
    """Create a typed node and return it, serialised exactly as the CLI emits it."""
    node = service.add_node(body.kind, body.text, body.data)
    return JSONResponse(content=node.model_dump(mode="json"))


@app.get("/nodes/{uuid}")
def get_node(uuid: UUID) -> JSONResponse:
    """Fetch a node with every edge incident on it (either direction)."""
    result = service.get(str(uuid))
    return JSONResponse(content=result.model_dump(mode="json"))


@app.patch("/nodes/{uuid}")
def patch_node(uuid: UUID, body: UpdateNodeIn) -> JSONResponse:
    """Merge new text/payload into a node, re-validate, and return it."""
    node = service.update_node(str(uuid), text=body.text, data=body.data)
    return JSONResponse(content=node.model_dump(mode="json"))


@app.delete("/nodes/{uuid}")
def delete_node(uuid: UUID) -> JSONResponse:
    """Delete a node; its incident edges cascade. Returns the cascade count."""
    result = service.delete_node(str(uuid))
    return JSONResponse(content=result.model_dump(mode="json"))


# ── Edges ─────────────────────────────────────────────────────────────────────


@app.post("/edges")
def create_edge(body: AddEdgeIn) -> JSONResponse:
    """Create a typed, directed edge between two existing nodes and return it."""
    edge = service.add_edge(body.kind, str(body.from_uuid), str(body.to_uuid), body.data)
    return JSONResponse(content=edge.model_dump(mode="json"))


@app.patch("/edges/{uuid}")
def patch_edge(uuid: UUID, body: UpdateEdgeIn) -> JSONResponse:
    """Merge new payload into an edge (kind and endpoints fixed) and return it."""
    edge = service.update_edge(str(uuid), data=body.data)
    return JSONResponse(content=edge.model_dump(mode="json"))


@app.delete("/edges/{uuid}")
def delete_edge(uuid: UUID) -> JSONResponse:
    """Delete a single edge and return the delete count."""
    result = service.delete_edge(str(uuid))
    return JSONResponse(content=result.model_dump(mode="json"))


# ── Query ─────────────────────────────────────────────────────────────────────


@app.get("/search")
def search(q: str, kind: str | None = None, limit: int = 20) -> JSONResponse:
    """Full-text search over node text, ranked best-first, optionally by kind."""
    result = service.search(q, kind=kind, limit=limit)
    return JSONResponse(content=result.model_dump(mode="json"))


@app.get("/expand")
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


@app.get("/schema")
def schema() -> JSONResponse:
    """Return the metamodel contract (node kinds + edge kinds + signatures)."""
    return JSONResponse(content=service.schema())


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe returning a static ``{"status": "ok"}`` payload."""
    return JSONResponse(content={"status": "ok"})


# The full-CRUD web view (GET / + /static assets) rides the same app.
web.register(app)
