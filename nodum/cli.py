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

from nodum import __version__, answers, assets, auth, db, extract, ingest, projectors, service, urls
from nodum import consolidate as consolidate_module
from nodum import search as search_module
from nodum.assets import AssetNotFound, AssetSourceChanged, AssetTooLarge, UnsupportedRendition
from nodum.cli_schema import build_cli_schema
from nodum.db import ENV_DB_VAR
from nodum.envelope import envelope, list_envelope, render_json
from nodum.models import ItemFailure, RollbackOut, TransitionFailure
from nodum.principal import Principal
from nodum.service import (
    EventNotFound,
    GrantNotPermitted,
    InvalidTransition,
    RecordNotFound,
    RollbackConflict,
    TypeNotFound,
    UndoNotPossible,
)
from nodum.vocab import (
    DIRECTIONS,
    GRANT_LEVEL_NAMES,
    PROPOSAL_KINDS,
    STATES,
    Direction,
    GrantLevel,
    NodeState,
    ProposalKind,
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
ingest_app = typer.Typer(
    no_args_is_help=True, help="Ingestion: files and URLs in, reviewable subgraphs out."
)
llm_app = typer.Typer(no_args_is_help=True, help="The language-model provider this install uses.")
app.add_typer(node_app, name="node")
app.add_typer(edge_app, name="edge")
app.add_typer(projector_app, name="projector")
app.add_typer(review_app, name="review")
app.add_typer(human_app, name="human")
app.add_typer(agent_app, name="agent")
app.add_typer(mcp_app, name="mcp")
app.add_typer(asset_app, name="asset")
app.add_typer(ingest_app, name="ingest")
app.add_typer(llm_app, name="llm")


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


def _emit_batch(result: BaseModel, failures: Sequence[TransitionFailure | ItemFailure]) -> None:
    """Print a batch result, name each per-item failure on stderr, exit 1 if any failed.

    ``ingest file``'s rule, and it is the same rule for the same reason. A batch
    never loses its successes, so the envelope is on stdout **before** the exit
    code is decided; and a run where something did not happen must not report
    success, so the exit code is 1 if any item failed. ``nodum retype main
    --type note`` accomplished nothing, said so in ``failed[]``, opened a cycle
    that closed ``completed`` with zero events — and exited 0, which is the one
    thing a script reads.

    The per-item reasons go to stderr as well as into the envelope, because an
    exit code of 1 with nothing on stderr breaks the other half of the contract:
    a failure is one readable line there.

    Args:
        result: The batch outcome to print as the command's one JSON object.
        failures: Its per-item failures — the ``failed`` list, or ``bulk_relink``'s
            ``skipped``. That list *is* the refusals now (a self-loop, a
            duplicate, a space the caller may not edit): its diff annotation
            ("nothing would change on this edge") moved to ``unchanged``, which
            is what let ``bulk-relink`` join this rule at all. Its **dry run**
            passes nothing here on purpose — a rehearsal's ``skipped`` is a
            prediction, nothing was attempted and nothing was lost, so exit 1
            would report a failure that has not happened. ``edge create-batch``
            is the one list keyed by input index rather than id — an
            :class:`ItemFailure` names ``index`` — and that index is what the
            stderr line names there.
    """
    _emit(result)
    for failure in failures:
        label = failure.id if isinstance(failure, TransitionFailure) else failure.index
        typer.echo(f"  failed {label}: {failure.error}", err=True)
    if failures:
        raise typer.Exit(1)


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
    """Resolve node content from ``--content`` or ``--content-file`` (``-`` = stdin).

    The file is read **through** :func:`_run`, not beside it, for the reason
    :func:`_principal` is: this helper is evaluated inside the argument list of
    the command's own ``_run(service.create_node, …)``, and Python builds that
    list before ``_run`` is entered — so ``--content-file /missing.md`` raised
    ``FileNotFoundError`` outside the error boundary and printed a full Rich
    traceback, against a contract whose named example of what must never be one
    is a missing file. Routing it here fixes ``node create`` and ``node update``
    at once.
    """
    if content is not None and content_file is not None:
        typer.echo("use either --content or --content-file, not both", err=True)
        raise typer.Exit(1)
    if content_file is not None:
        if content_file == "-":
            return sys.stdin.read()
        return _run(Path(content_file).read_text)
    return content


def _run(func, *args, **kwargs):
    """Call a service function, mapping expected errors to stderr + exit 1.

    Everything a caller can provoke — a bad id, a refused actor, a missing
    file, a database another writer is holding — is a message on stderr and
    exit 1, never a traceback: the CLI's contract is one JSON object on
    success and a readable line on failure.

    The except clauses are the literal list on purpose: ``test_http_api``
    AST-parses them to prove the HTTP surface maps every one of them, so
    hoisting them into a named tuple would silently retire that check.
    """
    try:
        return func(*args, **kwargs)
    except (
        # RecordNotFound covers node/edge/version ids and the transition
        # entry points that accept all three. `ingest.IngestError` and
        # `urls.TokenInvalid` are ValueError subclasses and so are already
        # here — naming them again would be noise, not coverage.
        RecordNotFound,
        TypeNotFound,
        EventNotFound,
        # `--as` naming an account that does not exist. It reaches this list
        # because `_principal` resolves *through* here rather than beside the
        # call it feeds — see that function for why the argument-list position
        # made it a traceback.
        auth.UnknownPrincipal,
        # A login name refused by the failed-login lockout (M5, 429 over
        # HTTP). No CLI command verifies a password today, so nothing here can
        # raise it yet — it is named so the surfaces' exception tables stay in
        # lockstep and a future command that does verify one inherits the
        # one-readable-line contract instead of a traceback.
        auth.LoginLocked,
        AssetNotFound,
        AssetTooLarge,
        AssetSourceChanged,
        UnsupportedRendition,
        InvalidTransition,
        GrantNotPermitted,
        UndoNotPossible,
        # A `UndoNotPossible` subclass, so already caught above; named because
        # the surfaces wave has to see that a refused rollback is one readable
        # line here and a 409 there, not a traceback.
        RollbackConflict,
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
    """Resolve ``--as`` to a human principal (trusted-local, no password).

    The resolution goes **through** :func:`_run` rather than beside it. Almost
    every command spells this as ``_run(service.fn, …, principal=_principal(…))``,
    where Python evaluates the argument list before ``_run`` is entered — so a
    refused actor raised from outside the error boundary and printed a full
    traceback, against the contract that says a failure is one readable line.
    Routing it here fixes every call site at once, wherever in an argument list
    it happens to sit, and Phase 5a adds several more of them.
    """
    human_id = as_human.removeprefix("human:")
    return _run(auth.owner_principal, human_id)


def _state_value(state: str | None) -> NodeState | None:
    """Narrow a ``--state`` string to the node-state vocabulary.

    Refused with the service's own sentence, so a bad value reads identically
    whether this helper or the service raises it. Callers route it through
    :func:`_run`, the same error boundary every service refusal goes through.
    """
    if state is not None and state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    return state


def _search_state_value(state: str) -> NodeState | None:
    """The ``search --state`` value: ``any`` translates to "no filter" here.

    ``any`` is this adapter's documented pseudo-value and is accepted exactly
    as before; every other value must be a real state.
    """
    if state == "any":
        return None
    return _state_value(state)


def _edge_states_value(edge_states: list[str] | None) -> list[NodeState] | None:
    """Narrow repeatable ``--edge-state`` values to the node-state vocabulary."""
    if edge_states is None:
        return None
    narrowed: list[NodeState] = []
    for edge_state in edge_states:
        if edge_state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {edge_state!r}")
        narrowed.append(edge_state)
    return narrowed


def _kind_value(kind: str | None) -> ProposalKind | None:
    """Narrow a ``--kind`` string to the proposal-kind vocabulary."""
    if kind is not None and kind not in PROPOSAL_KINDS:
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    return kind


def _direction_value(direction: str) -> Direction:
    """Narrow a ``--direction`` string to the traversal-direction vocabulary."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    return direction


def _level_value(level: str) -> GrantLevel:
    """Narrow a ``grant`` level argument to the grant-level vocabulary."""
    if level not in GRANT_LEVEL_NAMES:
        raise ValueError(f"level must be one of {GRANT_LEVEL_NAMES}, got {level!r}")
    return level


SET_OPTION = typer.Option(None, "--set", help="Repeatable key=value props (values parsed as JSON).")

#: The two read-side space controls, shared by `node list` and `search`. They
#: are independent of the write target (`--space` on `node create`): reading one
#: space while still filing into another is the ordinary case.
SPACE_FILTER_OPTION = typer.Option(
    None, "--space", help="Only this space (id or name); default: every space in scope."
)
INCLUDE_META_OPTION = typer.Option(
    False, "--include-meta", help="Include meta-space nodes (types, spaces) in an unnarrowed read."
)


@app.command()
def init() -> None:
    """Create the database (if needed) and apply pending migrations."""
    _emit(_run(service.init))


def _backup_to(destination: str, path: str | None) -> dict[str, str | int]:
    """Write a consistent snapshot of the graph to ``destination``.

    ``VACUUM INTO`` copies the source as one consistent snapshot, folding
    whatever committed rows still live in the ``-wal`` file into the new file —
    the rows a plain ``copyfile`` of the ``.db`` alone would silently lose
    while a connection is open. It refuses to run inside a transaction, so the
    source connection is a fresh ``db.connect`` with no DML before it: only
    DML opens an implicit DEFERRED transaction (``isolation_level`` is ``""``),
    never a bare ``SELECT`` or ``PRAGMA``.

    Raises:
        ValueError: If the source database does not exist, if the destination
            resolves to the source file itself, or if the destination already
            exists and is not empty.
    """
    source = db.db_path() if path is None else Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"no database at {source} — run 'nodum init' first")
    dest = Path(destination).expanduser()
    if source.resolve() == dest.resolve():
        raise ValueError(f"destination is the source database itself: {dest}")
    if dest.exists() and dest.stat().st_size > 0:
        raise ValueError(f"destination already exists and is not empty: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(source)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    with sqlite3.connect(str(dest)) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    return {"destination": str(dest), "bytes": dest.stat().st_size, "integrity": integrity}


@app.command("backup")
def backup(
    destination: str = typer.Argument(..., help="Path to write the backup database to."),
    path: str | None = typer.Option(
        None, "--path", help=f"Source graph path (defaults to ${ENV_DB_VAR})."
    ),
) -> None:
    """Write a consistent snapshot of the graph to another file (VACUUM INTO)."""
    _print_json(_run(_backup_to, destination, path))


@node_app.command("create")
def node_create(
    type: str = typer.Option(..., "--type", "-t", help="Node type id or name."),
    title: str | None = typer.Option(None, "--title", help="Display title (wikilink target)."),
    content: str | None = typer.Option(None, "--content", "-c", help="Markdown body."),
    content_file: str | None = typer.Option(
        None, "--content-file", help="Read the Markdown body from a file ('-' = stdin)."
    ),
    parent: str | None = typer.Option(None, "--parent", help="Parent node id."),
    space: str | None = typer.Option(
        None, "--space", help="Target space id or name (default: the 'main' space)."
    ),
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
        space=space,
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
    space: str | None = SPACE_FILTER_OPTION,
    include_meta: bool = INCLUDE_META_OPTION,
    limit: int = typer.Option(500, "--limit", help="Maximum rows."),
    as_human: str = AS_OPTION,
) -> None:
    """List nodes in creation order, optionally filtered."""
    nodes = _run(
        service.list_nodes,
        type=type,
        state=_run(_state_value, state),
        parent_id=parent,
        space=space,
        include_meta=include_meta,
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
    as_of: str | None = typer.Option(
        None, "--as-of", help="Read the edges true at this timestamp (validity window)."
    ),
    limit: int = typer.Option(500, "--limit", help="Maximum rows."),
    as_human: str = AS_OPTION,
) -> None:
    """List edges, optionally filtered by incident node, type, or state."""
    edges = _run(
        service.list_edges,
        node_id=node,
        type=type,
        state=_run(_state_value, state),
        as_of=as_of,
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
    """Propose a batch of edges; bad suggestions are reported, not fatal.

    The batch rule: the envelope is printed whatever happened, each refused
    suggestion is named on stderr by its input index, and the exit code is 1 if
    any suggestion failed — a run that wrote nothing must not report success to
    a script that only reads the code.
    """
    # Through `_run`, so a missing or unreadable file is the contract's one line
    # on stderr rather than a Rich traceback — the same reason `_read_content`
    # and `_principal` read through it.
    raw = sys.stdin.read() if suggestions_file == "-" else _run(Path(suggestions_file).read_text)
    try:
        suggestions = json.loads(raw)
    except json.JSONDecodeError:
        typer.echo("expected a JSON array of edge suggestions", err=True)
        raise typer.Exit(1) from None
    if not isinstance(suggestions, list):
        typer.echo("expected a JSON array of edge suggestions", err=True)
        raise typer.Exit(1)
    result = _run(service.propose_edges, suggestions, principal=_principal(as_human))
    _emit_batch(result, result.failed)


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
    cycle: str | None = typer.Option(
        None, "--cycle", help="Only the events one consolidation cycle produced."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Show the most recent event-log entries (newest first).

    `--cycle` is a journal entry's diff: a `cycles` row stores what each job
    examined and proposed, never a diff of its own, so what a cycle *changed* is
    read back off the append-only log it wrote — one record, not two that can
    disagree.
    """
    rows = _run(service.list_events, _principal(as_human), limit=limit, cycle_id=cycle)
    _emit_list("events", rows)


@app.command(name="types")
def list_types(as_human: str = AS_OPTION) -> None:
    """Show the full type catalog (node types and edge types)."""
    _emit(_run(service.list_types, principal=_principal(as_human)))


@app.command()
def search(
    query: str = typer.Argument(
        ...,
        help=(
            "Free-text query. Terms are ORed under a quorum: a node matches when the terms it "
            "carries are worth at least half the query's discriminating weight, so a question "
            "keeps working when the graph does not hold every one of its words."
        ),
    ),
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
    space: str | None = SPACE_FILTER_OPTION,
    include_meta: bool = INCLUDE_META_OPTION,
    expand: bool = typer.Option(
        False, "--expand", help="Append one-hop active-edge neighbors of the hits."
    ),
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help="With --expand, follow the edges true at this timestamp instead of the live graph.",
    ),
    nl: bool = typer.Option(
        False,
        "--nl",
        help="Let the model rewrite the question into search terms first (needs a provider).",
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Hybrid-search node title + content (BM25 + vector, RRF-fused).

    The vector signal participates when an embedding provider is available
    (fastembed installed and the model cached; otherwise search silently
    degrades to BM25). `signals` on each hit names what contributed.

    `--nl` layers a model-written query on top (design E3) and adds a `rewrite`
    object to the envelope saying what was asked on your behalf. It is a rewrite
    of the *words*, not of the retrieval: every signal, filter and cap below it
    is unchanged, and with no provider it is a no-op that says so and searches
    your own words.
    """
    shared = {
        "k": k,
        "state": _run(_search_state_value, state),
        "type": type,
        "created_by": created_by,
        "created_after": created_after,
        "created_before": created_before,
        "include_meta": include_meta,
        "space": space,
        "expand": expand,
        "as_of": as_of,
        "principal": _principal(as_human),
    }
    search_call = answers.natural_search if nl else search_module.search
    _emit(_run(search_call, query, **shared))


@app.command()
def ask(
    question: str = typer.Argument(..., help="What you want answered, in your own words."),
    k: int = typer.Option(
        answers.DEFAULT_ASK_K, "--k", help="Nodes to retrieve and put in front of the model."
    ),
    space: str | None = SPACE_FILTER_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Answer a question from the graph, with citations, or say it could not.

    One retrieval and one model call. **It writes nothing** (design E1), and
    `answered` is computed from citations that resolve to nodes you can read —
    never from the model's own claim to have answered, which was measured
    coming back `true` for a question its context could not answer.

    **`answered: true` means four deterministic checks held, not that the answer
    is true.** At least one citation resolves; the model did not also name a
    note that does not exist while offering only one that does; every number in
    the answer appears in the text that was really sent or in your question; and
    there is answer text. A model that invents content while citing a real node
    passes all four — so read the citations, and read `truncated_notes`, which
    names every note the model saw only part of.

    An unanswered question is an ordinary result and exit 0, not an error: the
    envelope carries `answered: false`, a `refusal` saying why, `unresolved`
    listing anything the model cited that does not exist,
    `unsupported_numbers` listing what the answer stated and the notes did not,
    and `used` saying what the attempt cost. `considered` is what reached the
    model and is empty when no call was made. With no provider configured the
    refusal names `NODUM_LLM_MODEL`.
    """
    _emit(
        _run(
            answers.ask,
            question,
            k=k,
            space=space,
            principal=_principal(as_human),
        )
    )


@app.command()
def summarize(
    node_id: str = typer.Argument(..., help="Node at the centre of the region to summarise."),
    depth: int = typer.Option(
        answers.DEFAULT_SUMMARY_DEPTH, "--depth", help="Hops of neighbourhood to include."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Summarise a node and its neighbourhood. Reads only.

    The subgraph is the bound, and it is read whether or not a provider is
    configured — so a node that does not resolve is the ordinary not-found
    refusal rather than a complaint about the model.

    **What is sent is narrower than what you can read.** Archived, proposed and
    meta-space nodes the walk returned are not put in front of the model — `ask`
    cannot reach them either — and they are named in `withheld`. Each note
    carries its `state`, `truncated_notes` names any the window narrowed, and
    `truncated` is still the separate fact that the *walk* stopped at its cap.

    Design E1 sketches an opt-in flag that files the summary as a reviewable
    `proposed` version. It is deliberately absent here: 5b-i is cut exactly at
    the line where a model call causes a write.
    """
    _emit(
        _run(
            answers.summarize,
            node_id,
            depth=depth,
            principal=_principal(as_human),
        )
    )


@llm_app.command("status")
def llm_status(
    probe: bool = typer.Option(
        True, "--probe/--no-probe", help="Make one small call to see whether the endpoint answers."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Say whether a model provider is configured, and whether it answers.

    **Two different facts, reported apart.** `configured` comes from the
    environment and costs nothing; `reachable` costs one small call, because
    that is the only thing that can answer it. `nodum.llm` deliberately makes no
    network call while resolving, so a server that is down at 03:00 and up at
    03:05 is not a configuration change — and `reachable` is therefore
    *tri-state*, and `null` is **not established** rather than "not asked":
    nothing is configured to ask, `--no-probe` declined it, or the probe was
    asked and no answer arrived inside `call_timeout`. That last one is
    deliberately not `false` — a refused connection is a server that is not
    running, and no answer yet is very often a live server loading a model.

    The probe is cheap in the case that matters: nothing listening is a refused
    connection in well under a millisecond, and a model the server does not have
    is an HTTP 404 in about one. It goes through the same runtime every other
    model call does, which is why it takes `--as`: a command that spent a call
    with nobody named would be the one unattributed spend in this system — and
    `used` says what it cost (34 tokens, measured), because a spend nobody can
    see is the same problem one step along. It waits the run's own
    `NODUM_LLM_CALL_TIMEOUT` rather than a ceiling of its own, so the sentence
    in `detail` and the number in `call_timeout` are the same number.

    Nothing here is an error. An install with no provider is a perfectly good
    install — the smart features are off — so this exits 0 and says so.
    """
    _emit(_run(answers.provider_status, principal=_principal(as_human), probe=probe))


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
            direction=_run(_direction_value, direction),
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
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help="Read the subgraph as it was true at this timestamp (validity window).",
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
            edge_states=_run(_edge_states_value, edge_state),
            min_confidence=min_confidence,
            created_by=created_by,
            node_types=node_type,
            as_of=as_of,
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


@projector_app.command("skips")
def projector_skips() -> None:
    """List events the projectors quarantined instead of applying."""
    _emit_list("skips", _run(projectors.list_skips))


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
    as_human: str = AS_OPTION,
) -> None:
    """Show one asset's metadata (never its bytes)."""
    _emit(_run(assets.get_asset, id_or_hash, principal=_principal(as_human)))


@asset_app.command("list")
def asset_list(as_human: str = AS_OPTION) -> None:
    """List every registered asset."""
    rows = _run(assets.list_assets, principal=_principal(as_human))
    _emit_list("assets", rows)


@asset_app.command("rendition")
def asset_rendition(
    id_or_hash: str = typer.Argument(..., help="Asset hash or asset-reference node id."),
    profile: str = typer.Option(
        "preview",
        "--profile",
        "-p",
        help="'thumb' or 'preview' for an image, or 'page:<n>' for a 1-based page of a PDF.",
    ),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Also write the WebP bytes to this path."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Fetch an image rendition, generating and caching it on first request.

    Prints the rendition metadata; image bytes stay in the database and are
    never inlined into the JSON output — use ``--out`` to extract them.

    ``page:<n>`` rasterises page *n* of a PDF and is an ordinary rendition
    otherwise: same lazy generation, same cache, same eviction by
    ``asset purge``. It needs the ``pdf`` extra; without it the request is
    refused by name rather than failing at import time.
    """
    rendition = _run(
        assets.get_rendition, id_or_hash, profile=profile, principal=_principal(as_human)
    )
    if out is not None:
        _run(assets.copy_rendition, rendition, out)
    _emit(rendition)


@asset_app.command("download-url")
def asset_download_url(
    id_or_hash: str = typer.Argument(..., help="Asset hash or asset-reference node id."),
    ttl: int = typer.Option(
        urls.DEFAULT_TTL_SECONDS,
        "--ttl",
        help=f"Lifetime in seconds (1 to {urls.MAX_TTL_SECONDS}).",
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Mint a short-lived, single-use URL for an asset's original bytes.

    The escape hatch for a host that shares no filesystem with the graph. The
    token is printed once and only its sha256 is stored, the URL is spent by
    the first request that redeems it, and both the mint and the redemption are
    event-logged. An asset this human cannot reach answers *not found* and
    nothing is minted.

    The URL points at ``nodum serve``, which has to be running for it to
    resolve; set ``NODUM_PUBLIC_URL`` when that server is not on the default
    address.
    """
    _emit(
        _run(
            urls.mint_download,
            id_or_hash,
            ttl_seconds=ttl,
            principal=_principal(as_human),
        )
    )


@asset_app.command("upload-url")
def asset_upload_url(
    name: str = typer.Option(..., "--name", help="Original name the bytes will arrive under."),
    mime: str = typer.Option(..., "--mime", help="Declared content type of the bytes."),
    size: int = typer.Option(
        ..., "--size", help=f"Declared size in bytes (at most {urls.MAX_UPLOAD_BYTES})."
    ),
    sha256: str | None = typer.Option(
        None, "--sha256", help="Declared content hash (lowercase hex) — enables the dedup skip."
    ),
    space: str | None = typer.Option(
        None, "--space", help="Space the describing node lands in (default: the 'main' space)."
    ),
    ttl: int = typer.Option(
        urls.DEFAULT_TTL_SECONDS,
        "--ttl",
        help=f"Lifetime in seconds (1 to {urls.MAX_TTL_SECONDS}).",
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Mint a short-lived, single-use URL to PUT one file to.

    A ``--sha256`` this graph already holds is answered with the existing
    ``asset`` and **no** ``grant`` — the bytes are here, so no bytes move.
    Exactly one of the two is ever filled. The grant's ``max_bytes`` is the
    ``--size`` declared here, and the upload route enforces it.
    """
    _emit(
        _run(
            urls.mint_upload,
            name,
            mime,
            size,
            sha256=sha256,
            space=space,
            ttl_seconds=ttl,
            principal=_principal(as_human),
        )
    )


@asset_app.command("purge")
def asset_purge(
    asset: str | None = typer.Option(
        None, "--asset", help="Limit the purge to one asset's renditions (hash)."
    ),
) -> None:
    """Evict stored renditions (regenerable — they rebuild on next request)."""
    _emit(_run(assets.purge_renditions, asset_hash=asset))


# ── Ingestion ─────────────────────────────────────────────────────────────────


def _expand_user(raw: str) -> Path:
    """Expand a leading ``~`` in a path argument, as a refusal rather than a crash.

    ``Path.expanduser`` raises ``RuntimeError`` for a ``~user`` it cannot
    resolve (``~nobodyhere/notes.md``), which is a typo in an argument and not a
    defect. It is translated to ``ValueError`` here — the one class :func:`_run`
    already maps to the contract's single line — rather than by adding
    ``RuntimeError`` to that except list, where it would launder every genuine
    bug in these commands into a friendly message about a path.
    """
    try:
        return Path(raw).expanduser()
    except RuntimeError as exc:
        raise ValueError(f"cannot resolve the home directory in path: {raw}") from exc


def _files_in(directory: Path, *, recursive: bool) -> list[Path]:
    """Return the regular files inside ``directory``, sorted, dotfiles skipped.

    Sorted so the same folder ingests in the same order twice, and dot-names
    are skipped whole: a folder of PDFs is not an invitation to ingest its
    ``.DS_Store`` or to walk into its ``.git``.
    """
    found: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if recursive:
                found.extend(_files_in(entry, recursive=True))
        elif entry.is_file():
            found.append(entry)
    return found


def _ingest_sources(paths: Sequence[str], *, recursive: bool) -> list[Path]:
    """Expand the path arguments into the files a batch will ingest, in order.

    Called **through** :func:`_run`, for the reason :func:`_principal` and
    :func:`_read_content` are: it touches the filesystem, and an unreadable
    directory's ``PermissionError`` climbing out of ``iterdir`` here was a full
    Rich traceback from outside the error boundary.
    """
    found: list[Path] = []
    for raw in paths:
        candidate = _expand_user(raw)
        if candidate.is_dir():
            found.extend(_files_in(candidate, recursive=recursive))
        else:
            # Not a directory: pass it through as given, so a missing or
            # unreadable path reports itself in `ingest_file`'s own words
            # rather than being silently dropped by this expansion.
            found.append(candidate)
    return found


@ingest_app.command("file")
def ingest_file(
    paths: list[str] = typer.Argument(
        ..., help="Files to ingest; a directory ingests the files directly inside it."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Original name to record (default: the file's own) — one file only."
    ),
    space: str | None = typer.Option(
        None, "--space", help="Target space id or name (default: the 'main' space)."
    ),
    title: str | None = typer.Option(
        None, "--title", help="Title for the source node (default: --name) — one file only."
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r", help="Descend into subdirectories of a directory argument."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Ingest local files: register the bytes, extract text, describe, propose.

    One path naming a file prints that ingestion as a single JSON object.
    Anything else — several paths, or a directory — is a batch and prints
    `{"ingestions": [...], "count": n}`; `--name` and `--title` describe one
    document and are refused there. A directory contributes the files directly
    inside it and `--recursive` the ones below it too, skipping dot-names and
    anything that is not a regular file, in sorted order.

    A batch never loses its successes: each file is ingested on its own, a file
    that fails prints its reason and then `  skipped <path>` on stderr and the
    batch carries on, and every file that landed is in the envelope on stdout.
    **The exit code is 1 if any file failed** — so a non-zero exit here means
    "read stderr for what is missing", not "nothing happened". Re-running is
    safe: ingestion is idempotent, so the files that already landed are found
    rather than duplicated.
    """
    principal = _principal(as_human)
    if len(paths) == 1 and not _run(_expand_user, paths[0]).is_dir():
        _emit(
            _run(
                ingest.ingest_file,
                paths[0],
                name=name,
                space=space,
                title=title,
                principal=principal,
            )
        )
        return

    if name is not None or title is not None:
        typer.echo(
            "--name and --title describe one document; drop them to ingest several files",
            err=True,
        )
        raise typer.Exit(1)
    sources = _run(_ingest_sources, paths, recursive=recursive)
    if not sources:
        typer.echo(f"no files to ingest in {', '.join(paths)}", err=True)
        raise typer.Exit(1)

    ingested: list = []
    failures = 0
    for source in sources:
        try:
            ingested.append(_run(ingest.ingest_file, source, space=space, principal=principal))
        except typer.Exit:
            # Reported through `_run`, so a batch and a single-file run explain
            # a failure in identical words; a batch then names the file the
            # explanation belongs to and carries on, since "not a file" alone
            # does not say which of twenty it was.
            failures += 1
            typer.echo(f"  skipped {source}", err=True)
    # Printed before the exit code is decided: the successes are the point of
    # not aborting, and a caller must be able to read them off stdout whether
    # or not a sibling file failed.
    _emit_list("ingestions", ingested)
    if failures:
        raise typer.Exit(1)


@ingest_app.command("url")
def ingest_url(
    url: str = typer.Argument(..., help="An http or https URL to fetch and ingest."),
    name: str | None = typer.Option(
        None, "--name", help="Original name to record (default: the URL's own filename)."
    ),
    space: str | None = typer.Option(
        None, "--space", help="Target space id or name (default: the 'main' space)."
    ),
    title: str | None = typer.Option(None, "--title", help="Title for the source node."),
    as_human: str = AS_OPTION,
) -> None:
    """Fetch a URL into the blob store and ingest it exactly like a local file.

    `http` and `https` only, one bounded read with a timeout, and a redirect
    that leaves those two schemes is refused. The URL is recorded on both
    written nodes as provenance. Loopback and private addresses are *not*
    blocked — this is itself a loopback service — so granting ingestion grants
    the server's network position.
    """
    _emit(
        _run(
            ingest.ingest_url,
            url,
            name=name,
            space=space,
            title=title,
            principal=_principal(as_human),
        )
    )


@ingest_app.command("handlers")
def ingest_handlers() -> None:
    """List every extraction handler, its MIME families, and whether it can run.

    This is where "my PDF produced no text" gets its answer: a handler whose
    optional dependency is absent reports `available: false` with a `detail`
    naming the extra to install. No `--as` — availability is a property of this
    install, not of the graph.
    """
    _emit_list("handlers", _run(extract.availability))


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
    to the human behind an authenticated session, and no request field can
    change that. Auth is password login (``POST /api/login``) backed by
    server-side sessions; ``/healthz`` and the static UI stay open. Unbuilt
    frontend? The API still serves; the UI is a "run make web-build"
    placeholder.

    A non-loopback bind is allowed — login is the boundary, not the bind —
    and the session cookie gains ``Secure`` there. Either way, any process
    that can reach the port may *attempt* a login — throttled only by the
    failed-login lockout, five misses per name per quarter-hour then a 429
    until the window slides past them — so the human's password is still the
    heart of the defence; the banner says so rather than leaving it implicit.
    """
    import uvicorn

    from nodum import http_api, scheduler

    resolved_db = Path(db_path) if db_path is not None else db.db_path()
    loopback = http_api.bare_host(host) in http_api.LOOPBACK_BIND_ADDRESSES
    typer.echo(f"nodum serve → http://{host}:{port}  (database: {resolved_db})", err=True)
    typer.echo(
        "auth: password login — any process that can reach this port may attempt one, "
        "so every /api request still needs a human password (nodum human passwd).",
        err=True,
    )
    try:
        consolidate_at = scheduler.configured_time()
    except ValueError:
        # An unparseable value is announced by `create_app`, which is also what
        # decides to start without a schedule. Saying it twice, in two voices,
        # is worse than saying it once where the decision is made.
        consolidate_at = None
    if consolidate_at is not None:
        # The *successful* configuration is the one that matters more, and it
        # was the silent one: a typo got a warning while the setting that
        # actually starts a background writer on the human's graph produced
        # nothing at all on the console.
        typer.echo(
            f"nightly consolidation cycle: {consolidate_at.strftime('%H:%M')} local time "
            f"(${scheduler.ENV_CONSOLIDATE_AT}) — the gardener will write to this graph "
            "unasked; unset it to turn the schedule off.",
            err=True,
        )
    if not loopback:
        # uvicorn serves plain HTTP. The session cookie is marked Secure on a
        # non-loopback bind and so fails closed without TLS, but the login body
        # itself has already crossed the network by then (Q13 review S12).
        typer.echo(
            f"warning: {host} is not loopback and this server speaks plain HTTP — "
            "passwords cross the network in the clear unless a TLS proxy fronts it.",
            err=True,
        )

    try:
        _run(
            uvicorn.run,
            http_api.create_app(
                db_path=db_path,
                allowed_hosts=http_api.resolve_allowed_hosts(host, allow_host),
                # Loopback is plain HTTP, where a Secure cookie would never be
                # stored; a LAN bind fronts TLS, where it must be.
                secure_cookies=not loopback,
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
        kind=_run(_kind_value, kind),
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
    """Accept proposals by id (proposed → active); bad ids are reported, not fatal.

    The batch rule: the envelope is printed whatever happened, each refused id
    is named on stderr, and the exit code is 1 if any id was skipped — a run
    that accomplished nothing must not report success to a script that only
    reads the code.
    """
    result = _run(service.accept_proposals, ids, principal=_principal(as_human))
    _emit_batch(result, result.failed)


@review_app.command("reject")
def review_reject(
    ids: list[str] = typer.Argument(..., help="Node, edge, or proposed-version ids to reject."),
    reason: str = typer.Option(..., "--reason", help="Recorded in every reject event."),
    as_human: str = AS_OPTION,
) -> None:
    """Reject proposals by id (proposed → archived); bad ids are reported, not fatal.

    The reason is recorded in every reject event's payload. The batch rule: the
    envelope is printed whatever happened, each refused id is named on stderr,
    and the exit code is 1 if any id was skipped.
    """
    result = _run(service.reject_proposals, ids, reason=reason, principal=_principal(as_human))
    _emit_batch(result, result.failed)


@review_app.command("accept-all")
def review_accept_all(
    created_by: str | None = CREATED_BY_OPTION,
    type: str | None = REVIEW_TYPE_OPTION,
    kind: str | None = KIND_OPTION,
    created_before: str | None = CREATED_BEFORE_OPTION,
    created_after: str | None = CREATED_AFTER_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Accept every proposal matching the filters (e.g. one agent's whole run).

    Each match transitions with its own event. The batch rule: the envelope is
    printed whatever happened, each refused id is named on stderr, and the exit
    code is 1 if any proposal was skipped.
    """
    result = _run(
        service.accept_matching,
        created_by=created_by,
        type=type,
        kind=_run(_kind_value, kind),
        created_before=created_before,
        created_after=created_after,
        principal=_principal(as_human),
    )
    _emit_batch(result, result.failed)


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
    """Reject every proposal matching the filters, recording the reason.

    Each match transitions with its own event carrying the reason. The batch
    rule: the envelope is printed whatever happened, each refused id is named
    on stderr, and the exit code is 1 if any proposal was skipped.
    """
    result = _run(
        service.reject_matching,
        reason=reason,
        created_by=created_by,
        type=type,
        kind=_run(_kind_value, kind),
        created_before=created_before,
        created_after=created_after,
        principal=_principal(as_human),
    )
    _emit_batch(result, result.failed)


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
    owner: str = typer.Option("owner", "--owner", help="Owning human."),
    as_human: str = AS_OPTION,
) -> None:
    """Create an agent account and print its token — shown this once, only the hash is stored.

    Every account created here is **external**, and there is no flag for the
    other kind. `service.create_agent` refuses `kind="internal"` outright:
    `auth.internal_principal` selects the gardener by being the only row with
    that kind and refuses to choose between two, so a second internal agent does
    not add a gardener — it takes the existing one away and every consolidation
    path dies. `disable_agent` is no cure (the count precedes the `disabled`
    check) and no surface deletes an agent, so the install was recoverable only
    by hand-editing the database. A flag whose one non-default value is a
    permanent refusal is not a choice; HTTP already hardcodes `external`.
    """
    created = _run(
        service.create_agent,
        name,
        owner_human_id=owner,
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
    """Disable an agent (its token stops verifying; its proposals stay, reviewable)."""
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
    _emit(
        _run(
            service.grant,
            agent_id,
            space,
            _run(_level_value, level),
            principal=_principal(as_human),
        )
    )


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
    _emit(_run(service.create_space, name, principal=_principal(as_human)))


@app.command(name="space-list")
def space_list(as_human: str = AS_OPTION) -> None:
    """List active spaces with their live node counts and the agents granted on them."""
    _emit_list("spaces", _run(service.list_spaces, principal=_principal(as_human)))


@app.command(name="space-rename")
def space_rename(
    space: str = typer.Argument(..., help="Space id or name to rename."),
    name: str = typer.Argument(..., help="New title for the space."),
    as_human: str = AS_OPTION,
) -> None:
    """Rename a space (a space is a node, so this is a node-title update)."""
    _emit(_run(service.rename_space, space, name, principal=_principal(as_human)))


@app.command(name="space-archive")
def space_archive(
    space: str = typer.Argument(..., help="Space id or name to archive."),
    as_human: str = AS_OPTION,
) -> None:
    """Archive a space (nodes keep their space_id; every grant on it goes inert).

    The grant rows survive, so `grant-list` still shows them and `grant-revoke`
    still reaches them by the space's id or name — they simply confer nothing
    while the space is archived, and come back if the archive is undone.
    """
    _emit(_run(service.archive_space, space, principal=_principal(as_human)))


# ── Consolidation cycles, the journal, and the curative tier (§8.2/§8.4) ──────
#
# Flat commands rather than a group, the shape the spaces phase settled on
# (`space-create`, `space-list`, …): each of these is one verb over one noun,
# and `rollback` in particular belongs beside `undo`, which is its sibling —
# an event with no cycle id is reversed by `undo`, an event with one by
# `rollback`.


@app.command()
def consolidate(
    scope: str | None = typer.Option(
        None, "--scope", help="Confine the cycle to one space (id or name); default: every space."
    ),
    job: list[str] | None = typer.Option(
        None,
        "--job",
        help="Job to run (repeatable, each name at most once; default: all jobs, in order).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute every job and write nothing but the journal entry."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Run a consolidation cycle: the gardener's deterministic jobs, and its report.

    The writes are the gardener's (`agent:builtin-gardener`), because the
    gardener made them; `--as` names who *asked*, which is what the cycle
    records as `triggered_by`. `--dry-run` computes everything and emits **no**
    event at all — the cycle row is still written and flagged, because the
    journal has to say which it was, and `events --cycle <id>` on it is empty.

    What the run changed is not in the report: it is `events --cycle <id>`.
    """
    _emit(
        _run(
            consolidate_module.consolidate,
            scope=scope,
            dry_run=dry_run,
            jobs=job or None,
            triggered_by=_principal(as_human).actor_string,
        )
    )


@app.command(name="cycle-list")
def cycle_list(
    limit: int = typer.Option(50, "--limit", help="Maximum cycles."),
    as_human: str = AS_OPTION,
) -> None:
    """List consolidation cycles, newest first — the dream journal (human-only)."""
    _emit_list("cycles", _run(service.list_cycles, limit=limit, principal=_principal(as_human)))


@app.command(name="cycle-get")
def cycle_get(
    cycle_id: str = typer.Argument(..., help="Cycle id."),
    as_human: str = AS_OPTION,
) -> None:
    """Show one cycle's journal entry: what ran, what it measured, how it ended.

    Human-only, for the reason `events` is: the journal says what the gardener
    did across every space in the file.
    """
    _emit(_run(service.get_cycle, cycle_id, principal=_principal(as_human)))


@app.command(name="cycle-abandon")
def cycle_abandon(
    cycle_id: str = typer.Argument(..., help="The interrupted cycle to close as failed."),
    as_human: str = AS_OPTION,
) -> None:
    """Close an interrupted cycle nobody is going to finish — the door out (human-only).

    A cycle left `running` by a `SIGKILL`, a power cut, or a server shutdown
    that cancelled the nightly task is not a cosmetic wart in the journal: it
    makes its own writes **irreversible on every surface**. `rollback` refuses a
    cycle that is still running, because its event set is not closed yet, and
    `undo` refuses every event a cycle stamped by name. This is what closes it,
    as `failed`, with a report saying who abandoned it — after which
    `rollback <cycle-id>` works normally.

    It is not a general "close this cycle": a cycle that already said how it
    ended is refused, since re-closing it would overwrite that record. Nothing
    the run already wrote is touched — those writes are real, and taking them
    back is exactly what this unlocks.
    """
    _emit(_run(service.abandon_cycle, cycle_id, principal=_principal(as_human)))


@app.command(name="cycle-stop")
def cycle_stop(
    cycle_id: str = typer.Argument(..., help="The running cycle to ask to stop."),
    as_human: str = AS_OPTION,
) -> None:
    """Ask a running cycle to stop, and record who asked — the kill switch (human-only).

    It stamps the cycle with who asked and when, and does nothing else: the entry
    stays `running`, no event is emitted, and nothing the run already wrote is
    touched. The run notices at its next check — between jobs, between items, or
    immediately before a model call — and closes its own entry `failed`, so the
    journal says the operator stopped that night rather than that a process died.

    **Not `cycle-abandon`.** That is a repair: a human closing a dead process's
    entry from *outside*, which is what makes its writes reversible. This is an
    instruction to a run that is still alive and expected to obey it. A `failed`
    entry read the next morning has to say which of the two happened, so the two
    verbs stay apart.

    **Not `rollback` either.** Stopping reverses nothing: every write the run made
    stays in the graph, stamped with the cycle, and `rollback <cycle-id>` is what
    takes those back once the entry has closed. Stopping and undoing are two
    decisions, and a switch that also reverted would make "stop, look at what it
    did, then decide" impossible — which is the reason a human hits one.

    Asking twice is a no-op that keeps the first asker. A cycle that has already
    said how it ended is refused: there is nothing left to obey the instruction,
    and the stamp would name a run that never saw it.

    **What obeys it today** is the model-calling runtime (`nodum.agent`), which
    checks the switch before every provider call. The four deterministic
    consolidation jobs make no model call and no such check, so a stop recorded
    against one is kept in the journal and that run finishes on its own —
    `cycle-abandon` is the verb for a run that will never finish at all.
    """
    _emit(_run(service.request_stop, cycle_id, principal=_principal(as_human)))


def _rollback(cycle_id: str, *, dry_run: bool, principal: Principal) -> RollbackOut:
    """Roll a cycle back, rendering a refusal as JSON rather than as a sentence.

    Every other failure in this CLI is one line, because one line is all there
    is to say. A refused rollback is a *list*: for each row in the way, which
    event of the cycle wrote it and which later event moved it, plus that
    event's actor and cycle. `RollbackConflict`'s message names only the first
    few and drops the actor and the cycle entirely, so the structured list is
    printed as the command's one JSON object and the message still goes to
    stderr with exit 1, exactly as every other refusal does.
    """
    try:
        return service.rollback_cycle(cycle_id, dry_run=dry_run, principal=principal)
    except RollbackConflict as exc:
        _print_json(
            {
                "error": {
                    "type": "RollbackConflict",
                    "message": str(exc),
                    "conflicts": [conflict.model_dump(mode="json") for conflict in exc.conflicts],
                }
            }
        )
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def rollback(
    cycle_id: str = typer.Argument(..., help="Consolidation cycle to take back."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be reversed, and what stands in the way."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Take a consolidation cycle back whole — all of it, or none of it (D7).

    The sibling of `undo`, and the split is one line: an event with no cycle id
    is reversed by `undo`, an event with one is reversed here. Human-only, for a
    stronger version of `undo`'s own reason — it writes recorded payloads back
    verbatim, across spaces, for a whole cycle at once.

    It **refuses rather than clobbers**: if anything outside the cycle has
    touched a row the cycle wrote, nothing is written and the refusal is this
    command's one JSON object — `{"error": {"type", "message", "conflicts"}}`,
    each conflict naming both ends of the collision — with the message on
    stderr and exit 1. `--dry-run` asks the same question without the refusal:
    it opens no cycle, writes nothing, and reports the conflicts in `conflicts`.
    """
    _emit(_run(_rollback, cycle_id, dry_run=dry_run, principal=_principal(as_human)))


@app.command(name="merge-nodes")
def merge_nodes(
    ids: list[str] = typer.Argument(..., help="Nodes to merge away."),
    into: str = typer.Option(..., "--into", help="The survivor's node id."),
    as_human: str = AS_OPTION,
) -> None:
    """Merge nodes into a survivor — soft, reversible, nothing destroyed (D9).

    Each merged-away node is archived and says where it went in
    `props.merged_into`; every incident edge is repointed at the survivor,
    keeping its original endpoints, or archived when repointing would make a
    self-loop or duplicate an edge the survivor already carries. Reads are
    unchanged: `node get` on a tombstone returns the tombstone.

    The whole merge lands in one cycle, and `rollback <cycle-id>` is what takes
    it back — `undo` refuses a cycle-stamped event by name, because reversing
    one row of a merge would leave the other half standing.
    """
    _emit(_run(service.merge_nodes, ids, into=into, principal=_principal(as_human)))


@app.command()
def retype(
    ids: list[str] = typer.Argument(..., help="Nodes to retype."),
    type: str = typer.Option(..., "--type", "-t", help="Target node type id or name."),
    as_human: str = AS_OPTION,
) -> None:
    """Change nodes' type — the one sanctioned exception to an immutable field (§8.2).

    A node's type is fixed at creation *by design*, which is why this is a
    curative operation rather than a field on `node update`. No props are
    transformed: what a property means after a retype is a judgement call, and
    this tier is the deterministic one. Per-item failures are reported rather
    than fatal, exactly as a batch accept/reject reports them — in `failed`, on
    stderr, and in the **exit code**, which is 1 if any node was skipped. That
    is `ingest file`'s rule: a batch never loses its successes, so the envelope
    is printed either way, but a run that accomplished nothing must not report
    success to a script that only reads the code.
    """
    retyped = _run(service.retype, ids, type, principal=_principal(as_human))
    _emit_batch(retyped, retyped.failed)


@app.command(name="supersede-edge")
def supersede_edge(
    edge_id: str = typer.Argument(..., help="The edge to retire."),
    src: str | None = typer.Option(None, "--src", help="Replacement's source node id."),
    dst: str | None = typer.Option(None, "--dst", help="Replacement's target node id."),
    type: str | None = typer.Option(None, "--type", "-t", help="Replacement's edge type."),
    confidence: float | None = typer.Option(
        None, "--confidence", help="Replacement's confidence in [0, 1]."
    ),
    set_props: list[str] | None = SET_OPTION,
    as_human: str = AS_OPTION,
) -> None:
    """Retire an edge that stopped being true, optionally naming its successor.

    Two facts are recorded because they are two facts: `valid_to` is closed
    (*when* it stopped being true) and the edge is archived (*it is no longer
    part of the live graph*).

    Every option here describes the **replacement**, and every field it does not
    name is inherited from the edge being replaced — so a supersede that only
    changes a confidence says only that, and naming none of them retires the
    edge with no successor at all.
    """
    replacement: dict = {}
    if src is not None:
        replacement["src_id"] = src
    if dst is not None:
        replacement["dst_id"] = dst
    if type is not None:
        replacement["type"] = type
    if confidence is not None:
        replacement["confidence"] = confidence
    if set_props:
        replacement["props"] = _parse_set(set_props)
    _emit(
        _run(
            service.supersede_edge,
            edge_id,
            replacement=replacement or None,
            principal=_principal(as_human),
        )
    )


@app.command(name="bulk-relink")
def bulk_relink(
    src: str | None = typer.Option(None, "--src", help="Select edges out of this node."),
    dst: str | None = typer.Option(None, "--dst", help="Select edges pointing at this node."),
    type: str | None = typer.Option(None, "--type", "-t", help="Select edges of this type."),
    state: str | None = typer.Option(
        None, "--state", help="Select edges in this state (default: everything but archived)."
    ),
    to_type: str | None = typer.Option(None, "--to-type", help="New edge type."),
    to_dst: str | None = typer.Option(None, "--to-dst", help="New target node id."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the diff and write nothing at all."
    ),
    as_human: str = AS_OPTION,
) -> None:
    """Repoint or retype many edges at once, behind a reviewable dry run (§8.5).

    The four selector options narrow; the two `--to-*` options say what changes.
    Neither may be empty — an empty selector would match every edge in the file
    — and the service refuses that rather than this adapter guessing at it.

    `--dry-run` opens no cycle and emits no event: it is the diff §8.5 asks for
    on a large refactor. The reversal of a run that did happen is
    `rollback <cycle-id>`.

    The batch rule applies, with one departure. `skipped[]` is the refusals — a
    self-loop, a duplicate, a space you may not edit — so a real run that
    refused an edge exits **1** and names each one on stderr, exactly as
    `retype` does; `unchanged[]` is the diff annotation and never affects the
    code. A `--dry-run` exits **0** whatever it predicts: nothing was attempted
    there, so nothing failed.
    """
    selector: dict = {}
    if src is not None:
        selector["src_id"] = src
    if dst is not None:
        selector["dst_id"] = dst
    if type is not None:
        selector["type"] = type
    if state is not None:
        selector["state"] = state
    changes: dict = {}
    if to_type is not None:
        changes["type"] = to_type
    if to_dst is not None:
        changes["dst_id"] = to_dst
    result = _run(
        service.bulk_relink,
        selector,
        changes,
        dry_run=dry_run,
        principal=_principal(as_human),
    )
    # The dry run's refusals are a prediction, not a loss, so they buy no exit
    # code — read off the result rather than the flag, since the service is what
    # decides which posture the run had.
    _emit_batch(result, [] if result.dry_run else result.skipped)
