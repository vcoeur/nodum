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
from importlib import resources
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


# ── Agent-ergonomics output shaping (presentation only, CLI-side) ─────────────

#: Default snippet length for search hits — the CLI returns a short snippet per
#: hit unless ``--fields full`` or an explicit ``--max-body-chars`` is given.
SNIPPET_CHARS = 200

#: Keys kept by ``--fields minimal`` on search hits (uuid, kind, title, rank).
_SEARCH_MINIMAL_KEYS = ("uuid", "kind", "title", "score")

#: Keys kept by ``--fields minimal`` on a ``get`` node payload.
_NODE_MINIMAL_KEYS = ("uuid", "kind", "title")


def _truncate_content(node: dict, max_chars: int) -> None:
    """Truncate a node payload's ``content`` in place, recording the cut.

    Adds the additive ``content_truncated`` / ``content_total_chars`` fields
    only when a cut actually happened, so a short payload is unchanged.
    """
    content = node.get("content")
    if isinstance(content, str) and len(content) > max_chars:
        node["content"] = content[:max_chars]
        node["content_truncated"] = True
        node["content_total_chars"] = len(content)


def _project(payload: dict, keys: tuple[str, ...]) -> dict:
    """Project a payload dict down to ``keys``, dropping everything else."""
    return {key: payload[key] for key in keys if key in payload}


def _check_fields_option(fields: str | None, allowed: tuple[str, ...]) -> None:
    """Reject an unknown ``--fields`` value."""
    if fields is not None and fields not in allowed:
        typer.echo(f"--fields must be one of {', '.join(allowed)}, got {fields!r}", err=True)
        raise typer.Exit(1)


def _read_batch_items(batch: Path) -> list:
    """Read a ``--batch`` JSON array from a file (``-`` reads stdin)."""
    raw = sys.stdin.read() if str(batch) == "-" else batch.read_text(encoding="utf-8")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"--batch input is not valid JSON: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not isinstance(items, list):
        typer.echo("--batch input must be a JSON array of items", err=True)
        raise typer.Exit(1)
    return items


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
    kind: str | None = typer.Argument(None, help="The node kind from the schema."),
    content: str | None = typer.Argument(
        None, help="The node's plain-text content (embeddable body)."
    ),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
    batch: Path | None = typer.Option(
        None,
        "--batch",
        help="Bulk create from a JSON array of {kind, content, data?} items ('-' reads stdin).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate every batch item without writing anything."
    ),
) -> None:
    """Create a typed node (or a batch of them) and print the result as JSON.

    Single form: ``add KIND CONTENT [--set k=v …]`` prints a NodeOut object.
    Batch form: ``add --batch FILE|-`` prints a BatchResult with one outcome
    per item — one bad item never aborts the rest; exit code is 1 when any
    item failed.
    """
    if batch is not None:
        if kind is not None or content is not None or set_:
            typer.echo("--batch is mutually exclusive with KIND, CONTENT and --set", err=True)
            raise typer.Exit(1)
        items = _read_batch_items(batch)
        with _service_errors():
            result = service.add_nodes_batch(items, dry_run=dry_run)
        _emit(result)
        if result.failed:
            raise typer.Exit(1)
        return
    if dry_run:
        typer.echo("--dry-run only applies to --batch", err=True)
        raise typer.Exit(1)
    if kind is None or content is None:
        typer.echo("missing KIND and CONTENT (or pass --batch FILE)", err=True)
        raise typer.Exit(1)
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
def get(
    uuids: list[str] = typer.Argument(..., help="One or more node UUIDs to fetch."),
    edge_kind: list[str] | None = typer.Option(
        None, "--edge-kind", help="Only include incident edges of these kinds (repeatable)."
    ),
    direction: str = typer.Option(
        "both", "--direction", help="Incident edges to include: in, out, or both (default)."
    ),
    fields: str | None = typer.Option(
        None, "--fields", help="Payload projection: minimal (uuid, kind, title) or full (default)."
    ),
    max_body_chars: int | None = typer.Option(
        None,
        "--max-body-chars",
        help="Truncate content to N characters; adds content_truncated / content_total_chars.",
    ),
) -> None:
    """Fetch node(s) and their incident edges, printed as JSON.

    One UUID prints the NodeWithEdges object (unchanged shape). Several UUIDs
    print ``{targets, nodes, failed}`` — a missing UUID lands in ``failed``
    without aborting the rest (exit 0 when at least one target resolved).
    """
    _check_fields_option(fields, ("minimal", "full"))

    def _shape(node_payload: dict) -> dict:
        if fields == "minimal":
            return _project(node_payload, _NODE_MINIMAL_KEYS)
        if max_body_chars is not None:
            _truncate_content(node_payload, max_body_chars)
        return node_payload

    with _service_errors():
        if len(uuids) == 1:
            result = service.get(uuids[0], edge_kinds=edge_kind or None, direction=direction)
            payload = result.model_dump(mode="json")
            payload["node"] = _shape(payload["node"])
            _print_json(payload)
            return
        result = service.get_many(uuids, edge_kinds=edge_kind or None, direction=direction)
    payload = result.model_dump(mode="json")
    for entry in payload["nodes"]:
        entry["node"] = _shape(entry["node"])
    _print_json(payload)
    if len(payload["failed"]) == len(uuids):
        raise typer.Exit(1)


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Free-text query (AND of terms)."),
    kind: str | None = typer.Option(None, "--kind", "-k", help="Optional node-kind filter."),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum number of hits."),
    tag: list[str] | None = typer.Option(
        None, "--tag", help="Require this tag (repeatable; AND semantics, matches data.tags)."
    ),
    fields: str | None = typer.Option(
        None,
        "--fields",
        help="Hit projection: minimal (uuid, kind, title, score) or full (untruncated content).",
    ),
    max_body_chars: int | None = typer.Option(
        None,
        "--max-body-chars",
        help="Truncate each hit's content to N characters (default: a 200-char snippet).",
    ),
) -> None:
    """Full-text search node text and print the ranked SearchResult JSON object.

    By default each hit's ``content`` is a short snippet (200 chars max, with
    ``content_truncated`` / ``content_total_chars`` added when cut); pass
    ``--fields full`` for the untruncated payload or ``--fields minimal`` for
    uuid/kind/title/score only.
    """
    _check_fields_option(fields, ("minimal", "full"))
    with _service_errors():
        result = service.search(query, kind=kind, limit=limit, tags=tag or None)
    payload = result.model_dump(mode="json")
    if fields == "minimal":
        payload["hits"] = [_project(hit, _SEARCH_MINIMAL_KEYS) for hit in payload["hits"]]
    else:
        budget = (
            max_body_chars
            if max_body_chars is not None
            else (None if fields == "full" else SNIPPET_CHARS)
        )
        if budget is not None:
            for hit in payload["hits"]:
                _truncate_content(hit, budget)
    _print_json(payload)


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
    uuid: str | None = typer.Argument(None, help="The node UUID to update."),
    content: str | None = typer.Option(None, "--content", help="Replacement node content."),
    set_: list[str] | None = typer.Option(
        None, "--set", help="Payload key=value (repeatable); value parsed as JSON, else raw string."
    ),
    batch: Path | None = typer.Option(
        None,
        "--batch",
        help="Bulk edit from a JSON array of {uuid, content?, data?} items ('-' reads stdin).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate every batch item without writing anything."
    ),
) -> None:
    """Merge new content/payload into node(s) and print the result as JSON.

    Single form: ``edit-node UUID [--content …] [--set k=v …]`` prints the
    updated NodeOut object. Batch form: ``edit-node --batch FILE|-`` prints a
    BatchResult with one outcome per item — one bad item never aborts the
    rest; exit code is 1 when any item failed.
    """
    if batch is not None:
        if uuid is not None or content is not None or set_:
            typer.echo("--batch is mutually exclusive with UUID, --content and --set", err=True)
            raise typer.Exit(1)
        items = _read_batch_items(batch)
        with _service_errors():
            result = service.update_nodes_batch(items, dry_run=dry_run)
        _emit(result)
        if result.failed:
            raise typer.Exit(1)
        return
    if dry_run:
        typer.echo("--dry-run only applies to --batch", err=True)
        raise typer.Exit(1)
    if uuid is None:
        typer.echo("missing UUID (or pass --batch FILE)", err=True)
        raise typer.Exit(1)
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


