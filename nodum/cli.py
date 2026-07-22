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
import sys

import typer
from pydantic import BaseModel

from nodum import projectors, service
from nodum import search as search_module
from nodum.db import ENV_DB_VAR
from nodum.service import (
    EdgeNotFound,
    EventNotFound,
    InvalidTransition,
    NodeNotFound,
    PolicyNotFound,
    TypeNotFound,
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
policy_app = typer.Typer(no_args_is_help=True, help="Per-agent policy rulesets (auto-accept).")
review_app = typer.Typer(no_args_is_help=True, help="The review queue: pending proposals.")
app.add_typer(node_app, name="node")
app.add_typer(edge_app, name="edge")
app.add_typer(projector_app, name="projector")
app.add_typer(policy_app, name="policy")
app.add_typer(review_app, name="review")


@app.callback()
def _main(
    db_path: str | None = typer.Option(
        None, "--db", help=f"Database path (overrides ${ENV_DB_VAR})."
    ),
) -> None:
    """Resolve the database path for this invocation."""
    if db_path is not None:
        os.environ[ENV_DB_VAR] = db_path


def _print_json(payload: dict) -> None:
    """Write a single JSON object to stdout (the only thing on the success path)."""
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _emit(result: BaseModel) -> None:
    """Print a pydantic result as the single JSON object on stdout."""
    _print_json(result.model_dump(mode="json"))


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
    """Call a service function, mapping expected errors to stderr + exit 1."""
    try:
        return func(*args, **kwargs)
    except (
        NodeNotFound,
        EdgeNotFound,
        TypeNotFound,
        EventNotFound,
        PolicyNotFound,
        InvalidTransition,
        ValueError,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


ACTOR_OPTION = typer.Option("human", "--actor", help="Write actor: 'human' or 'agent:<name>'.")
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
    actor: str = ACTOR_OPTION,
) -> None:
    """Create a node (active for actor 'human', proposed otherwise)."""
    node = _run(
        service.create_node,
        type=type,
        title=title,
        content=_read_content(content, content_file) or "",
        parent_id=parent,
        props=_parse_set(set_props),
        actor=actor,
    )
    _emit(node)


@node_app.command("get")
def node_get(node_id: str = typer.Argument(..., help="Node id.")) -> None:
    """Fetch one node by id."""
    _emit(_run(service.get_node, node_id))


@node_app.command("update")
def node_update(
    node_id: str = typer.Argument(..., help="Node id."),
    title: str | None = typer.Option(None, "--title", help="New title."),
    content: str | None = typer.Option(None, "--content", "-c", help="New Markdown body."),
    content_file: str | None = typer.Option(
        None, "--content-file", help="Read the new Markdown body from a file ('-' = stdin)."
    ),
    set_props: list[str] | None = SET_OPTION,
    actor: str = ACTOR_OPTION,
) -> None:
    """Update a node's title, content, and/or props (only given fields change)."""
    kwargs: dict = {"actor": actor}
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
) -> None:
    """List nodes in creation order, optionally filtered."""
    nodes = _run(service.list_nodes, type=type, state=state, parent_id=parent, limit=limit)
    _print_json({"nodes": [node.model_dump(mode="json") for node in nodes], "count": len(nodes)})


@node_app.command("children")
def node_children(node_id: str = typer.Argument(..., help="Parent node id.")) -> None:
    """List a node's children in position order."""
    nodes = _run(service.list_children, node_id)
    _print_json({"nodes": [node.model_dump(mode="json") for node in nodes], "count": len(nodes)})


@edge_app.command("create")
def edge_create(
    src_id: str = typer.Argument(..., help="Source node id."),
    dst_id: str = typer.Argument(..., help="Target node id."),
    type: str = typer.Option(..., "--type", "-t", help="Edge type id or name."),
    confidence: float | None = typer.Option(None, "--confidence", help="Confidence in [0, 1]."),
    set_props: list[str] | None = SET_OPTION,
    actor: str = ACTOR_OPTION,
) -> None:
    """Create a typed, directed edge between two nodes."""
    edge = _run(
        service.create_edge,
        src_id,
        dst_id,
        type,
        props=_parse_set(set_props),
        confidence=confidence,
        actor=actor,
    )
    _emit(edge)


@edge_app.command("list")
def edge_list(
    node: str | None = typer.Option(None, "--node", help="Filter by incident node id."),
    type: str | None = typer.Option(None, "--type", "-t", help="Filter by edge type."),
    state: str | None = typer.Option(None, "--state", help="Filter by state."),
    limit: int = typer.Option(500, "--limit", help="Maximum rows."),
) -> None:
    """List edges, optionally filtered by incident node, type, or state."""
    edges = _run(service.list_edges, node_id=node, type=type, state=state, limit=limit)
    _print_json({"edges": [edge.model_dump(mode="json") for edge in edges], "count": len(edges)})


@app.command()
def accept(
    record_id: str = typer.Argument(..., help="Node or edge id."),
    actor: str = ACTOR_OPTION,
) -> None:
    """Accept a proposed node or edge (proposed → active)."""
    _emit(_run(service.transition, record_id, "accept", actor=actor))


@app.command()
def reject(
    record_id: str = typer.Argument(..., help="Node or edge id."),
    actor: str = ACTOR_OPTION,
) -> None:
    """Reject a proposed node or edge (proposed → archived)."""
    _emit(_run(service.transition, record_id, "reject", actor=actor))


@app.command()
def archive(
    record_id: str = typer.Argument(..., help="Node or edge id."),
    actor: str = ACTOR_OPTION,
) -> None:
    """Archive an active node or edge (active → archived)."""
    _emit(_run(service.transition, record_id, "archive", actor=actor))


