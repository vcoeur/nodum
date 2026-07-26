"""The Typer CLI adapter — a thin, JSON-emitting front end over the service layer.

Every command calls one :mod:`nodum.service` function and prints the result as
a single JSON object on **stdout**; human-facing and error messages go to
**stderr**. There is no ``--json`` flag — JSON is the only output format.

The database path comes from ``--db`` or the ``NODUM_DB`` environment variable,
falling back to ``~/.local/share/nodum/nodum.db``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

import typer
from pydantic import BaseModel

from nodum import __version__, assets, db, projectors, service
from nodum import search as search_module
from nodum.assets import AssetNotFound, AssetSourceChanged, AssetTooLarge, UnsupportedRendition
from nodum.cli_schema import build_cli_schema
from nodum.db import ENV_DB_VAR
from nodum.envelope import envelope, list_envelope, render_json
from nodum.principal import Principal
from nodum.service import (
    EventNotFound,
    GrantNotPermitted,
    InvalidTransition,
    RecordNotFound,
    TypeNotFound,
    UndoNotPossible,
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="JSON-emitting CLI over the nodum knowledge graph; each command prints one JSON object.",
)
node_app = typer.Typer(no_args_is_help=True, help="Node operations.")
edge_app = typer.Typer(no_args_is_help=True, help="Edge operations.")
projector_app = typer.Typer(
    no_args_is_help=True, help="Derived-index projectors over the event log."
)
human_app = typer.Typer(no_args_is_help=True, help="Human account administration.")
agent_app = typer.Typer(no_args_is_help=True, help="Agent account administration.")
review_app = typer.Typer(
    no_args_is_help=True, help="The review queue: pending proposals (human actor only)."
)
mcp_app = typer.Typer(
    no_args_is_help=True, help="MCP server for external agents (read + additive tiers, design §8)."
)
asset_app = typer.Typer(
    no_args_is_help=True, help="Content-addressed assets and derived image renditions."
)
app.add_typer(node_app, name="node")
app.add_typer(edge_app, name="edge")
app.add_typer(projector_app, name="projector")
app.add_typer(review_app, name="review")
app.add_typer(human_app, name="human")
app.add_typer(agent_app, name="agent")
app.add_typer(mcp_app, name="mcp")
app.add_typer(asset_app, name="asset")


@app.callback(invoke_without_command=True)
def _main(
    db_path: str | None = typer.Option(
        None, "--db", help=f"Database path (overrides ${ENV_DB_VAR})."
    ),
    version: bool = typer.Option(
        False,
        "--version",
        help="Print version and exit.",
        is_eager=True,
    ),
) -> None:
    """Resolve the database path for this invocation, and handle `--version`.

    The `invoke_without_command=True` + `no_args_is_help=True` combo lets
    `nodum --version` short-circuit without triggering the usage text; a bare
    `nodum` with no subcommand still falls through to the help view.
    """
    if version:
        typer.echo(f"nodum {__version__}")
        raise typer.Exit(0)
    if db_path is not None:
        os.environ[ENV_DB_VAR] = db_path


def _print_json(payload: dict) -> None:
    """Write a single JSON object to stdout (the only thing on the success path)."""
    print(render_json(payload))


def _emit(result: BaseModel) -> None:
    """Print a pydantic result as the single JSON object on stdout."""
    _print_json(envelope(result))


def _emit_list(key: str, results: Sequence[BaseModel]) -> None:
    """Print a list result under its plural key plus a ``count``."""
    _print_json(list_envelope(key, results))


def _parse_set(pairs: list[str] | None) -> dict:
    """Parse repeatable ``--set key=value`` options into a props dict.

    Each value is decoded with :func:`json.loads`, falling back to the raw
    string, so ``--set year=1815`` yields an int and ``--set venue=Nature`` a
    string. Exits cleanly when a pair lacks ``=``.
    """
    props: dict = {}
    for pair in pairs or []:
        key, sep, raw = pair.partition("=")
        if not sep:
            typer.echo(f"--set expects key=value, got {pair!r}", err=True)
            raise typer.Exit(1)
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        props[key] = value
    return props


def _read_content(content: str | None, content_file: str | None) -> str | None:
    """Resolve node content from ``--content`` or ``--content-file`` (``-`` = stdin)."""
    if content is not None and content_file is not None:
        typer.echo("use either --content or --content-file, not both", err=True)
        raise typer.Exit(1)
    if content_file is not None:
        if content_file == "-":
            return sys.stdin.read()
        with open(content_file) as handle:
            return handle.read()
    return content


def _run(func, *args, **kwargs):
    """Call a service function, mapping expected errors to stderr + exit 1.

    Everything a caller can provoke — a bad id, a refused actor, a missing
    file, a database another writer is holding — is a message on stderr and
    exit 1, never a traceback: the CLI's contract is one JSON object on
    success and a readable line on failure.
    """
    try:
        return func(*args, **kwargs)
    except (
        # RecordNotFound covers node/edge/version ids and the transition
        # entry points that accept all three.
        RecordNotFound,
        TypeNotFound,
        EventNotFound,
        AssetNotFound,
        AssetTooLarge,
        AssetSourceChanged,
        UnsupportedRendition,
        InvalidTransition,
        GrantNotPermitted,
        UndoNotPossible,
        ValueError,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except OSError as exc:
        # A missing/unreadable file (`asset register /missing.png`) or a
        # database path the process cannot open.
        typer.echo(f"{exc.strerror or exc}: {exc.filename}" if exc.filename else str(exc), err=True)
        raise typer.Exit(1) from exc
    except sqlite3.Error as exc:
        # Chiefly "database is locked": SQLite allows one writer, so a
        # concurrent write that outlasts the busy timeout lands here.
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc


AS_OPTION = typer.Option(
    ...,
    "--as",
    help="Your human account ('human:<id>' or '<id>') — attribution is explicit, always.",
)


def _principal(as_human: str) -> Principal:
    """Resolve ``--as`` to a human principal (trusted-local, no password)."""
    from nodum import auth

    human_id = as_human.removeprefix("human:")
    return auth.owner_principal(human_id)


SET_OPTION = typer.Option(None, "--set", help="Repeatable key=value props (values parsed as JSON).")


@app.command()
def init() -> None:
    """Create the database (if needed) and apply pending migrations."""
    _emit(_run(service.init))


@node_app.command("create")
def node_create(
    type: str = typer.Option(..., "--type", "-t", help="Node type id or name."),
    title: str | None = typer.Option(None, "--title", help="Display title (wikilink target)."),
    content: str | None = typer.Option(None, "--content", "-c", help="Markdown body."),
    content_file: str | None = typer.Option(
        None, "--content-file", help="Read the Markdown body from a file ('-' = stdin)."
    ),
    parent: str | None = typer.Option(None, "--parent", help="Parent node id."),
    set_props: list[str] | None = SET_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Create a node (active for actor 'human', proposed otherwise)."""
    node = _run(
        service.create_node,
        type=type,
        title=title,
        content=_read_content(content, content_file) or "",
        parent_id=parent,
        props=_parse_set(set_props),
        principal=_principal(as_human),
    )
    _emit(node)


