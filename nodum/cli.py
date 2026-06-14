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
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from pydantic import BaseModel

from nodum import auth, service
from nodum.db import connect, init_schema, migrate
from nodum.service import EdgeNotFound, KindInUse, KindNotFound, NodeNotFound
from nodum.settings import load_settings

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Claude-first CLI over the nodum graph; each command emits one JSON object.",
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


def _parse_fields(raw: str | None) -> dict:
    """Parse a ``--fields`` JSON object (name → field spec) into a dict.

    Mirrors the ``fields`` shape that ``schema`` emits, e.g.
    ``'{"aliases": {"type": "list[str]"}, "born": {"type": "int"}}'``. Exits
    cleanly when the value is not a JSON object.
    """
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"--fields must be valid JSON: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not isinstance(value, dict):
        typer.echo("--fields must be a JSON object mapping name → spec", err=True)
        raise typer.Exit(1)
    return value


@contextmanager
def _service_errors() -> Iterator[None]:
    """Translate expected service failures into a stderr message and exit code 1.

    Keeps stdout clean: missing rows (``NodeNotFound`` / ``EdgeNotFound`` /
    ``KindNotFound``), a still-referenced kind (``KindInUse``), and ``ValueError``
    (bad kind, field, or signature — including ``metamodel.ValidationError``)
    become a concise stderr line plus ``Exit(1)``, never a traceback or stray
    stdout output.
    """
    try:
        yield
    except (NodeNotFound, EdgeNotFound, KindNotFound, KindInUse, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("add")
def add(
    kind: str = typer.Argument(..., help="The node kind from the schema."),
    content: str = typer.Argument(..., help="The node's plain-text content (embeddable body)."),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
) -> None:
    """Create a typed node and print it as a NodeOut JSON object."""
    data = _parse_set(set_)
    with _service_errors():
        node = service.add_node(kind, content, data=data)
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
    content: str | None = typer.Option(None, "--content", help="Replacement node content."),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
) -> None:
    """Merge new content/payload into a node and print the updated NodeOut JSON object."""
    data = _parse_set(set_)
    with _service_errors():
        node = service.update_node(uuid, content=content, data=data)
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
    """Print the live schema (node kinds + edge kinds + signatures) as JSON."""
    _print_json(service.schema())


@app.command("init-db")
def init_db() -> None:
    """Create the schema and seed the default kind catalog if absent; print a status object."""
    with connect() as conn:
        init_schema(conn)
    _print_json({"ok": True, "message": "schema ready"})


@app.command("migrate")
def migrate_db() -> None:
    """Upgrade an older database in place (kinds, content, auth); print a status object."""
    with connect() as conn:
        migrate(conn)
    _print_json({"ok": True, "message": "migrated"})


# ── Kind administration (the evolvable schema) ────────────────────────────────

node_kind_app = typer.Typer(
    no_args_is_help=True,
    help="Manage node kinds — add / edit / remove entries in the evolvable schema.",
)
app.add_typer(node_kind_app, name="node-kind")

edge_kind_app = typer.Typer(
    no_args_is_help=True,
    help="Manage edge kinds — add / edit / remove entries in the evolvable schema.",
)
app.add_typer(edge_kind_app, name="edge-kind")


@node_kind_app.command("add")
def node_kind_add(
    name: str = typer.Argument(..., help="The new node kind's name."),
    group: str = typer.Option("", "--group", help="Display group (e.g. entity / note)."),
    content_label: str = typer.Option(
        "text", "--content-label", help="What this kind's content means (e.g. name / citation)."
    ),
    fields: str | None = typer.Option(
        None, "--fields", help="Field schema as a JSON object: name → {type, required, choices, …}."
    ),
) -> None:
    """Register a new node kind and print its schema entry."""
    fields_dict = _parse_fields(fields)
    with _service_errors():
        result = service.add_node_kind(
            name, group=group, content_label=content_label, fields=fields_dict
        )
    _print_json(result)


@node_kind_app.command("edit")
def node_kind_edit(
    name: str = typer.Argument(..., help="The node kind to edit."),
    group: str | None = typer.Option(None, "--group", help="Replacement display group."),
    content_label: str | None = typer.Option(
        None, "--content-label", help="Replacement content label."
    ),
    fields: str | None = typer.Option(
        None, "--fields", help="Replacement field schema as a JSON object (replaces all fields)."
    ),
) -> None:
    """Edit a node kind (only the options you pass change) and print its schema entry."""
    fields_dict = _parse_fields(fields) if fields is not None else None
    with _service_errors():
        result = service.update_node_kind(
            name, group=group, content_label=content_label, fields=fields_dict
        )
    _print_json(result)


