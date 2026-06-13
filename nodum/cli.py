"""The Typer CLI adapter — a thin, Claude-first front end over the service layer.

Every command calls one :mod:`nodum.service` function and serialises the result
as a single JSON object on **stdout**; nothing else is written there on the
success path. Human-facing and error messages go to **stderr**. Because the HTTP
API serialises the same ``model_dump(mode="json")`` payload, identical data
yields byte-identical JSON across both surfaces.

``--set key=value`` options carry kind-specific payload keys: each value is
parsed with :func:`json.loads`, falling back to the raw string when that fails,
so ``--set born=1815`` yields an int, ``--set 'aliases=["a","b"]'`` a list, and
``--set venue=Nature`` a plain string.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import typer
from pydantic import BaseModel

from nodum import auth, service
from nodum.db import connect, init_schema, migrate_mvp
from nodum.service import EdgeNotFound, NodeNotFound
from nodum.settings import load_settings

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Claude-first CLI over the nodum typed graph; each command emits one JSON object.",
)


def _print_json(payload: dict) -> None:
    """Write a single JSON object to stdout (the only thing on the success path)."""
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _emit(result: BaseModel) -> None:
    """Print a pydantic result as the single JSON object on stdout.

    Uses the shared ``model_dump(mode="json")`` envelope so the CLI and the HTTP
    API produce identical JSON for identical data.
    """
    _print_json(result.model_dump(mode="json"))


def _parse_set(pairs: list[str] | None) -> dict:
    """Parse repeatable ``--set key=value`` options into a payload dict.

    Each value is decoded with :func:`json.loads`, falling back to the raw string
    when the value is not valid JSON. Exits cleanly when a pair lacks ``=``.

    Args:
        pairs: The raw ``key=value`` strings, or ``None`` when none were given.

    Returns:
        The assembled payload dict (empty when ``pairs`` is falsy).
    """
    data: dict = {}
    for pair in pairs or []:
        key, sep, raw = pair.partition("=")
        if not sep:
            typer.echo(f"--set expects key=value, got {pair!r}", err=True)
            raise typer.Exit(1)
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        data[key] = value
    return data


@contextmanager
def _service_errors() -> Iterator[None]:
    """Translate expected service failures into a stderr message and exit code 1.

    Keeps stdout clean: ``NodeNotFound`` / ``EdgeNotFound`` (missing rows) and
    ``ValueError`` (bad kind, field, or signature — including
    ``metamodel.ValidationError``) become a concise stderr line plus ``Exit(1)``,
    never a traceback or stray stdout output.
    """
    try:
        yield
    except (NodeNotFound, EdgeNotFound, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("add")
def add(
    kind: str = typer.Argument(..., help="The node kind from the metamodel."),
    text: str = typer.Argument(..., help="The node's universal text."),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
) -> None:
    """Create a typed node and print it as a NodeOut JSON object."""
    data = _parse_set(set_)
    with _service_errors():
        node = service.add_node(kind, text, data=data)
    _emit(node)


@app.command("link")
def link(
    from_uuid: str = typer.Argument(..., help="Source node UUID."),
    to_uuid: str = typer.Argument(..., help="Target node UUID."),
    edge_kind: str = typer.Argument(..., help="The edge kind from the metamodel."),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
) -> None:
    """Create a typed, directed edge from_uuid → to_uuid and print it as an EdgeOut JSON object."""
    data = _parse_set(set_)
    with _service_errors():
        edge = service.add_edge(edge_kind, from_uuid, to_uuid, data=data)
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
    kind: str | None = typer.Option(None, "--kind", "-k", help="Optional node-kind filter."),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum number of hits."),
) -> None:
    """Full-text search node text and print the ranked SearchResult JSON object."""
    with _service_errors():
        result = service.search(query, kind=kind, limit=limit)
    _emit(result)


@app.command("expand")
def expand(
    seed: str = typer.Argument(..., help="Seed node UUID."),
    depth: int = typer.Option(1, "--depth", "-d", help="Maximum number of hops (>= 1)."),
    edge_kind: list[str] | None = typer.Option(
        None, "--edge-kind", help="Restrict traversal to these edge kinds (repeatable)."
    ),
) -> None:
    """Expand a seed node into its connected subgraph and print the Subgraph JSON object."""
    with _service_errors():
        result = service.expand(seed, depth=depth, edge_kinds=edge_kind or None)
    _emit(result)


@app.command("edit-node")
def edit_node(
    uuid: str = typer.Argument(..., help="The node UUID to update."),
    text: str | None = typer.Option(None, "--text", help="Replacement node text."),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
) -> None:
    """Merge new text/payload into a node and print the updated NodeOut JSON object."""
    data = _parse_set(set_)
    with _service_errors():
        node = service.update_node(uuid, text=text, data=data)
    _emit(node)


@app.command("edit-edge")
def edit_edge(
    uuid: str = typer.Argument(..., help="The edge UUID to update."),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
) -> None:
    """Merge new payload into an edge and print the updated EdgeOut JSON object."""
    data = _parse_set(set_)
    with _service_errors():
        edge = service.update_edge(uuid, data=data)
    _emit(edge)


@app.command("rm-node")
def rm_node(uuid: str = typer.Argument(..., help="The node UUID to delete.")) -> None:
    """Delete a node (its incident edges cascade) and print a Deleted JSON object."""
    with _service_errors():
        result = service.delete_node(uuid)
    _emit(result)


@app.command("rm-edge")
def rm_edge(uuid: str = typer.Argument(..., help="The edge UUID to delete.")) -> None:
    """Delete a single edge and print a Deleted JSON object."""
    with _service_errors():
        result = service.delete_edge(uuid)
    _emit(result)


@app.command("schema")
def schema() -> None:
    """Print the metamodel contract (node kinds + edge kinds + signatures) as JSON."""
    _print_json(service.schema())


@app.command("init-db")
def init_db() -> None:
    """Create the typed schema and seed kind tables if absent; print a status JSON object."""
    with connect() as conn:
        init_schema(conn)
    _print_json({"ok": True, "message": "schema ready"})


@app.command("migrate")
def migrate() -> None:
    """Upgrade a pre-typed (MVP) database in place; print a status JSON object."""
    with connect() as conn:
        migrate_mvp(conn)
    _print_json({"ok": True, "message": "migrated"})


auth_app = typer.Typer(
    no_args_is_help=True,
    help="Manage the single main password that gates the API and web view.",
)
app.add_typer(auth_app, name="auth")


@auth_app.command("set-password")
def auth_set_password(
    password: str | None = typer.Option(
        None, "--password", help="Set non-interactively (discouraged; prefer the prompt)."
    ),
) -> None:
    """Set or replace the main password; prints a status JSON object (never the hash).

    With no ``--password`` and an interactive terminal, prompts twice with no echo.
    When stdin is piped, reads the password from the first line (for automation).
    """
    if password is None:
        if sys.stdin.isatty():
            password = typer.prompt("New main password", hide_input=True, confirmation_prompt=True)
        else:
            password = sys.stdin.readline().rstrip("\n")
    if not password:
        typer.echo("password must not be empty", err=True)
        raise typer.Exit(1)
    result = auth.set_password(password)
    _print_json(
        {
            "ok": True,
            "configured": result.configured,
            "updated_at": result.updated_at.isoformat() if result.updated_at else None,
        }
    )


@auth_app.command("status")
def auth_status() -> None:
    """Print whether a main password is configured and when it was last set."""
    result = auth.status()
    _print_json(
        {
            "configured": result.configured,
            "updated_at": result.updated_at.isoformat() if result.updated_at else None,
        }
    )


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