@app.command()
def undo(
    seq: int | None = typer.Argument(None, help="Event seq to reverse (default: latest)."),
    actor: str = ACTOR_OPTION,
) -> None:
    """Reverse an event, restoring the prior state from its payload."""
    _emit(_run(service.undo, seq, actor=actor))


@app.command()
def history(node_id: str = typer.Argument(..., help="Node id.")) -> None:
    """Show a node's version history (chronological)."""
    versions = _run(service.history, node_id)
    _print_json(
        {
            "versions": [version.model_dump(mode="json") for version in versions],
            "count": len(versions),
        }
    )


@app.command()
def events(limit: int = typer.Option(50, "--limit", help="Maximum rows.")) -> None:
    """Show the most recent event-log entries (newest first)."""
    rows = _run(service.list_events, limit=limit)
    _print_json({"events": [row.model_dump(mode="json") for row in rows], "count": len(rows)})


@app.command(name="types")
def list_types() -> None:
    """Show the full type catalog (node types and edge types)."""
    _emit(_run(service.list_types))


@app.command()
def search(
    query: str = typer.Argument(..., help="Free-text query; terms are ANDed."),
    k: int = typer.Option(10, "--k", help="Maximum hits."),
    state: str = typer.Option(
        "active", "--state", help="Node-state filter ('any' searches all states)."
    ),
    type: str | None = typer.Option(None, "--type", "-t", help="Filter by node type."),
) -> None:
    """Keyword-search node title + content (BM25-ranked, from the FTS index)."""
    result = _run(
        search_module.search, query, k=k, state=None if state == "any" else state, type=type
    )
    _emit(result)


@projector_app.command("run")
def projector_run(
    names: list[str] | None = typer.Argument(
        None, help="Projectors to run (default: all registered)."
    ),
) -> None:
    """Apply pending event-log entries to the derived indexes."""
    runs = _run(projectors.run_projectors, names=names)
    _print_json({"projectors": [run.model_dump(mode="json") for run in runs]})


@projector_app.command("rebuild")
def projector_rebuild(name: str = typer.Argument(..., help="Projector to rebuild.")) -> None:
    """Drop one projector's derived state and replay the full event log."""
    _emit(_run(projectors.rebuild_projector, name))


@projector_app.command("status")
def projector_status() -> None:
    """Show every projector's checkpoint, backlog, and derived-store size."""
    statuses = _run(projectors.projector_status)
    _print_json({"projectors": [status.model_dump(mode="json") for status in statuses]})


# ── Policies ──────────────────────────────────────────────────────────────────


@policy_app.command("set")
def policy_set(
    agent: str = typer.Argument(..., help="Actor the policy governs, e.g. 'agent:researcher'."),
    rule: list[str] = typer.Option(
        ...,
        "--rule",
        help="Repeatable JSON rule object, e.g. "
        '\'{"edge_type":"mentions","min_confidence":0.9,"action":"auto_accept"}\'.',
    ),
    actor: str = ACTOR_OPTION,
) -> None:
    """Create or replace an agent's policy ruleset (audited as policy.set)."""
    rules = []
    for raw in rule:
        try:
            rules.append(json.loads(raw))
        except json.JSONDecodeError:
            typer.echo(f"--rule expects a JSON object, got {raw!r}", err=True)
            raise typer.Exit(1) from None
    _emit(_run(service.set_policy, agent, rules, actor=actor))


@policy_app.command("get")
def policy_get(
    agent: str = typer.Argument(..., help="Actor string, e.g. 'agent:researcher'."),
) -> None:
    """Show one agent's policy."""
    _emit(_run(service.get_policy, agent))


@policy_app.command("list")
def policy_list() -> None:
    """List every stored policy."""
    policies = _run(service.list_policies)
    _print_json(
        {
            "policies": [policy.model_dump(mode="json") for policy in policies],
            "count": len(policies),
        }
    )


# ── Review queue ──────────────────────────────────────────────────────────────

KIND_OPTION = typer.Option(None, "--kind", help="Limit to 'node' or 'edge' proposals.")
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
) -> None:
    """List pending proposals (proposed nodes/edges) with reviewer context."""
    proposals = _run(
        service.list_proposals,
        created_by=created_by,
        type=type,
        kind=kind,
        created_before=created_before,
        created_after=created_after,
        limit=limit,
    )
    _print_json(
        {
            "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
            "count": len(proposals),
        }
    )


@review_app.command("accept")
def review_accept(
    ids: list[str] = typer.Argument(..., help="Node/edge ids to accept."),
    actor: str = ACTOR_OPTION,
) -> None:
    """Accept proposals by id (proposed → active); bad ids are reported, not fatal."""
    _emit(_run(service.accept_proposals, ids, actor=actor))


@review_app.command("reject")
def review_reject(
    ids: list[str] = typer.Argument(..., help="Node/edge ids to reject."),
    reason: str = typer.Option(..., "--reason", help="Recorded in every reject event."),
    actor: str = ACTOR_OPTION,
) -> None:
    """Reject proposals by id (proposed → archived); bad ids are reported, not fatal."""
    _emit(_run(service.reject_proposals, ids, reason=reason, actor=actor))


@review_app.command("accept-all")
def review_accept_all(
    created_by: str | None = CREATED_BY_OPTION,
    type: str | None = REVIEW_TYPE_OPTION,
    kind: str | None = KIND_OPTION,
    created_before: str | None = CREATED_BEFORE_OPTION,
    created_after: str | None = CREATED_AFTER_OPTION,
    actor: str = ACTOR_OPTION,
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
            actor=actor,
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
    actor: str = ACTOR_OPTION,
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
            actor=actor,
        )
    )
