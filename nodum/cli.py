"""The Typer CLI adapter — a thin, Claude-first front end over the service layer.

Every command calls one :mod:`nodum.service` function and serialises the
returned pydantic model as a single JSON object on **stdout**; nothing else is
written there on the success path. Human-facing and error messages go to
**stderr**. Because the HTTP API serialises the same ``model_dump(mode="json")``
payload, identical data yields byte-identical JSON across both surfaces.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import typer
from pydantic import BaseModel

from nodum import service
from nodum.db import connect, init_schema
from nodum.service import NodeNotFound
from nodum.settings import load_settings

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Claude-first CLI over the nodum knowledge graph; each command emits one JSON object.",
)


def _emit(result: BaseModel) -> None:
    """Print a pydantic result as the single JSON object on stdout.

    Uses the shared ``model_dump(mode="json")`` envelope so the CLI and the
    HTTP API produce identical JSON for identical data.
    """
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))


def _parse_data(raw: str) -> dict:
    """Parse the ``--data`` JSON object string, exiting cleanly on bad input.

    Args:
        raw: A JSON object string (``"{}"`` by default).

    Returns:
        The decoded object as a dict.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"--data is not valid JSON: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not isinstance(parsed, dict):
        typer.echo("--data must be a JSON object", err=True)
        raise typer.Exit(1)
    return parsed


@contextmanager
def _service_errors() -> Iterator[None]:
    """Translate expected service failures into a stderr message and exit code 1.

    Keeps stdout clean: ``NodeNotFound`` (missing node) and ``ValueError`` (bad
    input) become a concise stderr line plus ``Exit(1)``, never a traceback or
    stray stdout output.
    """
    try:
        yield
    except (NodeNotFound, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("add-node")
def add_node(
    text: str = typer.Argument(..., help="The node's primary text."),
    type: str | None = typer.Option(None, "--type", "-t", help="Optional type name."),
    data: str = typer.Option("{}", "--data", help="Extra payload as a JSON object string."),
) -> None:
    """Create a node and print it as a NodeOut JSON object."""
    payload = _parse_data(data)
    with _service_errors():
        node = service.add_node(text, type=type, data=payload)
    _emit(node)


@app.command("add-edge")
def add_edge(
    from_uuid: str = typer.Argument(..., help="Source node UUID."),
    to_uuid: str = typer.Argument(..., help="Target node UUID."),
    type: str | None = typer.Option(None, "--type", "-t", help="Optional edge type."),
    data: str = typer.Option("{}", "--data", help="Extra payload as a JSON object string."),
) -> None:
    """Create a directed edge ``from_uuid → to_uuid`` and print it as an EdgeOut JSON object."""
    payload = _parse_data(data)
    with _service_errors():
        edge = service.add_edge(from_uuid, to_uuid, type=type, data=payload)
    _emit(edge)


@app.command("get")
def get(uuid: str = typer.Argument(..., help="The node UUID to fetch.")) -> None:
    """Fetch a node and its incident edges, printed as a NodeWithEdges JSON object."""
    with _service_errors():
        result = service.get(uuid)
    _emit(result)


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Free-text query (AND of terms)."),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum number of hits."),
) -> None:
    """Full-text search node text and print the ranked SearchResult JSON object."""
    with _service_errors():
        result = service.search(query, limit=limit)
    _emit(result)


@app.command("expand")
def expand(
    seed: str = typer.Argument(..., help="Seed node UUID."),
    depth: int = typer.Option(1, "--depth", "-d", help="Maximum number of hops (>= 1)."),
) -> None:
    """Expand a seed node into its connected subgraph and print the Subgraph JSON object."""
    with _service_errors():
        result = service.expand(seed, depth=depth)
    _emit(result)


@app.command("init-db")
def init_db() -> None:
    """Create the schema if absent and print a small status JSON object."""
    with connect() as conn:
        init_schema(conn)
    print(json.dumps({"ok": True, "message": "schema ready"}, indent=2, ensure_ascii=False))


@app.command("serve")
def serve(
    host: str | None = typer.Option(None, "--host", help="Bind address; defaults to api_host."),
    port: int | None = typer.Option(None, "--port", help="Bind port; defaults to api_port."),
) -> None:
    """Run the HTTP API with uvicorn, defaulting host/port from settings."""
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "nodum.api:app",
        host=host if host is not None else settings.api_host,
        port=port if port is not None else settings.api_port,
    )