# ── Bundled agent skill ───────────────────────────────────────────────────────

skill_app = typer.Typer(
    no_args_is_help=True,
    help="Install the bundled nodum agent skill (SKILL.md) for LLM agent harnesses.",
)
app.add_typer(skill_app, name="skill")

# Default install targets, by scope.
USER_SKILL_DIR = Path.home() / ".config" / "agents" / "skills" / "nodum"
PROJECT_SKILL_DIR = Path(".agents") / "skills" / "nodum"


def _bundled_skill_text() -> str:
    """Read the SKILL.md shipped inside the package (``nodum/skill/SKILL.md``)."""
    return (resources.files("nodum") / "skill" / "SKILL.md").read_text(encoding="utf-8")


@skill_app.command("install")
def skill_install(
    user: bool = typer.Option(
        False, "--user", help="Install to ~/.config/agents/skills/nodum/ (default)."
    ),
    project: bool = typer.Option(
        False, "--project", help="Install to ./.agents/skills/nodum/ (project-local)."
    ),
    dest: Path | None = typer.Option(None, "--dest", help="Install to an explicit directory."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing SKILL.md."),
) -> None:
    """Copy the bundled SKILL.md into a skills directory and print a status object."""
    scopes = [name for name, flag in (("--user", user), ("--project", project)) if flag]
    if dest is not None and scopes:
        typer.echo(f"--dest is mutually exclusive with {scopes[0]}", err=True)
        raise typer.Exit(1)
    if dest is not None:
        target_dir = dest
    elif project:
        target_dir = Path.cwd() / PROJECT_SKILL_DIR
    else:
        target_dir = USER_SKILL_DIR
    target = target_dir / "SKILL.md"
    existed = target.exists()
    if existed and not force:
        typer.echo(f"{target} already exists — pass --force to overwrite", err=True)
        raise typer.Exit(1)
    text = _bundled_skill_text()
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _print_json(
        {"installed": str(target), "bytes": len(text.encode("utf-8")), "overwritten": existed}
    )


@skill_app.command("status")
def skill_status() -> None:
    """Report where the skill is installed and whether it matches the bundled copy."""
    bundled = _bundled_skill_text()
    targets = {
        "user": USER_SKILL_DIR / "SKILL.md",
        "project": Path.cwd() / PROJECT_SKILL_DIR / "SKILL.md",
    }
    rows = []
    for scope, path in targets.items():
        installed = path.exists()
        rows.append(
            {
                "scope": scope,
                "path": str(path),
                "installed": installed,
                "current": installed and path.read_text(encoding="utf-8") == bundled,
            }
        )
    _print_json({"bundled_bytes": len(bundled.encode("utf-8")), "targets": rows})


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