@node_kind_app.command("rm")
def node_kind_rm(
    name: str = typer.Argument(..., help="The node kind to delete."),
    into: str | None = typer.Option(
        None, "--into", help="Reassign this kind's nodes + signatures here, then delete."
    ),
) -> None:
    """Delete a node kind; refuses when in use unless --into reassigns it first."""
    with _service_errors():
        result = service.delete_node_kind(name, into=into)
    _emit(result)


@edge_kind_app.command("add")
def edge_kind_add(
    name: str = typer.Argument(..., help="The new edge kind's name."),
    from_kinds: list[str] | None = typer.Option(
        None, "--from", help="Allowed source node kind (repeatable)."
    ),
    to_kinds: list[str] | None = typer.Option(
        None, "--to", help="Allowed target node kind (repeatable)."
    ),
    symmetric: bool = typer.Option(False, "--symmetric", help="Mark the relation symmetric."),
    fields: str | None = typer.Option(
        None, "--fields", help="Field schema as a JSON object: name → {type, required, choices, …}."
    ),
) -> None:
    """Register a new edge kind (its from→to signature) and print its schema entry."""
    fields_dict = _parse_fields(fields)
    with _service_errors():
        result = service.add_edge_kind(
            name, from_kinds or [], to_kinds or [], symmetric=symmetric, fields=fields_dict
        )
    _print_json(result)


@edge_kind_app.command("edit")
def edge_kind_edit(
    name: str = typer.Argument(..., help="The edge kind to edit."),
    from_kinds: list[str] | None = typer.Option(
        None, "--from", help="Replacement source node kinds (repeatable; replaces all)."
    ),
    to_kinds: list[str] | None = typer.Option(
        None, "--to", help="Replacement target node kinds (repeatable; replaces all)."
    ),
    symmetric: bool | None = typer.Option(
        None, "--symmetric/--asymmetric", help="Set or clear the symmetric flag."
    ),
    fields: str | None = typer.Option(
        None, "--fields", help="Replacement field schema as a JSON object (replaces all fields)."
    ),
) -> None:
    """Edit an edge kind (only the options you pass change) and print its schema entry."""
    fields_dict = _parse_fields(fields) if fields is not None else None
    with _service_errors():
        result = service.update_edge_kind(
            name,
            from_kinds=from_kinds,
            to_kinds=to_kinds,
            symmetric=symmetric,
            fields=fields_dict,
        )
    _print_json(result)


@edge_kind_app.command("rm")
def edge_kind_rm(
    name: str = typer.Argument(..., help="The edge kind to delete."),
    into: str | None = typer.Option(
        None, "--into", help="Reassign edges of this kind to this kind, then delete."
    ),
    purge: bool = typer.Option(
        False, "--purge", help="Delete this kind's edges too, then delete the kind."
    ),
) -> None:
    """Delete an edge kind; refuses when edges use it unless --into or --purge resolves them."""
    with _service_errors():
        result = service.delete_edge_kind(name, into=into, purge=purge)
    _emit(result)


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


def _admin_password_from_env() -> str | None:
    """Read the bootstrap admin password from the env (file first, then value).

    ``NODUM_ADMIN_PASSWORD_FILE`` (a path, e.g. a Docker secret) takes precedence
    over ``NODUM_ADMIN_PASSWORD``. Returns ``None`` when neither is set or the
    file cannot be read.
    """
    path = os.environ.get("NODUM_ADMIN_PASSWORD_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    value = os.environ.get("NODUM_ADMIN_PASSWORD")
    return value.strip() if value else None


@auth_app.command("ensure-password")
def auth_ensure_password() -> None:
    """Set the main password from a secret on first boot; no-op if already set.

    Reads ``NODUM_ADMIN_PASSWORD_FILE`` (preferred) or ``NODUM_ADMIN_PASSWORD``.
    Used by the Docker entrypoint so a fresh deploy is hands-off. An already
    configured password is left untouched, so a later manual change survives a
    restart; with no secret and no password set, the install stays locked.
    """
    if auth.is_configured():
        _print_json({"configured": True, "action": "unchanged"})
        return
    password = _admin_password_from_env()
    if not password:
        _print_json({"configured": False, "action": "no-secret"})
        return
    auth.set_password(password)
    _print_json({"configured": True, "action": "set"})


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
