"""FastAPI HTTP adapter over the nodum data-service layer.

A thin transport: each route parses its request body or query params, calls the
matching :mod:`nodum.service` function, and returns the model serialised exactly
as the CLI emits it — ``model_dump(mode="json")`` wrapped in a ``JSONResponse``,
with no ``response_model`` so keys are neither added nor reordered. Domain errors
raised by the service are mapped to clean JSON: a missing node is a 404, bad
input a 422.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import nodum
from nodum import service, web
from nodum.models import AddEdgeIn, AddNodeIn
from nodum.service import NodeNotFound

app = FastAPI(title="nodum", version=nodum.__version__)


# ── Error mapping ─────────────────────────────────────────────────────────────


@app.exception_handler(NodeNotFound)
async def _handle_node_not_found(request: Request, exc: NodeNotFound) -> JSONResponse:
    """Map a missing-node error to a 404 with a clean JSON detail body."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def _handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
    """Map a service input-validation error to a 422 with a clean JSON detail body."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# ── Routes ────────────────────────────────────────────────────────────────────


@app.post("/nodes")
def create_node(body: AddNodeIn) -> JSONResponse:
    """Create a node.

    Args:
        body: The node text, an optional type, and extra payload keys.

    Returns:
        The created node, serialised exactly as the CLI emits it.
    """
    node = service.add_node(text=body.text, type=body.type, data=body.data)
    return JSONResponse(content=node.model_dump(mode="json"))


@app.post("/edges")
def create_edge(body: AddEdgeIn) -> JSONResponse:
    """Create a directed edge between two existing nodes.

    Args:
        body: The two endpoint UUIDs, an optional type, and extra payload keys.

    Returns:
        The created edge, serialised exactly as the CLI emits it.
    """
    edge = service.add_edge(str(body.from_uuid), str(body.to_uuid), type=body.type, data=body.data)
    return JSONResponse(content=edge.model_dump(mode="json"))


@app.get("/nodes/{uuid}")
def get_node(uuid: UUID) -> JSONResponse:
    """Fetch a node together with every edge incident on it (either direction).

    Args:
        uuid: The node UUID from the path.

    Returns:
        The node and its edges, serialised exactly as the CLI emits it.
    """
    result = service.get(str(uuid))
    return JSONResponse(content=result.model_dump(mode="json"))


@app.get("/search")
def search(q: str, limit: int = 20) -> JSONResponse:
    """Full-text search over node text, ranked best-first.

    Args:
        q: The free-text query.
        limit: Maximum number of hits to return.

    Returns:
        The search result, serialised exactly as the CLI emits it.
    """
    result = service.search(q, limit)
    return JSONResponse(content=result.model_dump(mode="json"))


@app.get("/expand")
def expand(seed: UUID, depth: int = 1) -> JSONResponse:
    """Expand a seed node into its connected subgraph, following edges outward.

    Args:
        seed: The seed node UUID.
        depth: Maximum number of hops to follow (>= 1).

    Returns:
        The expanded subgraph, serialised exactly as the CLI emits it.
    """
    result = service.expand(str(seed), depth)
    return JSONResponse(content=result.model_dump(mode="json"))


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe.

    Returns:
        A static ``{"status": "ok"}`` payload.
    """
    return JSONResponse(content={"status": "ok"})


# The minimal read-first web view (GET / + /static assets) rides the same app.
web.register(app)