@node_app.command("get")
def node_get(
    node_id: str = typer.Argument(..., help="Node id."),
    depth: int = typer.Option(
        0, "--depth", help="Include the active-edge neighborhood out to this many hops."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Fetch one node by id (plus its neighborhood when --depth > 0)."""
    if depth > 0:
        _emit(_run(service.get_neighborhood, node_id, depth=depth, principal=_principal(as_human)))
    else:
        _emit(_run(service.get_node, node_id, principal=_principal(as_human)))


@node_app.command("update")
def node_update(
    node_id: str = typer.Argument(..., help="Node id."),
    title: str | None = typer.Option(None, "--title", help="New title."),
    content: str | None = typer.Option(None, "--content", "-c", help="New Markdown body."),
    content_file: str | None = typer.Option(
        None, "--content-file", help="Read the new Markdown body from a file ('-' = stdin)."
    ),
    set_props: list[str] | None = SET_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Update a node (applies with edit, stages a proposed version with suggest)."""
    kwargs: dict = {"principal": _principal(as_human)}
    resolved = _read_content(content, content_file)
    if title is not None:
        kwargs["title"] = title
    if resolved is not None:
        kwargs["content"] = resolved
    if set_props:
        kwargs["props"] = _parse_set(set_props)
    if len(kwargs) == 1:
        typer.echo("nothing to update: pass --title, --content/--content-file, or --set", err=True)
        raise typer.Exit(1)
    _emit(_run(service.update_node, node_id, **kwargs))


@node_app.command("list")
def node_list(
    type: str | None = typer.Option(None, "--type", "-t", help="Filter by node type."),
    state: str | None = typer.Option(None, "--state", help="Filter by state."),
    parent: str | None = typer.Option(None, "--parent", help="Filter by parent node id."),
    limit: int = typer.Option(500, "--limit", help="Maximum rows."),
    as_human: str = AS_OPTION,
) -> None:
    """List nodes in creation order, optionally filtered."""
    nodes = _run(
        service.list_nodes,
        type=type,
        state=state,
        parent_id=parent,
        principal=_principal(as_human),
        limit=limit,
    )
    _emit_list("nodes", nodes)


@node_app.command("children")
def node_children(
    node_id: str = typer.Argument(..., help="Parent node id."),
    as_human: str = AS_OPTION,
) -> None:
    """List a node's children in position order."""
    nodes = _run(service.list_children, node_id, principal=_principal(as_human))
    _emit_list("nodes", nodes)


@edge_app.command("create")
def edge_create(
    src_id: str = typer.Argument(..., help="Source node id."),
    dst_id: str = typer.Argument(..., help="Target node id."),
    type: str = typer.Option(..., "--type", "-t", help="Edge type id or name."),
    confidence: float | None = typer.Option(None, "--confidence", help="Confidence in [0, 1]."),
    set_props: list[str] | None = SET_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Create a typed, directed edge between two nodes."""
    edge = _run(
        service.create_edge,
        src_id,
        dst_id,
        type,
        props=_parse_set(set_props),
        confidence=confidence,
        principal=_principal(as_human),
    )
    _emit(edge)


@edge_app.command("list")
def edge_list(
    node: str | None = typer.Option(None, "--node", help="Filter by incident node id."),
    type: str | None = typer.Option(None, "--type", "-t", help="Filter by edge type."),
    state: str | None = typer.Option(None, "--state", help="Filter by state."),
    limit: int = typer.Option(500, "--limit", help="Maximum rows."),
    as_human: str = AS_OPTION,
) -> None:
    """List edges, optionally filtered by incident node, type, or state."""
    edges = _run(
        service.list_edges,
        node_id=node,
        type=type,
        state=state,
        principal=_principal(as_human),
        limit=limit,
    )
    _emit_list("edges", edges)


@edge_app.command("create-batch")
def edge_create_batch(
    suggestions_file: str = typer.Argument(
        ..., help="JSON array of {src, dst, edge_type, props?, confidence?} ('-' = stdin)."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Propose a batch of edges; bad suggestions are reported, not fatal."""
    raw = sys.stdin.read() if suggestions_file == "-" else Path(suggestions_file).read_text()
    try:
        suggestions = json.loads(raw)
    except json.JSONDecodeError:
        typer.echo("expected a JSON array of edge suggestions", err=True)
        raise typer.Exit(1) from None
    if not isinstance(suggestions, list):
        typer.echo("expected a JSON array of edge suggestions", err=True)
        raise typer.Exit(1)
    _emit(_run(service.propose_edges, suggestions, principal=_principal(as_human)))


@app.command()
def accept(
    record_id: str = typer.Argument(..., help="Node, edge, or proposed-version id."),
    as_human: str = AS_OPTION,
) -> None:
    """Accept a proposed node, edge, or update (proposed → active) — human actor only.

    A proposed-version id applies exactly the fields the proposal named to
    the node as it stands now.
    """
    _emit(_run(service.transition, record_id, "accept", principal=_principal(as_human)))


@app.command()
def reject(
    record_id: str = typer.Argument(..., help="Node, edge, or proposed-version id."),
    reason: str = typer.Option(..., "--reason", help="Recorded in the reject event."),
    as_human: str = AS_OPTION,
) -> None:
    """Reject a proposed node, edge, or update (proposed → archived) — human actor only.

    The reason is required and lands in the event payload, exactly as it does
    for the batch `review reject` — one operation, one audit guarantee.
    """
    _emit(
        _run(
            service.transition,
            record_id,
            "reject",
            reason=reason,
            principal=_principal(as_human),
        )
    )


@app.command()
def archive(
    record_id: str = typer.Argument(..., help="Node or edge id."),
    as_human: str = AS_OPTION,
) -> None:
    """Archive an active node or edge (active → archived)."""
    _emit(_run(service.transition, record_id, "archive", principal=_principal(as_human)))


@app.command()
def undo(
    seq: int | None = typer.Argument(None, help="Event seq to reverse (default: latest)."),
    as_human: str = AS_OPTION,
) -> None:
    """Reverse an event, restoring the prior state from its payload."""
    _emit(_run(service.undo, seq, principal=_principal(as_human)))


@app.command()
def history(
    node_id: str = typer.Argument(..., help="Node id."),
    as_human: str = AS_OPTION,
) -> None:
    """Show a node's version history (chronological)."""
    versions = _run(service.history, node_id, principal=_principal(as_human))
    _emit_list("versions", versions)


@app.command()
def events(
    limit: int = typer.Option(50, "--limit", help="Maximum rows."),
    as_human: str = AS_OPTION,
) -> None:
    """Show the most recent event-log entries (newest first)."""
    rows = _run(service.list_events, _principal(as_human), limit=limit)
    _emit_list("events", rows)


@app.command(name="types")
def list_types(as_human: str = AS_OPTION) -> None:
    """Show the full type catalog (node types and edge types)."""
    _emit(_run(service.list_types, principal=_principal(as_human)))


@app.command()
def search(
    query: str = typer.Argument(..., help="Free-text query; terms are ANDed."),
    k: int = typer.Option(10, "--k", help="Maximum hits."),
    state: str = typer.Option(
        "active", "--state", help="Node-state filter ('any' searches all states)."
    ),
    type: str | None = typer.Option(None, "--type", "-t", help="Filter by node type."),
    created_by: str | None = typer.Option(None, "--created-by", help="Filter by writer."),
    created_after: str | None = typer.Option(
        None, "--created-after", help="Only nodes created after this timestamp."
    ),
    created_before: str | None = typer.Option(
        None, "--created-before", help="Only nodes created before this timestamp."
    ),
    expand: bool = typer.Option(
        False, "--expand", help="Append one-hop active-edge neighbors of the hits."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Hybrid-search node title + content (BM25 + vector, RRF-fused).

    The vector signal participates when an embedding provider is available
    (fastembed installed and the model cached; otherwise search silently
    degrades to BM25). `signals` on each hit names what contributed.
    """
    result = _run(
        search_module.search,
        query,
        k=k,
        state=None if state == "any" else state,
        type=type,
        created_by=created_by,
        created_after=created_after,
        created_before=created_before,
        expand=expand,
        principal=_principal(as_human),
    )
    _emit(result)


@app.command(name="suggest-links")
def suggest_links(
    prefix: str = typer.Argument(..., help="Title prefix typed so far ('' matches every title)."),
    limit: int = typer.Option(20, "--limit", help="Maximum suggestions."),
    as_human: str = AS_OPTION,
) -> None:
    """Suggest wikilink targets by title prefix (case-insensitive).

    Backs an editor's autocomplete. Reads the node table directly, not an
    index, so it answers on a database whose projectors have never run.
    Archived nodes are never suggested.
    """
    nodes = _run(service.suggest_links, prefix, principal=_principal(as_human), limit=limit)
    _emit_list("nodes", nodes)


@app.command()
def traverse(
    start_id: str = typer.Argument(..., help="Node id to start from."),
    edge_type: list[str] | None = typer.Option(
        None, "--edge-type", help="Restrict to these edge types (repeatable)."
    ),
    depth: int = typer.Option(2, "--depth", help="Maximum hops."),
    direction: str = typer.Option("both", "--direction", help="'out', 'in', or 'both'."),
    as_human: str = AS_OPTION,
) -> None:
    """Walk the subgraph reachable from a node over active edges."""
    _emit(
        _run(
            service.traverse,
            start_id,
            edge_types=edge_type,
            depth=depth,
            direction=direction,
            principal=_principal(as_human),
        )
    )


@app.command()
def subgraph(
    root_id: str = typer.Argument(..., help="Node id at the centre of the subgraph."),
    depth: int = typer.Option(2, "--depth", help="Maximum hops."),
    edge_type: list[str] | None = typer.Option(
        None, "--edge-type", help="Only follow these edge types (repeatable)."
    ),
    edge_state: list[str] | None = typer.Option(
        None, "--edge-state", help="Edge states to follow (repeatable; default: active)."
    ),
    min_confidence: float | None = typer.Option(
        None, "--min-confidence", help="Only follow edges whose stated confidence reaches this."
    ),
    created_by: str | None = typer.Option(
        None, "--created-by", help="Only follow edges written by this actor."
    ),
    node_type: list[str] | None = typer.Option(
        None, "--node-type", help="Only include nodes of these types (repeatable)."
    ),
    limit: int = typer.Option(200, "--limit", help="Maximum nodes, root included."),
    as_human: str = AS_OPTION,
) -> None:
    """Bounded, filtered neighborhood of a node — node and edge caps stop the walk.

    Unlike `traverse` (edge type only), every filter here composes, and
    `--limit` is enforced while walking rather than by slicing afterwards, and
    is clamped to a server ceiling of 2000. The edge list is capped with it,
    since a node cap does not bound edges. `truncated` says whether either cap
    cut the walk short.
    """
    _emit(
        _run(
            service.subgraph,
            root_id,
            depth=depth,
            edge_types=edge_type,
            edge_states=edge_state,
            min_confidence=min_confidence,
            created_by=created_by,
            node_types=node_type,
            principal=_principal(as_human),
            limit=limit,
        )
    )


@app.command(name="find-path")
def find_path(
    a: str = typer.Argument(..., help="Start node id."),
    b: str = typer.Argument(..., help="Target node id."),
    as_human: str = AS_OPTION,
) -> None:
    """Find the shortest path between two nodes over active edges."""
    _emit(_run(service.find_path, a, b, principal=_principal(as_human)))


@app.command()
def diff(
    a: int = typer.Argument(..., help="First version id (see `history`)."),
    b: int = typer.Argument(..., help="Second version id."),
    as_human: str = AS_OPTION,
) -> None:
    """Unified diff between two versions of one node."""
    _emit(_run(service.diff_versions, a, b, principal=_principal(as_human)))


@app.command()
def schema(
    type: str = typer.Argument(..., help="Node or edge type id/name."),
    as_human: str = AS_OPTION,
) -> None:
    """Show one type's catalog entry, including its JSON schema."""
    _emit(_run(service.get_schema, type, principal=_principal(as_human)))


@app.command("schema-dump")
def schema_dump() -> None:
    """Describe this CLI's own command surface (no database needed)."""
    _print_json(build_cli_schema())


@projector_app.command("run")
def projector_run(
    names: list[str] | None = typer.Argument(
        None, help="Projectors to run (default: all registered)."
    ),
) -> None:
    """Apply pending event-log entries to the derived indexes."""
    runs = _run(projectors.run_projectors, names=names)
    _emit_list("projectors", runs)


@projector_app.command("rebuild")
def projector_rebuild(name: str = typer.Argument(..., help="Projector to rebuild.")) -> None:
    """Drop one projector's derived state and replay the full event log."""
    _emit(_run(projectors.rebuild_projector, name))


@projector_app.command("status")
def projector_status() -> None:
    """Show every projector's checkpoint, backlog, and derived-store size."""
    statuses = _run(projectors.projector_status)
    _emit_list("projectors", statuses)


# ── Assets and renditions ─────────────────────────────────────────────────────


@asset_app.command("register")
def asset_register(
    file: str = typer.Argument(
        ..., help="Local file to store in the database, keyed by its sha256."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Original name to record (default: the file's name)."
    ),
) -> None:
    """Register a file as a content-addressed asset (idempotent dedup by sha256)."""
    _emit(_run(assets.register_asset, file, name=name))


@asset_app.command("get")
def asset_get(
    id_or_hash: str = typer.Argument(..., help="Asset hash or asset-reference node id."),
) -> None:
    """Show one asset's metadata (never its bytes)."""
    _emit(_run(assets.get_asset, id_or_hash))


@asset_app.command("list")
def asset_list() -> None:
    """List every registered asset."""
    rows = _run(assets.list_assets)
    _emit_list("assets", rows)


@asset_app.command("rendition")
def asset_rendition(
    id_or_hash: str = typer.Argument(..., help="Asset hash or asset-reference node id."),
    profile: str = typer.Option("preview", "--profile", "-p", help="'thumb' or 'preview'."),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Also write the WebP bytes to this path."
    ),
) -> None:
    """Fetch an image rendition, generating and caching it on first request.

    Prints the rendition metadata; image bytes stay in the database and are
    never inlined into the JSON output — use ``--out`` to extract them.
    """
    rendition = _run(assets.get_rendition, id_or_hash, profile=profile)
    if out is not None:
        _run(assets.copy_rendition, rendition, out)
    _emit(rendition)


@asset_app.command("purge")
def asset_purge(
    asset: str | None = typer.Option(
        None, "--asset", help="Limit the purge to one asset's renditions (hash)."
    ),
) -> None:
    """Evict stored renditions (regenerable — they rebuild on next request)."""
    _emit(_run(assets.purge_renditions, asset_hash=asset))


# ── MCP server ────────────────────────────────────────────────────────────────


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Launch the MCP server on stdio (read + additive tiers, design §8).

    Authentication is the agent token in ``NODUM_AGENT_TOKEN`` — minted by
    ``nodum agent create`` and carried in the MCP client config's env block.
    """
    from nodum import mcp_server

    _run(mcp_server.serve)


# ── HTTP server (the human surface) ──────────────────────────────────────────

#: Default port for ``nodum serve``. The frontend dev server proxies ``/api``
#: and ``/healthz`` here, so changing it means changing `web/vite.config.ts`.
#:
#: 8600 is not arbitrary: each app in this workspace owns an 8xxx decade so two
#: can run side by side, and 86xx is nodum's. Do not "just pick a free port" —
#: 84xx belongs to another app, and 8000/8080 are avoided because unrelated
#: tools take them.
DEFAULT_HTTP_PORT = 8600


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind (loopback default)."),
    port: int = typer.Option(DEFAULT_HTTP_PORT, "--port", help="TCP port to listen on."),
    token: str | None = typer.Option(
        None, "--token", help="Require 'Authorization: Bearer <token>' on /api (LAN case)."
    ),
    allow_host: list[str] = typer.Option(
        None,
        "--allow-host",
        help="Extra Host/Origin name to answer to (repeatable); '*' disables the check.",
    ),
    db_path: str | None = typer.Option(
        None, "--db", help=f"Database path for this server (overrides ${ENV_DB_VAR})."
    ),
) -> None:
    """Serve the human web UI and its JSON API (design §9).

    The inverse of ``mcp serve``: every write this surface makes is attributed
    to the ``human`` actor and no request field can change that. Binds loopback
    by default; ``--token`` adds a static bearer token on ``/api`` only
    (``/healthz`` and the UI stay open). Unbuilt frontend? The API still serves;
    the UI is a "run make web-build" placeholder.

    Two things this command refuses to do quietly. A non-loopback bind without
    ``--token`` is an unauthenticated write API on the network, so it exits 1
    rather than starting. And a loopback bind without ``--token`` is reachable
    by **every process on this machine**, including an MCP server launched with
    ``--actor agent:x``, which could then accept its own proposals over HTTP —
    so it says so at startup instead of leaving the operator to work it out.
    """
    import uvicorn

    from nodum import http_api

    if token is None and http_api.bare_host(host) not in http_api.LOOPBACK_BIND_ADDRESSES:
        typer.echo(
            f"refusing to bind {host} with no --token: that is an unauthenticated write API "
            "reachable from the network. Pass --token, or bind 127.0.0.1.",
            err=True,
        )
        raise typer.Exit(1)

    resolved_db = Path(db_path) if db_path is not None else db.db_path()
    typer.echo(f"nodum serve → http://{host}:{port}  (database: {resolved_db})", err=True)
    if token is None:
        typer.echo(
            "no --token: any process on this machine can drive this API as the 'human' actor, "
            "including local agents. Do not run it alongside agents you do not trust.",
            err=True,
        )
    else:
        typer.echo(f"open the UI at http://{host}:{port}/#token={token}", err=True)

    try:
        _run(
            uvicorn.run,
            http_api.create_app(
                db_path=db_path,
                token=token,
                allowed_hosts=http_api.resolve_allowed_hosts(host, allow_host),
            ),
            host=host,
            port=port,
        )
    except SystemExit as exc:
        # uvicorn catches a failed bind ("address already in use") itself, logs
        # it, and signals the caller with `sys.exit(STARTUP_FAILURE)` — a
        # non-zero code, but not the 1 this CLI's contract promises for every
        # error. Translate it rather than leaving one command exempt.
        if exc.code:
            typer.echo(f"could not serve on {host}:{port}", err=True)
            raise typer.Exit(1) from exc
        raise


# ── Review queue ──────────────────────────────────────────────────────────────

KIND_OPTION = typer.Option(None, "--kind", help="Limit to 'node', 'edge', or 'update' proposals.")
CREATED_BY_OPTION = typer.Option(None, "--created-by", help="Filter by proposing actor.")
CREATED_BEFORE_OPTION = typer.Option(
    None, "--created-before", help="Only proposals created before this timestamp."
)
CREATED_AFTER_OPTION = typer.Option(
    None, "--created-after", help="Only proposals created after this timestamp."
)
REVIEW_TYPE_OPTION = typer.Option(None, "--type", "-t", help="Filter by node/edge type.")


@review_app.command("queue")
def review_queue(
    created_by: str | None = CREATED_BY_OPTION,
    type: str | None = REVIEW_TYPE_OPTION,
    kind: str | None = KIND_OPTION,
    created_before: str | None = CREATED_BEFORE_OPTION,
    created_after: str | None = CREATED_AFTER_OPTION,
    limit: int = typer.Option(500, "--limit", help="Maximum proposals."),
    as_human: str = AS_OPTION,
) -> None:
    """List pending proposals (proposed nodes/edges/updates) with reviewer context."""
    proposals = _run(
        service.list_proposals,
        created_by=created_by,
        type=type,
        kind=kind,
        created_before=created_before,
        created_after=created_after,
        principal=_principal(as_human),
        limit=limit,
    )
    _emit_list("proposals", proposals)


@review_app.command("accept")
def review_accept(
    ids: list[str] = typer.Argument(..., help="Node, edge, or proposed-version ids to accept."),
    as_human: str = AS_OPTION,
) -> None:
    """Accept proposals by id (proposed → active); bad ids are reported, not fatal."""
    _emit(_run(service.accept_proposals, ids, principal=_principal(as_human)))


@review_app.command("reject")
def review_reject(
    ids: list[str] = typer.Argument(..., help="Node, edge, or proposed-version ids to reject."),
    reason: str = typer.Option(..., "--reason", help="Recorded in every reject event."),
    as_human: str = AS_OPTION,
) -> None:
    """Reject proposals by id (proposed → archived); bad ids are reported, not fatal."""
    _emit(_run(service.reject_proposals, ids, reason=reason, principal=_principal(as_human)))


@review_app.command("accept-all")
def review_accept_all(
    created_by: str | None = CREATED_BY_OPTION,
    type: str | None = REVIEW_TYPE_OPTION,
    kind: str | None = KIND_OPTION,
    created_before: str | None = CREATED_BEFORE_OPTION,
    created_after: str | None = CREATED_AFTER_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Accept every proposal matching the filters (e.g. one agent's whole run)."""
    _emit(
        _run(
            service.accept_matching,
            created_by=created_by,
            type=type,
            kind=kind,
            created_before=created_before,
            created_after=created_after,
            principal=_principal(as_human),
        )
    )


@review_app.command("reject-all")
def review_reject_all(
    reason: str = typer.Option(..., "--reason", help="Recorded in every reject event."),
    created_by: str | None = CREATED_BY_OPTION,
    type: str | None = REVIEW_TYPE_OPTION,
    kind: str | None = KIND_OPTION,
    created_before: str | None = CREATED_BEFORE_OPTION,
    created_after: str | None = CREATED_AFTER_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Reject every proposal matching the filters, recording the reason."""
    _emit(
        _run(
            service.reject_matching,
            reason=reason,
            created_by=created_by,
            type=type,
            kind=kind,
            created_before=created_before,
            created_after=created_after,
            principal=_principal(as_human),
        )
    )


# ── Accounts, grants, and spaces (Q13) ────────────────────────────────────────


@human_app.command("create")
def human_create(
    name: str = typer.Argument(..., help="Display name for the account."),
    as_human: str = AS_OPTION,
) -> None:
    """Create a human account (passwordless until `human passwd`)."""
    _emit(_run(service.create_human, name, principal=_principal(as_human)))


@human_app.command("list")
def human_list(as_human: str = AS_OPTION) -> None:
    """List human accounts."""
    _emit_list("humans", _run(service.list_humans, principal=_principal(as_human)))


@human_app.command("passwd")
def human_passwd(
    human_id: str = typer.Argument("owner", help="Account id (default: owner)."),
    password: str = typer.Option(
        ...,
        "--password",
        help="The new password (argon2id-hashed at rest).",
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Set or change a human's password (prompted, never echoed)."""
    _run(service.set_human_password, human_id, password, principal=_principal(as_human))
    _print_json({"ok": True, "human_id": human_id})


@human_app.command("disable")
def human_disable(
    human_id: str = typer.Argument(..., help="Account to disable."),
    as_human: str = AS_OPTION,
) -> None:
    """Disable a human (its sessions and its agents' tokens die; proposals stay)."""
    _run(service.disable_human, human_id, principal=_principal(as_human))
    _print_json({"ok": True, "human_id": human_id, "disabled": True})


@human_app.command("enable")
def human_enable(
    human_id: str = typer.Argument(..., help="Account to re-enable."),
    as_human: str = AS_OPTION,
) -> None:
    """Re-enable a disabled human."""
    _run(service.enable_human, human_id, principal=_principal(as_human))
    _print_json({"ok": True, "human_id": human_id, "disabled": False})


@agent_app.command("create")
def agent_create(
    name: str = typer.Argument(..., help="Agent id (becomes agent:<name>)."),
    kind: str = typer.Option("external", "--kind", help="'external' or 'internal'."),
    owner: str = typer.Option("owner", "--owner", help="Owning human (external agents)."),
    as_human: str = AS_OPTION,
) -> None:
    """Create an agent account and print its token — shown this once, only the hash is stored."""
    created = _run(
        service.create_agent,
        name,
        kind=kind,
        owner_human_id=None if kind == "internal" else owner,
        principal=_principal(as_human),
    )
    _emit(created)
    if created.token:
        typer.echo(
            f"token (shown once — store it now): {created.token}",
            err=True,
        )


@agent_app.command("list")
def agent_list(as_human: str = AS_OPTION) -> None:
    """List agent accounts."""
    _emit_list("agents", _run(service.list_agents, principal=_principal(as_human)))


@agent_app.command("token-rotate")
def agent_token_rotate(
    agent_id: str = typer.Argument(..., help="Agent whose token to replace."),
    as_human: str = AS_OPTION,
) -> None:
    """Replace an agent's token (the old one dies now; the new one shows once)."""
    token = _run(service.rotate_agent_token, agent_id, principal=_principal(as_human))
    typer.echo(f"token (shown once — store it now): {token}", err=True)
    _print_json({"ok": True, "agent_id": agent_id})


@agent_app.command("disable")
def agent_disable(
    agent_id: str = typer.Argument(..., help="Agent to disable."),
    as_human: str = AS_OPTION,
) -> None:
    """Disable an agent (its token dies immediately; its proposals stay, reviewable)."""
    _run(service.disable_agent, agent_id, principal=_principal(as_human))
    _print_json({"ok": True, "agent_id": agent_id, "disabled": True})


@agent_app.command("enable")
def agent_enable(
    agent_id: str = typer.Argument(..., help="Agent to re-enable."),
    as_human: str = AS_OPTION,
) -> None:
    """Re-enable a disabled agent."""
    _run(service.enable_agent, agent_id, principal=_principal(as_human))
    _print_json({"ok": True, "agent_id": agent_id, "disabled": False})


@app.command()
def grant(
    agent_id: str = typer.Argument(..., help="Agent to grant."),
    space: str = typer.Argument(..., help="Space id or name."),
    level: str = typer.Argument(..., help="'read', 'suggest', or 'edit'."),
    as_human: str = AS_OPTION,
) -> None:
    """Grant (or re-level) an agent's access to a space; event-logged."""
    _emit(_run(service.grant, agent_id, space, level, principal=_principal(as_human)))


@app.command()
def revoke(
    agent_id: str = typer.Argument(..., help="Agent to revoke from."),
    space: str = typer.Argument(..., help="Space id or name."),
    as_human: str = AS_OPTION,
) -> None:
    """Revoke an agent's grant on a space; event-logged."""
    _run(service.revoke, agent_id, space, principal=_principal(as_human))
    _print_json({"ok": True, "agent_id": agent_id, "space": space})


@app.command()
def grants(
    agent_id: str | None = typer.Option(None, "--agent", help="Limit to one agent."),
    as_human: str = AS_OPTION,
) -> None:
    """List grant rows."""
    _emit_list("grants", _run(service.list_grants, agent_id, principal=_principal(as_human)))


@app.command(name="space-create")
def space_create(
    name: str = typer.Argument(..., help="Title (and id suffix) for the new space."),
    as_human: str = AS_OPTION,
) -> None:
    """Create a space (a node of builtin type 'space' in meta; edit there is human-tier)."""
    node = _run(
        service.create_node, type="space", title=name, space="meta", principal=_principal(as_human)
    )
    _emit(node)


@app.command(name="space-list")
def space_list(as_human: str = AS_OPTION) -> None:
    """List spaces."""
    nodes = _run(
        service.list_nodes,
        type="space",
        state="active",
        include_meta=True,
        principal=_principal(as_human),
    )
    _emit_list("spaces", nodes)


@app.command(name="space-archive")
def space_archive(
    space_id: str = typer.Argument(..., help="Space node id to archive."),
    as_human: str = AS_OPTION,
) -> None:
    """Archive a space (nodes keep their space_id; grants on it go inert)."""
    _emit(_run(service.transition, space_id, "archive", principal=_principal(as_human)))
