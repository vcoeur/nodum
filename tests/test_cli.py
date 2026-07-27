"""CLI smoke tests: every command emits one parseable JSON object on stdout."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from helpers import agent, seed_space
from typer.testing import CliRunner

from nodum import db, extract, service
from nodum.cli import app

runner = CliRunner()

#: The committed two-page PDF `tests/test_ingest.py` ingests end to end; here it
#: is the only asset a `page:<n>` rendition can be asked of.
FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample.pdf"


#: Commands that never touch a principal: init, schema-dump, --version,
#: projector/asset plumbing, and the two server launches. Everything else
#: requires an explicit `--as` — reads included, since reads take a principal.
NO_AS_GROUPS = {"init", "schema-dump", "projector", "asset", "mcp", "serve"}

#: The asset commands that read through the graph — they carry ``--as`` like
#: every other read. ``register``/``purge`` touch the blob store alone.
AS_ASSET_COMMANDS = {"get", "list", "rendition", "download-url", "upload-url"}

#: `ingest handlers` reports what this *install* can extract, not what the graph
#: holds, so it is the one ingest command with no principal.
NO_AS_INGEST_COMMANDS = {"handlers"}


def _needs_as(args) -> bool:
    if not args or "--as" in args:
        return False
    if args[0] == "asset":
        return len(args) > 1 and args[1] in AS_ASSET_COMMANDS
    if args[0] == "ingest":
        return len(args) > 1 and args[1] not in NO_AS_INGEST_COMMANDS
    return args[0] not in NO_AS_GROUPS


def _run_json(*args, input_text=None):
    args = list(args)
    if _needs_as(args):
        args += ["--as", "owner"]
    result = runner.invoke(app, args, input=input_text)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_init(fresh_db):
    payload = _run_json("init")
    assert payload["db_path"].endswith("graph.db")
    assert payload["applied"] == []


def test_node_create_get_update_list(fresh_db):
    created = _run_json(
        "node", "create", "--type", "note", "--title", "CLI note", "--content", "hello"
    )
    assert created["state"] == "active"
    assert created["type"] == "note"

    fetched = _run_json("node", "get", created["id"])
    assert fetched["title"] == "CLI note"

    updated = _run_json("node", "update", created["id"], "--content", "changed", "--set", "k=1")
    assert updated["content"] == "changed"
    assert updated["props"] == {"k": 1}

    listing = _run_json("node", "list", "--type", "note")
    assert listing["count"] == 1
    assert listing["nodes"][0]["id"] == created["id"]


def test_node_create_from_stdin(fresh_db):
    created = _run_json(
        "node",
        "create",
        "--type",
        "note",
        "--title",
        "stdin",
        "--content-file",
        "-",
        input_text="piped body",
    )
    assert created["content"] == "piped body"


def test_node_children(fresh_db):
    page = _run_json("node", "create", "--type", "page", "--title", "P")
    _run_json("node", "create", "--type", "block", "--content", "b1", "--parent", page["id"])
    _run_json("node", "create", "--type", "block", "--content", "b2", "--parent", page["id"])
    children = _run_json("node", "children", page["id"])
    assert children["count"] == 2
    assert [node["position"] for node in children["nodes"]] == [1.0, 2.0]


def test_edge_create_and_list(fresh_db):
    a = _run_json("node", "create", "--type", "claim", "--title", "A")
    b = _run_json("node", "create", "--type", "claim", "--title", "B")
    edge = _run_json(
        "edge", "create", a["id"], b["id"], "--type", "supports", "--confidence", "0.9"
    )
    assert edge["state"] == "active"
    listing = _run_json("edge", "list", "--node", b["id"])
    assert listing["count"] == 1
    assert listing["edges"][0]["id"] == edge["id"]


def test_state_transitions_and_actor(fresh_db):
    # A suggest-level agent's write (via the service; the CLI is human-only).
    proposed = service.create_node(type="note", title="bot", principal=agent("test"))
    assert proposed.state == "proposed"
    proposed = proposed.model_dump(mode="json")
    accepted = _run_json("accept", proposed["id"])
    assert accepted["state"] == "active"
    archived = _run_json("archive", accepted["id"])
    assert archived["state"] == "archived"


def test_reject_records_its_reason_in_the_event(fresh_db):
    """One id or a hundred, a reject leaves the same audit trail."""
    proposed = service.create_node(type="note", title="bot", principal=agent("test"))
    rejected = _run_json("reject", proposed.id, "--reason", "off topic")
    assert rejected["state"] == "archived"

    event = _run_json("events", "--limit", "1")["events"][0]
    assert event["op"] == "node.reject"
    assert event["payload"]["reason"] == "off topic"


def test_reject_without_a_reason_is_refused(fresh_db):
    proposed = service.create_node(type="note", title="bot", principal=agent("test"))
    result = runner.invoke(app, ["reject", proposed.id, "--as", "owner"])
    assert result.exit_code == 2  # typer: missing required --reason
    assert _run_json("node", "get", proposed.id)["state"] == "proposed"


def test_every_list_command_reports_a_count(fresh_db):
    """One envelope for lists: `{"<plural>": [...], "count": n}`, no exceptions."""
    node = _run_json("node", "create", "--type", "note", "--title", "T", "--content", "body")
    for args, key in (
        (("node", "list"), "nodes"),
        (("node", "children", node["id"]), "nodes"),
        (("edge", "list"), "edges"),
        (("history", node["id"]), "versions"),
        (("events",), "events"),
        (("review", "queue"), "proposals"),
        (("asset", "list"), "assets"),
        (("ingest", "handlers"), "handlers"),
        (("projector", "run"), "projectors"),
        (("projector", "status"), "projectors"),
    ):
        payload = _run_json(*args)
        assert payload["count"] == len(payload[key]), args


def test_invalid_transition_exits_1(fresh_db):
    node = _run_json("node", "create", "--type", "note", "--title", "active")
    result = runner.invoke(app, ["accept", node["id"], "--as", "owner"])
    assert result.exit_code == 1
    assert "cannot accept" in result.stderr


def test_undo_and_history(fresh_db):
    node = _run_json("node", "create", "--type", "note", "--title", "H", "--content", "v1")
    _run_json("node", "update", node["id"], "--content", "v2")
    undone = _run_json("undo")
    assert undone["undone_op"] == "node.update"
    fetched = _run_json("node", "get", node["id"])
    assert fetched["content"] == "v1"

    history = _run_json("history", node["id"])
    assert history["count"] == 3  # create, update, undo-restore


def test_events_and_types(fresh_db):
    _run_json("node", "create", "--type", "note", "--title", "T")
    events = _run_json("events")
    assert events["count"] == 1
    assert events["events"][0]["op"] == "node.create"

    types = _run_json("types")
    names = {t["name"] for t in types["node_types"]}
    assert {"page", "note", "claim"} <= names
    edge_names = {t["name"] for t in types["edge_types"]}
    assert "supports" in edge_names


def test_unknown_node_exits_1(fresh_db):
    result = runner.invoke(app, ["node", "get", "missing", "--as", "owner"])
    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_bad_set_pair_exits_1(fresh_db):
    result = runner.invoke(
        app, ["node", "create", "--type", "note", "--set", "nokey", "--as", "owner"]
    )
    assert result.exit_code == 1
    assert "--set expects key=value" in result.stderr


def test_set_values_are_json_with_a_raw_string_fallback(fresh_db):
    """`--set` decodes JSON, and keeps anything that is not JSON as a string.

    Without the fallback, `--set venue=Nature` would exit 1 on a decode error
    instead of storing the obvious string.
    """
    created = _run_json(
        "node",
        "create",
        "--type",
        "source",
        "--title",
        "Paper",
        "--set",
        "venue=Nature",  # bare word: not JSON, kept verbatim
        "--set",
        "year=1815",  # int
        "--set",
        "peer_reviewed=true",  # bool
        "--set",
        'authors=["Ada", "Grace"]',  # array
        "--set",
        'doi={"prefix": "10.1000"}',  # object
        "--set",
        "note=",  # empty value is an empty string, not a crash
        "--set",
        "date=2024-01-02",  # JSON-ish but not JSON: still a string
    )
    assert created["props"] == {
        "venue": "Nature",
        "year": 1815,
        "peer_reviewed": True,
        "authors": ["Ada", "Grace"],
        "doi": {"prefix": "10.1000"},
        "note": "",
        "date": "2024-01-02",
    }


# ── Every reachable failure is a stderr line and exit 1, never a traceback ───


def test_undo_blocked_by_children_exits_1(fresh_db):
    page = _run_json("node", "create", "--type", "page", "--title", "P")
    _run_json("node", "create", "--type", "block", "--content", "b1", "--parent", page["id"])
    create_seq = _run_json("events")["events"][-1]["seq"]

    result = runner.invoke(app, ["undo", str(create_seq), "--as", "owner"])
    assert result.exit_code == 1
    assert "child node" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _run_json("node", "get", page["id"])["id"] == page["id"]


def test_asset_register_missing_file_exits_1(fresh_db, tmp_path):
    result = runner.invoke(app, ["asset", "register", str(tmp_path / "missing.png")])
    assert result.exit_code == 1
    assert "missing.png" in result.stderr
    assert "Traceback" not in result.stderr


def test_locked_database_exits_1(fresh_db, monkeypatch):
    """SQLite has one writer: contention is a message, not a stack trace."""
    real_connect = db.connect

    def impatient_connect(path=None):
        conn = real_connect(path)
        conn.execute("PRAGMA busy_timeout=50")  # keep the test quick
        return conn

    monkeypatch.setattr(db, "connect", impatient_connect)
    blocker = real_connect(fresh_db)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        result = runner.invoke(
            app, ["node", "create", "--type", "note", "--title", "blocked", "--as", "owner"]
        )
    finally:
        blocker.rollback()
        blocker.close()

    assert result.exit_code == 1
    assert "database is locked" in result.stderr
    assert "Traceback" not in result.stderr


def test_search(fresh_db):
    node = _run_json(
        "node", "create", "--type", "note", "--title", "Sap", "--content", "xylem carries sap"
    )
    result = _run_json("search", "xylem")
    assert result["query"] == "xylem"
    assert [hit["node_id"] for hit in result["hits"]] == [node["id"]]
    assert result["hits"][0]["signals"]["bm25"] > 0

    result = _run_json("search", "xylem", "--type", "concept")
    assert result["hits"] == []


def test_search_hybrid_signals_over_the_cli(fresh_db, fake_embedder):
    node = _run_json(
        "node", "create", "--type", "note", "--title", "Sap", "--content", "xylem carries sap"
    )
    result = _run_json("search", "xylem")
    hit = result["hits"][0]
    assert hit["node_id"] == node["id"]
    assert set(hit["signals"]) == {"bm25", "vector"}
    assert hit["score"] == hit["signals"]["bm25"] + hit["signals"]["vector"]


def test_projector_run_status_rebuild(fresh_db):
    _run_json("node", "create", "--type", "note", "--title", "T", "--content", "body")

    status = _run_json("projector", "status")
    statuses = {projector["name"]: projector for projector in status["projectors"]}
    assert set(statuses) == {"fts", "vec"}
    assert statuses["fts"]["pending_events"] == 1
    # No embedding provider in tests: vec reports itself unavailable.
    assert statuses["vec"]["available"] is False
    assert statuses["vec"]["detail"]

    assert status["count"] == len(status["projectors"])

    runs = _run_json("projector", "run")
    assert runs["count"] == len(runs["projectors"])
    by_name = {run["name"]: run for run in runs["projectors"]}
    assert by_name["fts"] == {
        "name": "fts",
        "applied": 1,
        "from_seq": 0,
        "to_seq": 1,
        "detail": None,
    }
    assert by_name["vec"]["applied"] == 0
    assert by_name["vec"]["detail"]

    status = _run_json("projector", "status")
    statuses = {projector["name"]: projector for projector in status["projectors"]}
    assert statuses["fts"]["pending_events"] == 0
    assert statuses["fts"]["rows"] == 1

    rebuilt = _run_json("projector", "rebuild", "fts")
    assert rebuilt["from_seq"] == 0
    assert rebuilt["applied"] == 1


def test_projector_rebuild_unknown_exits_1(fresh_db):
    result = runner.invoke(app, ["projector", "rebuild", "nope"])
    assert result.exit_code == 1
    assert "unknown projector" in result.stderr


def test_node_get_with_depth(fresh_db):
    a = _run_json("node", "create", "--type", "concept", "--title", "A")
    b = _run_json("node", "create", "--type", "concept", "--title", "B")
    _run_json("edge", "create", a["id"], b["id"], "--type", "relates_to")

    subgraph = _run_json("node", "get", a["id"], "--depth", "1")
    assert {node["id"] for node in subgraph["nodes"]} == {a["id"], b["id"]}
    assert len(subgraph["edges"]) == 1


def test_traverse_and_find_path(fresh_db):
    a = _run_json("node", "create", "--type", "concept", "--title", "A")
    b = _run_json("node", "create", "--type", "concept", "--title", "B")
    _run_json("edge", "create", a["id"], b["id"], "--type", "supports")

    walked = _run_json("traverse", a["id"], "--edge-type", "supports")
    assert {node["id"] for node in walked["nodes"]} == {a["id"], b["id"]}

    path = _run_json("find-path", a["id"], b["id"])
    assert path["found"] is True
    assert path["hops"] == 1


def test_diff_and_schema(fresh_db):
    note = _run_json("node", "create", "--type", "note", "--title", "Draft", "--content", "v1")
    _run_json("node", "update", note["id"], "--content", "v2")
    versions = _run_json("history", note["id"])["versions"]

    diffed = _run_json("diff", str(versions[0]["id"]), str(versions[1]["id"]))
    assert diffed["changed_fields"] == ["content"]

    schema = _run_json("schema", "supports")
    assert schema["inverse_name"] == "supported_by"


def test_edge_create_batch_from_stdin(fresh_db):
    a = _run_json("node", "create", "--type", "concept", "--title", "A")
    b = _run_json("node", "create", "--type", "concept", "--title", "B")
    suggestions = json.dumps(
        [
            {"src": a["id"], "dst": b["id"], "edge_type": "relates_to"},
            {"src": a["id"], "dst": "missing", "edge_type": "supports"},
        ]
    )
    result = runner.invoke(app, ["edge", "create-batch", "-", "--as", "owner"], input=suggestions)
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.stdout)
    assert outcome["created"][0]["state"] == "active"
    assert outcome["failed"][0]["index"] == 1


def test_search_date_filters(fresh_db):
    node = _run_json(
        "node", "create", "--type", "note", "--title", "T", "--content", "tardigrade cryptobiosis"
    )
    stamp = node["created_at"]

    # Bounds are exclusive, so the node's own timestamp excludes it either way.
    assert _run_json("search", "tardigrade", "--created-after", stamp)["hits"] == []
    assert _run_json("search", "tardigrade", "--created-before", stamp)["hits"] == []

    windowed = _run_json(
        "search",
        "tardigrade",
        "--created-after",
        "2000-01-01 00:00:00",
        "--created-before",
        "2999-01-01 00:00:00",
    )
    assert [hit["node_id"] for hit in windowed["hits"]] == [node["id"]]


def test_search_filters_and_expand(fresh_db):
    a = _run_json("node", "create", "--type", "concept", "--title", "xylem alpha")
    b = _run_json("node", "create", "--type", "note", "--title", "xylem beta")
    _run_json("edge", "create", a["id"], b["id"], "--type", "supports", "--confidence", "0.9")

    filtered = _run_json("search", "xylem", "--created-by", "agent:nobody")
    assert filtered["hits"] == []

    expanded = _run_json("search", "xylem alpha", "--expand")
    assert [hit["node_id"] for hit in expanded["hits"]] == [a["id"], b["id"]]
    assert expanded["hits"][1]["signals"]["graph"] > 0


def test_agent_update_proposes_and_review_accepts(fresh_db):
    note = _run_json("node", "create", "--type", "note", "--title", "N", "--content", "original")
    # The CLI is human-only (agents use MCP); a suggest-level agent's update is
    # staged via the service, then reviewed over the CLI.
    version = service.update_node(note["id"], content="bot rewrite", principal=agent("researcher"))
    assert version.state == "proposed"

    queue = _run_json("review", "queue", "--kind", "update")
    assert queue["proposals"][0]["id"] == str(version.id)

    accepted = _run_json("accept", str(version.id))
    assert accepted["state"] == "applied"
    assert _run_json("node", "get", note["id"])["content"] == "bot rewrite"


# ── mcp serve: the actor is validated before anything is served ───────────────


def test_human_and_agent_admin_over_the_cli(fresh_db):
    _run_json("human", "create", "alice", "--as", "owner")
    humans = _run_json("human", "list", "--as", "owner")
    assert humans["count"] == 2
    assert {h["name"] for h in humans["humans"]} == {"owner", "alice"}

    result = runner.invoke(app, ["agent", "create", "researcher", "--as", "owner"])
    assert result.exit_code == 0, result.output
    assert "ndm_" in result.stderr  # the token prints once, to stderr
    agents = _run_json("agent", "list", "--as", "owner")
    assert agents["agents"][0]["has_token"] is True

    result = runner.invoke(app, ["agent", "token-rotate", "researcher", "--as", "owner"])
    assert result.exit_code == 0, result.output
    assert "ndm_" in result.stderr

    _run_json("grant", "researcher", "main", "suggest", "--as", "owner")
    listed = _run_json("grants", "--agent", "researcher", "--as", "owner")
    assert listed["grants"][0]["level"] == "suggest"
    _run_json("revoke", "researcher", "main", "--as", "owner")
    # What remains is the creation-time template row (read on meta).
    remaining = _run_json("grants", "--agent", "researcher", "--as", "owner")
    assert [(g["space_id"], g["level"]) for g in remaining["grants"]] == [("meta", "read")]

    _run_json("agent", "disable", "researcher", "--as", "owner")
    assert _run_json("agent", "list", "--as", "owner")["agents"][0]["disabled"] is True


def test_space_admin_over_the_cli(fresh_db):
    created = _run_json("space-create", "sandbox", "--as", "owner")
    assert created["type"] == "space"
    assert created["space_id"] == "meta"
    spaces = _run_json("space-list", "--as", "owner")
    assert {s["title"] for s in spaces["spaces"]} == {"main", "meta", "sandbox"}

    renamed = _run_json("space-rename", "sandbox", "scratch", "--as", "owner")
    assert (renamed["id"], renamed["title"]) == (created["id"], "scratch")

    _run_json("space-archive", created["id"], "--as", "owner")
    spaces = _run_json("space-list", "--as", "owner")
    assert {s["title"] for s in spaces["spaces"]} == {"main", "meta"}


def test_the_cli_inherits_the_space_guards_from_the_service(fresh_db):
    """Neither guard is a screen's job: the CLI has no screen and needs both."""
    _run_json("space-create", "research", "--as", "owner")

    for command in (
        ["space-archive", "main", "--as", "owner"],
        ["space-archive", "meta", "--as", "owner"],
        # The generic node archive reaches the same row, so it is the same hole.
        ["archive", "main", "--as", "owner"],
        # And one name per space, whichever command spells the write.
        ["space-create", "research", "--as", "owner"],
        ["space-rename", "research", "main", "--as", "owner"],
        ["node", "create", "--type", "space", "--title", "research", "--as", "owner"],
        # A space belongs in meta; the generic create is the path that could
        # put one anywhere else, and `--space` defaults to `main`.
        ["node", "create", "--type", "space", "--title", "elsewhere", "--as", "owner"],
        # fmt: off
        [
            "node",
            "create",
            "--type",
            "space",
            "--title",
            "elsewhere",
            "--space",
            "main",
            "--as",
            "owner",
        ],
        # fmt: on
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 1, command
        assert "Traceback" not in result.output

    assert {s["title"] for s in _run_json("space-list", "--as", "owner")["spaces"]} == {
        "main",
        "meta",
        "research",
    }
    # Renaming a structural space is still fine — the id is what things depend on.
    renamed = _run_json("space-rename", "main", "trunk", "--as", "owner")
    assert (renamed["id"], renamed["title"]) == ("main", "trunk")


def test_an_archived_space_keeps_its_name_on_the_cli_too(fresh_db):
    """`space-list` shows active spaces, so the refusal is all the human gets.

    Which is why it says the holder is archived, and why the CLI is where the
    name can actually be freed: `space-rename` resolves live spaces only, so
    renaming a retired space is `node update` on its id.
    """
    created = _run_json("space-create", "research", "--as", "owner")
    _run_json("space-archive", created["id"], "--as", "owner")

    refused = runner.invoke(app, ["space-create", "research", "--as", "owner"])
    assert refused.exit_code == 1
    assert "archived space already answers to 'research'" in refused.output
    assert "Traceback" not in refused.output

    # Freeing it is a node-title update, and then the name is available again.
    _run_json("node", "update", created["id"], "--title", "research-2025", "--as", "owner")
    reused = _run_json("space-create", "research", "--as", "owner")
    assert reused["title"] == "research"
    assert reused["id"] != created["id"]


def test_a_grant_on_an_archived_space_is_inert_and_still_revocable_on_the_cli(fresh_db):
    """`space-archive` promises the grants go inert; `grant-revoke` must reach them.

    Both halves were wrong: the agent kept live authority over everything in the
    space it could reach by node id, and `revoke` resolved active spaces only,
    so there was no supported way to take the grant away at all.
    """
    created = _run_json("space-create", "research", "--as", "owner")
    _run_json("agent", "create", "researcher", "--as", "owner")
    _run_json("grant", "researcher", "research", "edit", "--as", "owner")
    _run_json("space-archive", created["id"], "--as", "owner")

    # The row survives so the human can see what is still delegated.
    held = _run_json("grants", "--agent", "researcher", "--as", "owner")["grants"]
    assert (created["id"], "edit") in {(row["space_id"], row["level"]) for row in held}

    # Granting more is refused, in one line, naming the reason.
    refused = runner.invoke(app, ["grant", "researcher", "research", "read", "--as", "owner"])
    assert refused.exit_code == 1
    assert "cannot grant on the archived space" in refused.output
    assert "Traceback" not in refused.output

    # And it can be revoked, by the space's name as well as by its id.
    _run_json("revoke", "researcher", "research", "--as", "owner")
    left = _run_json("grants", "--agent", "researcher", "--as", "owner")["grants"]
    assert created["id"] not in {row["space_id"] for row in left}


def test_space_list_reports_live_counts_and_grant_holders(fresh_db):
    _run_json("space-create", "research", "--as", "owner")
    _run_json(
        "node", "create", "--type", "note", "--title", "n", "--space", "research", "--as", "owner"
    )
    runner.invoke(app, ["agent", "create", "researcher", "--as", "owner"])
    _run_json("grant", "researcher", "research", "suggest", "--as", "owner")

    listed = {s["title"]: s for s in _run_json("space-list", "--as", "owner")["spaces"]}

    assert listed["research"]["node_count"] == 1
    assert [(g["agent_id"], g["level"]) for g in listed["research"]["grants"]] == [
        ("researcher", "suggest")
    ]


def test_the_space_filter_and_meta_toggle_over_the_cli(fresh_db):
    """The two read-side controls, independent of the write target."""
    _run_json("space-create", "research", "--as", "owner")
    _run_json(
        "node",
        "create",
        "--type",
        "note",
        "--title",
        "main memo",
        "-c",
        "territory",
        "--as",
        "owner",
    )
    _run_json(
        "node",
        "create",
        "--type",
        "note",
        "--title",
        "filed",
        "-c",
        "territory",
        "--space",
        "research",
        "--as",
        "owner",
    )

    everything = _run_json("node", "list", "--as", "owner")
    assert {row["title"] for row in everything["nodes"]} == {"main memo", "filed"}
    narrowed = _run_json("node", "list", "--space", "research", "--as", "owner")
    assert [row["title"] for row in narrowed["nodes"]] == ["filed"]

    # Meta is out of a content listing until it is asked for by name or by flag.
    assert {row["space_id"] for row in everything["nodes"]} == {
        "main",
        narrowed["nodes"][0]["space_id"],
    }
    with_meta = _run_json("node", "list", "--include-meta", "--as", "owner")
    assert "meta" in {row["space_id"] for row in with_meta["nodes"]}

    hits = _run_json("search", "territory", "--space", "research", "--as", "owner")
    assert [hit["title"] for hit in hits["hits"]] == ["filed"]


def test_mcp_serve_requires_an_agent_token(fresh_db, monkeypatch):
    monkeypatch.delenv("NODUM_AGENT_TOKEN", raising=False)
    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1, result.output
    assert "NODUM_AGENT_TOKEN" in result.output


# ── Assets and renditions ─────────────────────────────────────────────────────


def _register_png(tmp_path, size=(800, 400), name="cli.png"):
    from PIL import Image

    source = tmp_path / name
    Image.new("RGB", size, (10, 200, 90)).save(source)
    return _run_json("asset", "register", str(source))


def test_asset_register_get_list(fresh_db, tmp_path):
    asset = _register_png(tmp_path)
    assert asset["mime"] == "image/png"

    fetched = _run_json("asset", "get", asset["hash"])
    assert fetched["hash"] == asset["hash"]

    listing = _run_json("asset", "list")
    assert listing["count"] == 1


def test_asset_rendition_out_and_purge(fresh_db, tmp_path):
    asset = _register_png(tmp_path, size=(800, 400))
    out_file = tmp_path / "preview.webp"

    rendition = _run_json(
        "asset", "rendition", asset["hash"], "--profile", "preview", "--out", str(out_file)
    )
    assert rendition["mime"] == "image/webp"
    assert rendition["cached"] is False
    assert (rendition["width"], rendition["height"]) == (800, 400)
    assert out_file.read_bytes()[:4] == b"RIFF"
    # The key is always in the envelope; the CLI never fills it — bytes go to
    # --out, never into stdout.
    assert rendition["data_base64"] is None
    assert out_file.stat().st_size == rendition["size_bytes"]

    cached = _run_json("asset", "rendition", asset["hash"], "--profile", "preview")
    assert cached["cached"] is True

    purged = _run_json("asset", "purge")
    assert purged["purged"] == 1
    regenerated = _run_json("asset", "rendition", asset["hash"], "--profile", "preview")
    assert regenerated["cached"] is False


def test_asset_rendition_rejects_non_images(fresh_db, tmp_path):
    text_file = tmp_path / "doc.txt"
    text_file.write_text("not an image")
    asset = _run_json("asset", "register", str(text_file))

    result = runner.invoke(app, ["asset", "rendition", asset["hash"], "--as", "owner"])
    assert result.exit_code == 1
    assert "only supported for image assets" in result.stderr


@pytest.mark.skipif(
    not extract.PdfHandler().availability()[0], reason="the pdf extra is not installed"
)
def test_asset_rendition_rasterises_a_pdf_page(fresh_db, tmp_path):
    """`page:<n>` is an ordinary rendition: lazily generated, then cached."""
    asset = _run_json("asset", "register", str(FIXTURE_PDF))
    out_file = tmp_path / "page1.webp"

    rendition = _run_json(
        "asset", "rendition", asset["hash"], "--profile", "page:1", "--out", str(out_file)
    )
    assert rendition["profile"] == "page:1"
    assert rendition["mime"] == "image/webp"
    assert rendition["cached"] is False
    assert rendition["data_base64"] is None
    assert out_file.read_bytes()[:4] == b"RIFF"

    cached = _run_json("asset", "rendition", asset["hash"], "--profile", "page:1")
    assert cached["cached"] is True
    assert cached["id"] == rendition["id"]


def test_asset_rendition_rejects_a_malformed_page_profile(fresh_db, tmp_path):
    """Profile validation lives in `assets.resolve_profile`; the CLI just reports it."""
    asset = _register_png(tmp_path)

    result = runner.invoke(
        app, ["asset", "rendition", asset["hash"], "--profile", "page:0", "--as", "owner"]
    )
    assert result.exit_code == 1
    assert "unknown rendition profile" in result.stderr
    assert "Traceback" not in result.stderr


# ── Capability URLs ──────────────────────────────────────────────────────────


def test_asset_download_url_prints_a_url_and_an_expiry(fresh_db, tmp_path):
    asset = _register_png(tmp_path)

    grant = _run_json("asset", "download-url", asset["hash"])

    assert grant["kind"] == "download"
    assert grant["asset_hash"] == asset["hash"]
    assert grant["url"].endswith(f"/api/download/{grant['token']}")
    assert grant["expires_at"]


def test_asset_download_url_honours_the_ttl_ceiling(fresh_db, tmp_path):
    """`--ttl` reaches `urls.mint_download`, bounds included."""
    asset = _register_png(tmp_path)
    assert _run_json("asset", "download-url", asset["hash"], "--ttl", "60")["token"]

    result = runner.invoke(
        app, ["asset", "download-url", asset["hash"], "--ttl", "999999", "--as", "owner"]
    )
    assert result.exit_code == 1
    assert "ttl_seconds must be between" in result.stderr


def test_asset_download_url_for_an_unknown_asset_exits_1(fresh_db):
    result = runner.invoke(app, ["asset", "download-url", "nope", "--as", "owner"])
    assert result.exit_code == 1
    assert "asset not found" in result.stderr
    assert "Traceback" not in result.stderr


def test_asset_upload_url_dedups_a_hash_already_stored(fresh_db, tmp_path):
    """Design §5.7 rule 4: a known sha256 answers with the asset and no grant."""
    asset = _register_png(tmp_path)

    deduped = _run_json(
        "asset",
        "upload-url",
        "--name",
        "cli.png",
        "--mime",
        "image/png",
        "--size",
        str(asset["size_bytes"]),
        "--sha256",
        asset["hash"],
    )

    assert deduped["grant"] is None
    assert deduped["asset"]["hash"] == asset["hash"]


def test_asset_upload_url_without_a_known_hash_mints_a_grant(fresh_db):
    granted = _run_json(
        "asset", "upload-url", "--name", "new.png", "--mime", "image/png", "--size", "1024"
    )

    assert granted["asset"] is None
    assert granted["grant"]["kind"] == "upload"
    assert granted["grant"]["max_bytes"] == 1024
    assert granted["grant"]["url"].endswith(f"/api/uploads/{granted['grant']['token']}")


# ── Ingestion ────────────────────────────────────────────────────────────────


def _drop_folder(tmp_path, names, body="body of {name}"):
    """Build a folder of small text files, plus a dotfile that must be skipped."""
    folder = tmp_path / "drop"
    folder.mkdir()
    for name in names:
        (folder / name).write_text(body.format(name=name), encoding="utf-8")
    (folder / ".hidden.txt").write_text("never ingested", encoding="utf-8")
    return folder


def test_ingest_file_prints_one_object(fresh_db, tmp_path):
    """One path naming a file: one JSON object, the whole subgraph in it."""
    source = tmp_path / "note.txt"
    source.write_text("xylem carries sap", encoding="utf-8")

    result = _run_json("ingest", "file", str(source))

    assert result["created"] is True
    assert result["extraction"]["handler"] == "text"
    assert result["asset"]["original_name"] == "note.txt"
    assert result["asset_ref"]["type"] == "asset_ref"
    assert result["source"]["content"] == "xylem carries sap"
    assert result["edges"][0]["type"] == "derived_from"
    assert result["event_seq"] > 0


def test_ingest_file_takes_name_title_and_space(fresh_db, tmp_path):
    seed_space("research")
    source = tmp_path / "note.txt"
    source.write_text("scoped body", encoding="utf-8")

    result = _run_json(
        "ingest",
        "file",
        str(source),
        "--name",
        "renamed.txt",
        "--title",
        "A Better Title",
        "--space",
        "research",
    )

    assert result["asset"]["original_name"] == "renamed.txt"
    assert result["source"]["title"] == "A Better Title"
    assert result["source"]["space_id"] == "research"


def test_ingest_file_on_a_directory_ingests_every_file(fresh_db, tmp_path):
    """The phase's exit criterion in miniature: drop a folder, get subgraphs."""
    folder = _drop_folder(tmp_path, ["a.txt", "b.txt", "c.txt"])

    payload = _run_json("ingest", "file", str(folder))

    assert payload["count"] == 3
    assert [item["asset"]["original_name"] for item in payload["ingestions"]] == [
        "a.txt",
        "b.txt",
        "c.txt",
    ]
    assert all(item["created"] for item in payload["ingestions"])
    # Every file became its own subgraph, dotfile excluded.
    assert _run_json("node", "list", "--type", "asset_ref")["count"] == 3
    assert _run_json("node", "list", "--type", "source")["count"] == 3
    assert _run_json("edge", "list", "--type", "derived_from")["count"] == 3


def test_recursive_reaches_a_nested_file_and_the_default_does_not(fresh_db, tmp_path):
    folder = _drop_folder(tmp_path, ["top.txt"])
    (folder / "deep").mkdir()
    (folder / "deep" / "nested.txt").write_text("nested body", encoding="utf-8")

    shallow = _run_json("ingest", "file", str(folder))
    assert [item["asset"]["original_name"] for item in shallow["ingestions"]] == ["top.txt"]

    deep = _run_json("ingest", "file", str(folder), "--recursive")
    assert [item["asset"]["original_name"] for item in deep["ingestions"]] == [
        "nested.txt",
        "top.txt",
    ]
    # The re-run of top.txt converges instead of duplicating: ingestion is idempotent.
    assert [item["created"] for item in deep["ingestions"]] == [True, False]


def test_a_failing_path_does_not_lose_the_batch_successes(fresh_db, tmp_path):
    """A batch reports the failure, keeps the successes on stdout, and exits 1."""
    good = tmp_path / "good.txt"
    good.write_text("kept", encoding="utf-8")
    missing = tmp_path / "missing.txt"

    result = runner.invoke(app, ["ingest", "file", str(good), str(missing), "--as", "owner"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["ingestions"][0]["asset"]["original_name"] == "good.txt"
    assert "missing.txt" in result.stderr
    assert "not a file" in result.stderr
    assert "Traceback" not in result.stderr
    # The success is really in the graph, not just in the envelope.
    assert _run_json("node", "list", "--type", "source")["count"] == 1


def test_name_and_title_are_refused_for_a_multi_file_run(fresh_db, tmp_path):
    """They describe one document; silently stamping twenty files with one title
    is the footgun this refusal exists for."""
    folder = _drop_folder(tmp_path, ["a.txt", "b.txt"])

    result = runner.invoke(app, ["ingest", "file", str(folder), "--title", "One", "--as", "owner"])

    assert result.exit_code == 1
    assert "--name and --title describe one document" in result.stderr
    assert _run_json("node", "list", "--type", "source")["count"] == 0


def test_an_empty_directory_is_reported_rather_than_silently_ingesting_nothing(fresh_db, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    result = runner.invoke(app, ["ingest", "file", str(empty), "--as", "owner"])

    assert result.exit_code == 1
    assert "no files to ingest" in result.stderr


def test_ingest_file_missing_path_exits_1(fresh_db, tmp_path):
    result = runner.invoke(app, ["ingest", "file", str(tmp_path / "missing.txt"), "--as", "owner"])
    assert result.exit_code == 1
    assert "not a file" in result.stderr
    assert "Traceback" not in result.stderr


class _CannedHandler(BaseHTTPRequestHandler):
    """Serves one canned response; nothing here reaches the network."""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming
        body, content_type = self.server.canned
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture()
def fixture_server():
    """A loopback HTTP server serving one canned body — the suite never leaves the machine."""
    server = HTTPServer(("127.0.0.1", 0), _CannedHandler)
    server.canned = (b"", "text/plain")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_ingest_url_over_the_cli(fresh_db, fixture_server):
    fixture_server.canned = (
        b"<html><body><p>Basin hydrology</p></body></html>",
        "text/html; charset=utf-8",
    )
    url = f"http://127.0.0.1:{fixture_server.server_address[1]}/article"

    result = _run_json("ingest", "url", url)

    assert result["created"] is True
    assert result["extraction"]["handler"] == "html"
    assert result["source"]["content"] == "Basin hydrology"
    assert result["source"]["props"]["url"] == url
    assert result["asset_ref"]["props"]["url"] == url


def test_ingest_url_bad_scheme_exits_1(fresh_db):
    result = runner.invoke(app, ["ingest", "url", "file:///etc/passwd", "--as", "owner"])
    assert result.exit_code == 1
    assert "ingest_url takes" in result.stderr
    assert "Traceback" not in result.stderr


def test_ingest_handlers_lists_every_handler_with_its_availability(fresh_db):
    """How a user finds out why their PDF produced no text."""
    payload = _run_json("ingest", "handlers")

    assert [handler["name"] for handler in payload["handlers"]] == [
        handler.name for handler in extract.REGISTRY
    ]
    assert all(handler["mimes"] for handler in payload["handlers"])
    by_name = {handler["name"]: handler for handler in payload["handlers"]}
    # text and html are stdlib-only, so they can never be unavailable.
    assert by_name["text"]["available"] is True
    assert by_name["html"]["available"] is True
    # Whatever is unavailable here says why, and names the extra to install.
    assert all(handler["detail"] for handler in payload["handlers"] if not handler["available"])


def test_ingest_handlers_needs_no_principal_and_no_database(tmp_path, monkeypatch):
    """Availability is a property of the install, so it answers on a bare one."""
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "never-created.db"))

    result = runner.invoke(app, ["ingest", "handlers"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["count"] > 0
    assert not (tmp_path / "never-created.db").exists()


def test_version_flag_short_circuits():
    """`--version` prints the version and exits 0 without needing a database."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("nodum ")


def test_schema_dump_describes_command_surface():
    """`schema-dump` enumerates the CLI's own commands — the self-describing contract."""
    payload = _run_json("schema-dump")
    assert payload["tool"] == "nodum"
    assert payload["version"]

    names = {command["name"] for command in payload["commands"]}
    assert {"node", "edge", "search", "schema", "schema-dump", "mcp"} <= names


def test_schema_dump_recurses_into_groups():
    """A command group carries its subcommands, so the dump covers the real surface."""
    payload = _run_json("schema-dump")
    node = next(c for c in payload["commands"] if c["name"] == "node")
    subcommands = {sub["name"] for sub in node["subcommands"]}
    assert {"create", "get", "update"} <= subcommands


def test_schema_dump_surfaces_params():
    """Params are introspected despite typer vendoring its own click."""
    payload = _run_json("schema-dump")
    schema_command = next(c for c in payload["commands"] if c["name"] == "schema")
    assert any(p["kind"] == "argument" and p["name"] == "type" for p in schema_command["params"])
