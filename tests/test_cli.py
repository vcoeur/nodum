"""CLI smoke tests: every command emits one parseable JSON object on stdout."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import typer
from helpers import agent, owner, seed_space
from typer.testing import CliRunner

from nodum import auth, db, extract, llm, service
from nodum import consolidate as consolidate_module
from nodum.cli import app
from nodum.migrations import GARDENER_AGENT_ID

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


def test_edge_list_as_of(fresh_db):
    """The D2/B8 gate through the CLI: a closed window hides the edge from the
    live read and from an as-of read after the close, and shows it at the
    instants the window covered."""
    a = _run_json("node", "create", "--type", "claim", "--title", "A")
    b = _run_json("node", "create", "--type", "claim", "--title", "B")
    edge = _run_json("edge", "create", a["id"], b["id"], "--type", "supports")
    retired = _run_json("archive", edge["id"])
    assert retired["state"] == "archived"
    assert retired["valid_to"]

    # A deterministic window, exactly as test_service stamps it.
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE edges SET valid_from = ?, valid_to = ? WHERE id = ?",
            ("2026-08-01 10:00:00", "2026-08-01 10:00:10", edge["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    assert _run_json("edge", "list", "--state", "active")["edges"] == []
    mid = _run_json("edge", "list", "--as-of", "2026-08-01 10:00:05")
    assert [e["id"] for e in mid["edges"]] == [edge["id"]]
    assert _run_json("edge", "list", "--as-of", "2026-08-01 10:00:20")["edges"] == []


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
        (("projector", "skips"), "skips"),
        (("cycle-list",), "cycles"),
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
    assert isinstance(result.exception, SystemExit)


def test_every_command_that_reads_a_file_reports_a_missing_one_in_one_line(fresh_db, tmp_path):
    """A missing file is the CLI contract's own example of what must never be a traceback.

    ``asset register`` and ``ingest file`` already held it; these three read
    their file *outside* ``_run`` — ``Path(...).read_text()`` evaluated in the
    argument list of the command's own ``_run(...)``, which Python builds before
    ``_run`` is entered — so a missing path raised ``FileNotFoundError`` past the
    error boundary and printed a full Rich traceback with an exit code that was
    not 1. Exactly the trap ``_principal`` was moved inside ``_run`` to close.
    """
    missing = str(tmp_path / "missing.md")
    node = _run_json("node", "create", "--type", "note", "--title", "Target")
    for command in (
        ["node", "create", "--type", "note", "--title", "T", "--content-file", missing],
        ["node", "update", node["id"], "--content-file", missing],
        ["edge", "create-batch", str(tmp_path / "missing.json")],
        # The two that already behaved, swept with the rest so the rule is one
        # rule rather than two commands that happen to agree.
        ["asset", "register", str(tmp_path / "missing.png")],
        ["ingest", "file", str(tmp_path / "missing.pdf")],
    ):
        args = [*command, "--as", "owner"] if _needs_as(command) else list(command)
        result = runner.invoke(app, args)

        assert result.exit_code == 1, command
        assert "missing." in result.stderr, command
        assert isinstance(result.exception, SystemExit), command
        assert result.stdout == "", command


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
    assert isinstance(result.exception, SystemExit)


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
    # "xylem vessels" (with the "Sap" title) keeps this above the vector
    # similarity floor: the pre-floor fixture "xylem carries sap" repeated
    # "sap" across title and content, which diluted the HashEmbedder cosine
    # to 0.59 — below search._VECTOR_MIN_SIMILARITY — and dropped the signal.
    node = _run_json(
        "node", "create", "--type", "note", "--title", "Sap", "--content", "xylem vessels"
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
        "skipped": 0,
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


def test_projector_skips_lists_quarantined_events(fresh_db):
    """Finding M12's CLI half: the quarantine has a read surface.

    `projector status` reports the count; `projector skips` lists the rows
    behind it, so a human can see which event was skipped and why.
    """
    _run_json("node", "create", "--type", "note", "--title", "one", "--content", "xylem")
    _run_json("node", "create", "--type", "note", "--title", "two", "--content", "phloem")
    conn = db.connect(fresh_db)
    try:
        conn.execute("UPDATE events SET payload = '{not json' WHERE seq = 1")
        conn.commit()
    finally:
        conn.close()

    runs = _run_json("projector", "run")
    fts = {run["name"]: run for run in runs["projectors"]}["fts"]
    assert fts["skipped"] == 1
    assert fts["applied"] == 1

    status = _run_json("projector", "status")
    statuses = {entry["name"]: entry for entry in status["projectors"]}
    assert statuses["fts"]["skipped"] == 1

    skips = _run_json("projector", "skips")
    assert skips["count"] == 1
    (row,) = skips["skips"]
    assert (row["projector"], row["seq"], row["op"]) == ("fts", 1, "node.create")
    assert "JSONDecodeError" in row["error"]


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
            {"src": a["id"], "dst": b["id"], "edge_type": "supports"},
        ]
    )
    result = runner.invoke(app, ["edge", "create-batch", "-", "--as", "owner"], input=suggestions)
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.stdout)
    assert [edge["state"] for edge in outcome["created"]] == ["active", "active"]
    assert outcome["failed"] == []


def test_an_edge_create_batch_that_wrote_nothing_does_not_exit_zero(fresh_db):
    """`edge create-batch`'s half of the batch rule: refusals buy exit 1.

    `retype`'s defect, the batch-edge surface: the envelope reported every
    suggestion in `failed[]` and the command still exited **0** — the one thing
    a script reads. `ingest file`'s rule applies here too: a run that wrote
    nothing must not report success. The identifier on the stderr line is the
    input index, not an id — that is all a suggestion has.
    """
    a = _run_json("node", "create", "--type", "concept", "--title", "A")
    b = _run_json("node", "create", "--type", "concept", "--title", "B")
    suggestions = json.dumps(
        [
            {"src": a["id"], "dst": "missing", "edge_type": "supports"},
            {"src": "also-missing", "dst": b["id"], "edge_type": "relates_to"},
        ]
    )
    result = runner.invoke(app, ["edge", "create-batch", "-", "--as", "owner"], input=suggestions)

    assert result.exit_code == 1
    outcome = json.loads(result.stdout)
    assert outcome["created"] == []
    assert [failure["index"] for failure in outcome["failed"]] == [0, 1]
    # The index is the name on stderr, and the reason rides along beside it.
    assert "failed 0:" in result.stderr
    assert "failed 1:" in result.stderr
    assert isinstance(result.exception, SystemExit)
    # The graph wrote nothing at all.
    assert _run_json("edge", "list")["count"] == 0


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


def test_search_as_of_reads_expansion_through_the_validity_window(fresh_db):
    """The D2/B8 gate through the CLI: `--as-of` re-opens the expansion to a
    retired edge at the instants its window covered."""
    a = _run_json("node", "create", "--type", "concept", "--title", "xylem alpha")
    b = _run_json("node", "create", "--type", "note", "--title", "xylem beta")
    edge = _run_json(
        "edge", "create", a["id"], b["id"], "--type", "supports", "--confidence", "0.9"
    )
    _run_json("archive", edge["id"])

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE edges SET valid_from = ?, valid_to = ? WHERE id = ?",
            ("2026-08-01 10:00:00", "2026-08-01 10:00:10", edge["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    live = _run_json("search", "xylem alpha", "--expand")
    assert [hit["node_id"] for hit in live["hits"]] == [a["id"]]
    mid = _run_json("search", "xylem alpha", "--expand", "--as-of", "2026-08-01 10:00:05")
    assert [hit["node_id"] for hit in mid["hits"]] == [a["id"], b["id"]]
    # `--nl` layers the rewrite on the identical filters; with no provider it
    # is a no-op that searches the original words, so as-of still applies.
    nl_mid = _run_json(
        "search", "xylem alpha", "--expand", "--nl", "--as-of", "2026-08-01 10:00:05"
    )
    assert [hit["node_id"] for hit in nl_mid["hits"]] == [a["id"], b["id"]]


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


def test_review_accept_applies_and_a_refused_batch_does_not_exit_zero(fresh_db):
    """`review accept`'s half of the batch rule: refusals buy exit 1.

    The single-id `accept` fails loudly through `_run`, but the batch surface
    reported its refusals in `failed[]` and still exited **0** — `ingest file`'s
    rule, which every batch command follows: the envelope is printed whatever
    happened, each refused id is named on stderr, and a run that accomplished
    nothing must not report success to a script that only reads the code.
    """
    proposal = service.create_node(type="note", title="Bot note", principal=agent("researcher"))
    assert proposal.state == "proposed"

    accepted = _run_json("review", "accept", proposal.id)
    assert accepted["transitioned"] == [proposal.id]
    assert accepted["failed"] == []
    assert _run_json("node", "get", proposal.id)["state"] == "active"

    refused = runner.invoke(app, ["review", "accept", "no-such-proposal", "--as", "owner"])
    assert refused.exit_code == 1
    payload = json.loads(refused.stdout)
    assert payload["transitioned"] == []
    assert [failure["id"] for failure in payload["failed"]] == ["no-such-proposal"]
    assert "failed no-such-proposal:" in refused.stderr
    assert isinstance(refused.exception, SystemExit)


# ── mcp serve: the actor is validated before anything is served ───────────────


def test_human_and_agent_admin_over_the_cli(fresh_db):
    _run_json("human", "create", "alice", "--as", "owner")
    humans = _run_json("human", "list", "--as", "owner")
    assert humans["count"] == 2
    assert {h["name"] for h in humans["humans"]} == {"owner", "alice"}

    result = runner.invoke(app, ["agent", "create", "researcher", "--as", "owner"])
    assert result.exit_code == 0, result.output
    assert "ndm_" in result.stderr  # the token prints once, to stderr
    agents = {row["id"]: row for row in _run_json("agent", "list", "--as", "owner")["agents"]}
    assert agents["researcher"]["has_token"] is True
    # The gardener lists beside it, credential-less: it authenticates in-process.
    assert agents[GARDENER_AGENT_ID]["has_token"] is False

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
    disabled = {row["id"]: row for row in _run_json("agent", "list", "--as", "owner")["agents"]}
    assert disabled["researcher"]["disabled"] is True


def test_agent_create_has_no_kind_flag_at_all(fresh_db):
    """The flag's one non-default value became a permanent refusal, so it is gone.

    `service.create_agent` refuses `kind="internal"` outright — a second
    internal agent does not add a gardener, it takes the existing one away, and
    with no `agent delete` the install was recoverable only by hand-editing the
    database. What was left was a flag with exactly one accepted value, offering
    a choice the service does not have; HTTP already hardcoded `external`.

    **Nothing here reads Typer's rendered error panel.** It used to assert
    `"--kind" in refused.output`, which passed locally and failed on both CI
    matrix jobs: the panel is wrapped to the terminal's width, and a narrow
    runner cuts the flag out of it. That assertion was never about the
    behaviour anyway — it checked how Click formats a message. The behaviour is
    that the option is not accepted **whatever value follows it**, that no
    account is written when one is passed, and that the flag is absent from the
    self-describing surface. All three are readable without rendering anything.
    """
    for value in ("internal", "external"):
        refused = runner.invoke(
            app, ["agent", "create", "mygardener", "--kind", value, "--as", "owner"]
        )
        # 2 is Click's usage-error code; `external` was the flag's own default,
        # so it failing too is what says the *option* is gone rather than one of
        # its values.
        assert refused.exit_code == 2, refused.output
        assert "mygardener" not in {row["id"] for row in _run_json("agent", "list")["agents"]}
    # And the flag is gone from the self-describing surface too.
    payload = _run_json("schema-dump")
    agent_group = next(row for row in payload["commands"] if row["name"] == "agent")
    create = next(row for row in agent_group["subcommands"] if row["name"] == "create")
    assert "--kind" not in {flag for param in create["params"] for flag in param.get("flags", ())}

    # An ordinary create still works, and is external.
    made = runner.invoke(app, ["agent", "create", "researcher", "--as", "owner"])
    assert made.exit_code == 0, made.output
    assert json.loads(made.stdout)["agent"]["kind"] == "external"


def test_space_admin_over_the_cli(fresh_db):
    created = _run_json("space-create", "sandbox", "--as", "owner")
    assert created["type"] == "space"
    assert created["space_id"] == "meta"
    spaces = _run_json("space-list", "--as", "owner")
    # `conventions` (migration 0016) is a real space, listed like any other.
    assert {s["title"] for s in spaces["spaces"]} == {"main", "meta", "conventions", "sandbox"}

    renamed = _run_json("space-rename", "sandbox", "scratch", "--as", "owner")
    assert (renamed["id"], renamed["title"]) == (created["id"], "scratch")

    _run_json("space-archive", created["id"], "--as", "owner")
    spaces = _run_json("space-list", "--as", "owner")
    assert {s["title"] for s in spaces["spaces"]} == {"main", "meta", "conventions"}


def test_the_cli_inherits_the_space_guards_from_the_service(fresh_db):
    """Neither guard is a screen's job: the CLI has no screen and needs both."""
    _run_json("space-create", "research", "--as", "owner")

    for command, expected in (
        (["space-archive", "main", "--as", "owner"], "cannot archive the 'main' space"),
        (["space-archive", "meta", "--as", "owner"], "cannot archive the 'meta' space"),
        # The generic node archive reaches the same row, so it is the same hole.
        (["archive", "main", "--as", "owner"], "cannot archive the 'main' space"),
        # And one name per space, whichever command spells the write.
        (["space-create", "research", "--as", "owner"], "a space already answers to 'research'"),
        (
            ["space-rename", "research", "main", "--as", "owner"],
            "a space already answers to 'main'",
        ),
        (
            ["node", "create", "--type", "space", "--title", "research", "--as", "owner"],
            "a space must live in the 'meta' space",
        ),
        # A space belongs in meta; the generic create is the path that could
        # put one anywhere else, and `--space` defaults to `main`.
        (
            ["node", "create", "--type", "space", "--title", "elsewhere", "--as", "owner"],
            "a space must live in the 'meta' space",
        ),
        # fmt: off
        (
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
            "a space must live in the 'meta' space",
        ),
        # fmt: on
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 1, command
        assert expected in result.stderr, command
        assert isinstance(result.exception, SystemExit), command

    assert {s["title"] for s in _run_json("space-list", "--as", "owner")["spaces"]} == {
        "main",
        "meta",
        "conventions",
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
    assert isinstance(refused.exception, SystemExit)

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
    assert isinstance(refused.exception, SystemExit)

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
    assert isinstance(result.exception, SystemExit)


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
    assert isinstance(result.exception, SystemExit)


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
    assert isinstance(result.exception, SystemExit)
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
    assert isinstance(result.exception, SystemExit)


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
    assert isinstance(result.exception, SystemExit)


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


# ── Consolidation cycles, the journal, and the curative tier (§8.2/§8.4) ─────


def _claim(title):
    """One active claim node, written over the CLI like everything else here."""
    return _run_json("node", "create", "--type", "claim", "--title", title)


def test_consolidate_runs_the_gardeners_jobs_and_journals_them(fresh_db):
    """The gardener acts, the human asks — and the journal records both."""
    _claim("Kafka Stream")
    _claim("Kafka Streams")

    outcome = _run_json("consolidate")

    cycle = outcome["cycle"]
    assert cycle["trigger"] == "manual"
    assert cycle["triggered_by"] == "human:owner"  # who asked
    assert (cycle["status"], cycle["dry_run"]) == ("completed", False)
    assert [job["name"] for job in outcome["report"]["jobs"]] == list(consolidate_module.JOBS)

    jobs = {job["name"]: job for job in outcome["report"]["jobs"]}
    (proposed,) = jobs["duplicate_candidates"]["proposed"]

    # The diff is the log, never the report: the edge the cycle proposed is
    # attributed to the gardener and stamped with the cycle.
    diff = _run_json("events", "--cycle", cycle["id"])
    assert [event["op"] for event in diff["events"]] == ["edge.propose"]
    assert diff["events"][0]["actor"] == f"agent:{GARDENER_AGENT_ID}"
    assert _run_json("edge", "list")["edges"][0]["id"] == proposed


def test_consolidate_dry_run_journals_the_rehearsal_and_emits_no_event(fresh_db):
    """A rehearsal is in the journal, and its event list is empty.

    That emptiness is the machine-checkable form of "it changed nothing".
    """
    _claim("Kafka Stream")
    _claim("Kafka Streams")

    outcome = _run_json("consolidate", "--dry-run")

    assert outcome["cycle"]["dry_run"] is True
    assert outcome["cycle"]["status"] == "completed"
    assert _run_json("events", "--cycle", outcome["cycle"]["id"])["count"] == 0
    assert _run_json("edge", "list")["count"] == 0


def test_consolidate_selects_jobs_and_a_scope(fresh_db):
    scoped = _run_json("consolidate", "--job", "neglect_report", "--scope", "main")

    assert [job["name"] for job in scoped["report"]["jobs"]] == ["neglect_report"]
    assert scoped["cycle"]["scope"] == "main"
    assert scoped["report"]["scope"] == "main"


def test_consolidate_refuses_a_repeated_job_name(fresh_db):
    """M18: `--job abstraction --job abstraction` is the same work twice.

    Each invocation mints a fresh full budget, so a repeat would spend 2x
    `NODUM_LLM_CYCLE_BUDGET` and file a second report over the same graph —
    refused before any cycle opens, like a misspelled job name.
    """
    result = runner.invoke(
        app, ["consolidate", "--job", "abstraction", "--job", "abstraction", "--as", "owner"]
    )

    assert result.exit_code == 1
    assert "duplicate consolidation job(s): abstraction" in result.stderr
    assert isinstance(result.exception, SystemExit)


def test_consolidate_accepts_distinct_job_names(fresh_db):
    """M18: repeats are refused, distinct names still run, in the order given."""
    outcome = _run_json("consolidate", "--job", "abstraction", "--job", "curation")

    assert [job["name"] for job in outcome["report"]["jobs"]] == ["abstraction", "curation"]


def test_cycle_list_and_cycle_get_are_the_journal(fresh_db):
    first = _run_json("consolidate", "--dry-run")["cycle"]
    second = _run_json("consolidate")["cycle"]

    listing = _run_json("cycle-list")
    assert [row["id"] for row in listing["cycles"]] == [second["id"], first["id"]]

    assert _run_json("cycle-get", first["id"]) == first


def test_cycle_abandon_is_the_door_out_of_an_interrupted_run(fresh_db):
    """A cycle left `running` makes its own writes irreversible on every surface.

    `rollback` refuses a cycle whose event set is not closed and `undo` refuses
    every event a cycle stamped, so a run killed by a `SIGKILL`, a power cut, or
    a server shutdown cancelling the nightly task stranded its writes behind
    advice ("close it first") that no verb could carry out. `service.abandon_
    cycle` existed and nothing called it.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())

    stuck = runner.invoke(app, ["rollback", cycle.id, "--as", "owner"])
    assert stuck.exit_code == 1
    assert "still running" in stuck.stderr

    abandoned = _run_json("cycle-abandon", cycle.id)

    assert abandoned["status"] == "failed"
    assert abandoned["report"]["abandoned"] is True
    assert abandoned["report"]["abandoned_by"] == "human:owner"
    # And the writes it made are reachable again, which is the whole point.
    _run_json("rollback", cycle.id)
    assert _run_json("cycle-get", cycle.id)["status"] == "rolled_back"
    gone = runner.invoke(app, ["node", "get", node.id, "--as", "owner"])
    assert gone.exit_code == 1


def test_cycle_abandon_refuses_a_cycle_that_already_ended(fresh_db):
    """Not a general "close this" verb: re-closing would overwrite the record."""
    finished = _run_json("consolidate")["cycle"]

    result = runner.invoke(app, ["cycle-abandon", finished["id"], "--as", "owner"])

    assert result.exit_code == 1
    assert "already completed, not running" in result.stderr
    assert isinstance(result.exception, SystemExit)
    assert _run_json("cycle-get", finished["id"])["status"] == "completed"


def test_cycle_stop_records_the_instruction_and_closes_nothing(fresh_db):
    """The kill switch's verb — and it is neither `cycle-abandon` nor `rollback`.

    `service.request_stop` shipped with migration `0015` and no surface reached
    it, which is the defect this repo keeps re-committing: a door nothing opens.

    What it must *not* do is as load-bearing as what it does. The entry stays
    `running` (the run is expected to notice and close its own), and every write
    the run already made stands — a switch that also reverted would make "stop,
    look at what it did, then decide" impossible.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())

    stopped = _run_json("cycle-stop", cycle.id)

    assert stopped["status"] == "running", "a stop is an instruction, not a close"
    assert stopped["stop_requested"] is True
    assert stopped["stop_requested_by"] == "human:owner"
    assert stopped["stop_requested_at"] is not None
    # Nothing was reversed, and the journal keeps the stamp where a reader finds it.
    assert _run_json("node", "get", node.id)["state"] == "active"
    assert _run_json("cycle-get", cycle.id)["stop_requested_by"] == "human:owner"
    # And a stop is not an abandon: no report claims a human closed this entry.
    assert _run_json("cycle-get", cycle.id)["report"] is None


def test_cycle_stop_twice_keeps_the_first_asker_and_is_not_an_error(fresh_db):
    """A switch that raised on the second press would make a human doubt the first.

    That is the one moment which must not be ambiguous, so the second call is a
    no-op — exit 0, the same row back — and the journal keeps whoever actually
    stopped the night.
    """
    second = service.create_human("second", principal=owner())
    cycle = service.open_cycle(trigger="manual", principal=owner())

    first = _run_json("cycle-stop", cycle.id)
    again = runner.invoke(app, ["cycle-stop", cycle.id, "--as", second.id])

    assert again.exit_code == 0
    assert json.loads(again.stdout)["stop_requested_by"] == "human:owner"
    assert json.loads(again.stdout)["stop_requested_at"] == first["stop_requested_at"]


def test_cycle_stop_refuses_a_cycle_that_already_ended(fresh_db):
    """Nothing is left to obey it, and the stamp would name a run that never saw it.

    The refusal is asserted by its *own* sentence rather than by the shared
    "already completed, not running" prefix: `cycle-abandon` refuses the same row
    with the same prefix, so a `cycle-stop` mis-wired to `abandon_cycle` would
    pass a test that read only that much.
    """
    finished = _run_json("consolidate")["cycle"]

    result = runner.invoke(app, ["cycle-stop", finished["id"], "--as", "owner"])

    assert result.exit_code == 1
    assert "already completed, not running" in result.stderr
    assert "a stop is an instruction to a live run" in result.stderr
    assert isinstance(result.exception, SystemExit)
    assert _run_json("cycle-get", finished["id"])["stop_requested"] is False
    # And the refused stop closed nothing: the row still says how it really ended.
    assert _run_json("cycle-get", finished["id"])["report"]["jobs"] != []


def test_merge_nodes_over_the_cli(fresh_db):
    survivor, duplicate, citer = _claim("Alpha"), _claim("Alpha (dup)"), _claim("Cites")
    edge = _run_json("edge", "create", citer["id"], duplicate["id"], "--type", "supports")

    merged = _run_json("merge-nodes", duplicate["id"], "--into", survivor["id"])

    assert merged["into"]["id"] == survivor["id"]
    assert [node["id"] for node in merged["tombstones"]] == [duplicate["id"]]
    assert [row["id"] for row in merged["relinked"]] == [edge["id"]]
    assert [row["tombstone_id"] for row in merged["redirects"]] == [duplicate["id"]]

    # The read path is unchanged: the tombstone comes back and says where it went.
    tombstone = _run_json("node", "get", duplicate["id"])
    assert tombstone["state"] == "archived"
    assert tombstone["props"]["merged_into"] == survivor["id"]

    # And the reversal is the cycle, not `undo`: naming one of its events is
    # refused and says what to use instead, because reversing one row of a merge
    # would leave the other half standing.
    stamped = _run_json("events", "--cycle", merged["cycle_id"])["events"][0]
    refused = runner.invoke(app, ["undo", str(stamped["seq"]), "--as", "owner"])
    assert refused.exit_code == 1
    assert "Roll the cycle back instead." in refused.stderr
    assert isinstance(refused.exception, SystemExit)


def test_retype_over_the_cli_and_its_per_item_failures(fresh_db):
    """A batch keeps its successes on stdout and pays for its failures in the exit code.

    `ingest file`'s rule, which the curative tier did not follow: the envelope
    is printed whatever happened, each skipped item is named on stderr, and the
    exit code is 1 if any of them was — so a script reading only the code is not
    told a run succeeded when part of it did not happen.
    """
    node = _claim("Alpha")

    result = runner.invoke(
        app, ["retype", node["id"], "missing", "--type", "note", "--as", "owner"]
    )

    assert result.exit_code == 1
    retyped = json.loads(result.stdout)
    assert retyped["new_type"] == "note"
    assert retyped["transitioned"] == [node["id"]]
    assert [failure["id"] for failure in retyped["failed"]] == ["missing"]
    assert retyped["cycle_id"]
    # The reason is on stderr too: an exit code of 1 with a silent stderr breaks
    # the other half of the contract.
    assert "failed missing:" in result.stderr
    assert isinstance(result.exception, SystemExit)
    # The success still landed — that is what "never loses its successes" means.
    assert _run_json("node", "get", node["id"])["type"] == "note"


def test_a_retype_that_changed_nothing_does_not_exit_zero(fresh_db):
    """`nodum retype main --type note` accomplishes nothing and used to report success.

    It reported the refusal in `failed[]`, opened a curative cycle that closed
    `completed` with zero events, and exited **0** — the one thing a script
    reads. `ingest file` had set exit 1 in exactly this situation since Phase 4.
    """
    result = runner.invoke(app, ["retype", "main", "--type", "note", "--as", "owner"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["transitioned"] == []
    assert [failure["id"] for failure in payload["failed"]] == ["main"]
    assert "failed main:" in result.stderr
    assert isinstance(result.exception, SystemExit)
    # The cycle is still in the journal, and still says it changed nothing.
    assert _run_json("events", "--cycle", payload["cycle_id"])["events"] == []


def test_supersede_edge_with_and_without_a_replacement(fresh_db):
    first, second = _claim("A"), _claim("B")
    edge = _run_json("edge", "create", first["id"], second["id"], "--type", "supports")

    result = _run_json("supersede-edge", edge["id"], "--confidence", "0.4")

    # Two facts, both recorded: when it stopped being true, and that it is gone.
    assert result["superseded"]["state"] == "archived"
    assert result["superseded"]["valid_to"]
    replacement = result["replacement"]
    # Every field the replacement does not name is inherited from the original.
    assert (replacement["src_id"], replacement["dst_id"], replacement["type"]) == (
        first["id"],
        second["id"],
        "supports",
    )
    assert replacement["confidence"] == 0.4
    assert replacement["props"]["supersedes"] == edge["id"]
    assert result["superseded"]["props"]["superseded_by"] == replacement["id"]

    # Naming no replacement option retires the edge with no successor at all.
    plain = _run_json("edge", "create", first["id"], second["id"], "--type", "relates_to")
    assert _run_json("supersede-edge", plain["id"])["replacement"] is None


def test_bulk_relink_previews_before_it_writes(fresh_db):
    first, second, third = _claim("A"), _claim("B"), _claim("C")
    edge = _run_json("edge", "create", first["id"], second["id"], "--type", "supports")

    preview = _run_json("bulk-relink", "--src", first["id"], "--to-dst", third["id"], "--dry-run")

    assert (preview["dry_run"], preview["cycle_id"]) == (True, None)
    assert [change["edge_id"] for change in preview["changes"]] == [edge["id"]]
    assert _run_json("edge", "list", "--node", third["id"])["count"] == 0

    applied = _run_json("bulk-relink", "--src", first["id"], "--to-dst", third["id"])

    assert applied["dry_run"] is False
    assert applied["cycle_id"]
    assert _run_json("edge", "list", "--node", third["id"])["count"] == 1


def test_a_bulk_relink_that_refused_an_edge_does_not_exit_zero(fresh_db):
    """The batch rule, now that `skipped[]` is a failure list and nothing else.

    `retype`'s defect, one command along. The exemption `bulk-relink` held was
    that its `skipped[]` mixed a diff annotation ("nothing would change on this
    edge") with real refusals under one field called `error`, so an exit code
    derived from it would have been wrong more often than right. `unchanged`
    took the annotation, `skipped` kept the refusals — and until this, a run
    that could not relink an edge still reported success to the one thing a
    script reads.
    """
    first, second, third = _claim("A"), _claim("B"), _claim("C")
    moving = _run_json("edge", "create", first["id"], second["id"], "--type", "supports")
    # Already saying what the relink asks for, so repointing `moving` onto it is
    # the duplicate refusal — and this edge itself is an `unchanged` entry.
    _run_json("edge", "create", first["id"], third["id"], "--type", "supports")

    result = runner.invoke(
        app, ["bulk-relink", "--src", first["id"], "--to-dst", third["id"], "--as", "owner"]
    )

    assert result.exit_code == 1
    relinked = json.loads(result.stdout)
    # The envelope is still on stdout, printed before the exit code was decided.
    assert relinked["changes"] == []
    assert [failure["id"] for failure in relinked["skipped"]] == [moving["id"]]
    assert f"failed {moving['id']}:" in result.stderr
    assert isinstance(result.exception, SystemExit)

    # And the annotation is not a failure: a run whose only non-change is
    # `unchanged` accomplished exactly what was asked and exits 0.
    unchanged = _run_json("bulk-relink", "--src", first["id"], "--to-type", "supports")
    assert unchanged["skipped"] == []
    assert len(unchanged["unchanged"]) == 2


def test_a_bulk_relink_dry_run_exits_zero_even_when_it_would_refuse(fresh_db):
    """A rehearsal's `skipped[]` is a prediction: nothing was attempted, nothing lost.

    The one place this command departs from the flat batch rule. Every check a
    real run makes runs on the dry run too, so the diff tells the truth about
    what *would* be refused — but exit 1 there would report a failure that has
    not happened, on a command whose whole job is to be read before it is run.
    """
    first, second, third = _claim("A"), _claim("B"), _claim("C")
    _run_json("edge", "create", first["id"], second["id"], "--type", "supports")
    _run_json("edge", "create", first["id"], third["id"], "--type", "supports")

    result = runner.invoke(
        app,
        [
            "bulk-relink",
            "--src",
            first["id"],
            "--to-dst",
            third["id"],
            "--dry-run",
            "--as",
            "owner",
        ],
    )

    assert result.exit_code == 0
    preview = json.loads(result.stdout)
    assert preview["dry_run"] is True
    assert len(preview["skipped"]) == 1
    # Nothing "failed": naming a prediction on stderr under that word would say
    # an attempt was made and lost.
    assert "failed" not in result.stderr


def test_rollback_takes_a_curative_cycle_back_whole(fresh_db):
    survivor, duplicate = _claim("Alpha"), _claim("Alpha (dup)")
    merged = _run_json("merge-nodes", duplicate["id"], "--into", survivor["id"])
    assert _run_json("node", "get", duplicate["id"])["state"] == "archived"

    rolled = _run_json("rollback", merged["cycle_id"])

    assert rolled["dry_run"] is False
    assert rolled["conflicts"] == []
    assert rolled["rollback_cycle_id"]
    assert _run_json("node", "get", duplicate["id"])["state"] == "active"

    taken_back = _run_json("cycle-get", merged["cycle_id"])
    assert taken_back["status"] == "rolled_back"
    assert taken_back["rolled_back_by"] == rolled["rollback_cycle_id"]


def test_a_refused_rollback_prints_its_conflicts_as_one_json_object(fresh_db):
    """The one refusal in this CLI that is a list rather than a sentence.

    `RollbackConflict`'s message names the first few rows and drops the actor
    and the cycle behind the later work entirely, so the structured list is what
    the command prints — and the message still goes to stderr with exit 1, like
    every other refusal here.
    """
    node = _claim("Alpha")
    retyped = _run_json("retype", node["id"], "--type", "note")
    # Work outside the cycle touches a row the cycle wrote.
    _run_json("node", "update", node["id"], "--content", "moved on")

    result = runner.invoke(app, ["rollback", retyped["cycle_id"], "--as", "owner"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "cannot roll back cycle" in result.stderr
    refusal = json.loads(result.stdout)["error"]
    assert refusal["type"] == "RollbackConflict"
    (conflict,) = refusal["conflicts"]
    assert conflict == {
        "kind": "node",
        "row_id": node["id"],
        "cycle_event_seq": conflict["cycle_event_seq"],
        "cycle_event_op": "node.retype",
        "conflicting_seq": conflict["conflicting_seq"],
        "conflicting_op": "node.update",
        "conflicting_actor": "human:owner",
        "conflicting_cycle_id": None,
    }
    assert conflict["conflicting_seq"] > conflict["cycle_event_seq"]
    # Nothing was written: the node still carries the later edit.
    assert _run_json("node", "get", node["id"])["content"] == "moved on"

    # The same question asked as a dry run is an answer, not a refusal.
    preview = _run_json("rollback", retyped["cycle_id"], "--dry-run")
    assert [row["row_id"] for row in preview["conflicts"]] == [node["id"]]
    assert preview["rollback_cycle_id"] is None


#: Values for the required options a command parses before `--as` is looked at.
#: None of them mean anything — the principal is refused first — they exist only
#: to get past Click's own required-parameter check and reach the command body.
_PLACEHOLDER_OPTIONS = {
    "--type": "note",
    "--reason": "why",
    "--into": "n_survivor",
    "--name": "placeholder",
    "--mime": "text/plain",
    "--size": "10",
    "--password": "placeholder-password",
}

#: Required arguments Click type-converts *before* the command body runs. A
#: non-numeric placeholder for `diff a b` is a usage error (exit 2), which would
#: hide the refusal this sweep is about behind Click's own complaint. `level`
#: is the same category one step later: `grant` narrows it against the grant
#: levels in the command body, and a "placeholder" value would be refused there
#: before `--as` is resolved — so it gets a valid level instead.
_PLACEHOLDER_ARGUMENTS = {"a": "1", "b": "2", "level": "read"}

#: Required arguments naming a file the command reads before it resolves `--as`.
#: `edge create-batch` reports the missing file first — correctly, and through
#: `_run` — so the sweep hands it a real one rather than asserting a different
#: message for the one command whose reads come first.
_FILE_ARGUMENTS = {"suggestions_file"}


def _as_taking_invocations(suggestions_file: Path) -> dict[str, list[str]]:
    """Every command in the tree declaring `--as`, as a runnable argv.

    Enumerated from ``schema-dump`` rather than hand-listed, which is the whole
    point of the sweep: a hand-list is a second copy of the command surface, and
    the copy is what goes stale. The list this replaced named nine commands; the
    CLI has sixty-four that take the option, and a tenth added tomorrow would
    have joined the fifty-five nothing was checking.
    """

    def walk(prefix: list[str], commands: list[dict]):
        for command in commands:
            path = [*prefix, command["name"]]
            subcommands = command.get("subcommands")
            if subcommands:
                yield from walk(path, subcommands)
                continue
            params = command["params"]
            if not any(p["kind"] == "option" and "--as" in p["flags"] for p in params):
                continue
            argv = list(path)
            for param in params:
                if param["kind"] == "argument" and param["required"]:
                    argv.append(
                        str(suggestions_file)
                        if param["name"] in _FILE_ARGUMENTS
                        else _PLACEHOLDER_ARGUMENTS.get(param["name"], "placeholder")
                    )
                elif param["kind"] == "option" and param["required"]:
                    flag = param["flags"][0]
                    if flag == "--as":
                        continue
                    assert flag in _PLACEHOLDER_OPTIONS, (
                        f"nodum {' '.join(path)} gained a required option {flag}; give it a "
                        "placeholder so this sweep can still reach the refusal behind it"
                    )
                    argv += [flag, _PLACEHOLDER_OPTIONS[flag]]
            yield " ".join(path), argv

    return dict(walk([], _run_json("schema-dump")["commands"]))


def _assert_is_the_contracts_refusal(result, expected_line: str, where: str) -> None:
    """Assert the CLI's one failure shape: exit 1, one line on stderr, nothing escaped.

    ``result.exception`` is what carries the weight. Asserting ``"Traceback" not
    in result.output`` — the spelling used around this file — cannot fail under
    ``CliRunner``: Typer prints its Rich traceback from ``sys.excepthook``, and
    the runner catches the exception before any hook runs, so an escaped
    ``KeyError`` and a clean refusal both come back with an empty stdout and
    ``exit_code == 1``. The two are told apart only by whether what came back is
    the ``SystemExit`` a ``typer.Exit`` becomes.
    """
    assert isinstance(result.exception, SystemExit), (
        f"{where}: {result.exception!r} escaped the error boundary — "
        "in a terminal that is the Rich traceback the contract forbids"
    )
    assert result.exit_code == 1, where
    assert result.stdout == "", where
    assert result.stderr.splitlines() == [expected_line], where


def test_an_unknown_principal_is_one_readable_line_on_every_command_taking_as(fresh_db, tmp_path):
    """`--as` is resolved in the argument list, before `_run` is even entered.

    That is why an unknown account once printed a full traceback: the resolution
    sat outside the error boundary the command's own call goes through, and
    `_principal` routing *through* `_run` is what fixed it at every call site at
    once, wherever in an argument list it sits.

    The sweep is over the whole surface because the bug was never about one
    command: it was about a position in a call, and any command written tomorrow
    can take that position. Enumerating from `schema-dump` is what makes the
    coverage total — a hand-list would have to be remembered.
    """
    suggestions = tmp_path / "suggestions.json"
    suggestions.write_text("[]", encoding="utf-8")
    invocations = _as_taking_invocations(suggestions)

    # A recursion that quietly stops at the top level would leave the groups —
    # `node`, `review`, `human`, `asset`, … — silently unswept, which is the
    # shape of an enumeration bug that looks like a passing test.
    assert {
        "node list",
        "edge create",
        "review queue",
        "human list",
        "agent list",
        "asset get",
        "ingest file",
        "llm status",
        "consolidate",
    } <= set(invocations), sorted(invocations)
    assert len(invocations) >= 60, sorted(invocations)

    for label, argv in invocations.items():
        result = runner.invoke(app, [*argv, "--as", "human:nope"])

        _assert_is_the_contracts_refusal(result, "unknown human account: nope", label)


def test_the_error_boundary_does_not_launder_a_real_bug(fresh_db, monkeypatch):
    """A defect must stay a defect; only what a caller can provoke is a message.

    The tempting fix for a refused actor is a wider `except` around the call —
    `LookupError` would catch `UnknownPrincipal` and read like a tidy
    generalisation. It would also catch every `KeyError` raised by a genuine bug
    beneath it, and turn each one into "unknown human account" with exit 1: a
    command that silently reports a typo for a fault in the graph layer, on every
    command taking `--as`. `RuntimeError` is the same trap one step along, and is
    why `_expand_user` translates `Path.expanduser`'s at the argument it belongs
    to rather than `_run` catching the class wholesale.

    So the property is stated from the other side: these must **escape**.
    """
    for target_module, attribute, error in (
        (auth, "owner_principal", KeyError("a bug in principal loading")),
        (service, "list_nodes", KeyError("a bug in the read path")),
        (service, "list_nodes", RuntimeError("a bug in the read path")),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(
                target_module,
                attribute,
                lambda *args, _error=error, **kwargs: (_ for _ in ()).throw(_error),
            )

            result = runner.invoke(app, ["node", "list", "--as", "owner"])

            where = f"{attribute} raising {type(error).__name__}"
            assert isinstance(result.exception, type(error)), f"{where}: {result.exception!r}"
            assert result.stderr == "", f"{where}: a bug was rendered as a friendly message"


def test_an_unreadable_directory_is_one_readable_line_not_a_traceback(fresh_db, tmp_path):
    """`ingest file`'s path expansion is the same argument-list position, unswept.

    `_ingest_sources` walks the directory arguments *beside* the command's own
    `_run`, not through it, so `iterdir` on a directory the process may not read
    climbed out as a `PermissionError` and printed the Rich traceback the
    contract forbids — the identical mechanism `_principal` and `_read_content`
    were each moved inside the boundary for, at the one call site left holding it.
    """
    blocked = tmp_path / "blocked"
    (blocked / "sub").mkdir(parents=True)
    blocked.chmod(0o000)
    try:
        result = runner.invoke(app, ["ingest", "file", str(blocked), "--as", "owner"])
    finally:
        # Restored whatever the assertions do: pytest cannot clean up a temp
        # directory it is not allowed to list.
        blocked.chmod(0o755)

    _assert_is_the_contracts_refusal(result, f"Permission denied: {blocked}", "ingest file")


def test_an_unresolvable_home_directory_is_one_readable_line_not_a_traceback(fresh_db):
    """`Path.expanduser` raises `RuntimeError` for a `~user` that does not exist.

    A typo in an argument, and the one error here that is *not* an `OSError`, so
    it escaped the boundary even once the walk was routed through it. It is
    translated to `ValueError` at the expansion rather than caught as
    `RuntimeError` by `_run`, which would launder real bugs — the sibling test
    above holds that line.
    """
    for argv in (
        ["ingest", "file", "~nobodyhere12345/notes.md"],
        ["ingest", "file", "~nobodyhere12345/a.md", "~nobodyhere12345/b.md"],
    ):
        result = runner.invoke(app, [*argv, "--as", "owner"])

        _assert_is_the_contracts_refusal(
            result,
            f"cannot resolve the home directory in path: {argv[2]}",
            " ".join(argv),
        )


def test_a_disabled_human_is_refused_the_same_way(fresh_db):
    """The sibling branch: `PrincipalDisabled` is an OSError, and still one line."""
    alice = _run_json("human", "create", "alice")
    _run_json("human", "disable", alice["id"])

    result = runner.invoke(app, ["consolidate", "--as", alice["id"]])

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [f"human account is disabled: {alice['id']}"]
    assert isinstance(result.exception, SystemExit)


def test_consolidate_is_refused_on_a_scope_the_gardener_holds_nothing_on(fresh_db):
    """The grant that has to be there is the gardener's, not the asker's.

    A human passes `require_review` unconditionally, so no curative verb on this
    surface can be refused for want of the *caller's* grant. The gardener's is
    the one that bites: migration `0014` gives it `main` and `meta` and nothing
    else, so every space made since needs an explicit grant.

    The refusal names that grant. It used to be the Q13 non-oracle "unknown
    space: <id>", which is the honest answer when the *caller* holds nothing and
    a false one here — the caller just created the space and can see it in every
    picker; it is the gardener that cannot. That sentence also outlived the
    command, in a `failed` journal row whose message the dream journal splices
    into the entry's headline.
    """
    _run_json("space-create", "research")

    result = runner.invoke(app, ["consolidate", "--scope", "research", "--as", "owner"])

    assert result.exit_code == 1
    assert "nodum grant builtin-gardener research edit" in result.stderr
    assert "unknown space" not in result.stderr
    assert isinstance(result.exception, SystemExit)
    journal = _run_json("cycle-list")["cycles"][0]
    assert journal["status"] == "failed"
    assert "unknown space" not in journal["report"]["failed"][0]["error"]


def test_consolidate_stops_when_the_gardener_is_disabled(fresh_db):
    """Disabling the gardener is the supported way to stop it running."""
    _run_json("agent", "disable", GARDENER_AGENT_ID)

    result = runner.invoke(app, ["consolidate", "--as", "owner"])

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [f"agent account is disabled: {GARDENER_AGENT_ID}"]
    assert isinstance(result.exception, SystemExit)


def test_every_curative_refusal_is_one_line_and_never_a_traceback(fresh_db):
    node, other = _claim("Alpha"), _claim("Beta")
    proposed = service.create_edge(node["id"], other["id"], "supports", principal=agent("bot"))

    for command, expected in (
        (["merge-nodes", node["id"], "--into", node["id"]], "into itself"),
        (["retype", node["id"], "--type", "space"], "cannot retype anything into"),
        (["supersede-edge", proposed.id], "cannot supersede an edge in state 'proposed'"),
        (["bulk-relink", "--to-type", "supports"], "needs a selector"),
        (["bulk-relink", "--src", node["id"]], "needs changes"),
        (["cycle-get", "missing"], "consolidation cycle not found"),
        (["rollback", "missing"], "consolidation cycle not found"),
        (["consolidate", "--job", "nope"], "unknown consolidation job"),
    ):
        result = runner.invoke(app, [*command, "--as", "owner"])

        assert result.exit_code == 1, command
        assert expected in result.stderr, command
        assert isinstance(result.exception, SystemExit), command


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


# ── The smart verbs: ask, summarize, search --nl, llm status ──────────────────
#
# The autouse `_no_llm_provider` fixture pins the provider absent, which is the
# shipped default (`NODUM_LLM_MODEL` unset means no provider), so the tests that
# want a completion inject a fake and the rest exercise the degraded path they
# would meet on a real install. No test asserts on model output text.


class _FakeLLM:
    """A provider that replays scripted completions."""

    provider_id = "fake://provider"
    model_id = "fake-model"
    context_tokens = 4096
    thinking = llm.DEFAULT_THINKING
    thinking_applied = True

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.structured_mode = llm.STRUCTURED_JSON_SCHEMA

    def estimate_prompt_tokens(self, messages, *, schema=None) -> int:
        return llm.estimate_prompt_tokens(messages)

    def output_reservation(self, max_output_tokens: int) -> int:
        """The real provider's rule: a reservation capped at a share of the
        window. An identity function here would model an endpoint that does not
        exist, and would leave the prompt no room at the shipped ceiling."""
        share = int(self.context_tokens * llm.OUTPUT_RESERVATION_FRACTION)
        return max(1, min(max_output_tokens, share))

    def chat(self, messages, *, schema=None, max_output_tokens, timeout, thinking=None):
        self.calls.append({"messages": list(messages), "schema": schema, "thinking": thinking})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _completion(payload: dict, *, finish_reason: str = "stop"):
    return llm.Completion(
        text=json.dumps(payload),
        prompt_tokens=100,
        output_tokens=20,
        finish_reason=finish_reason,
        model_id="fake-model",
        provider_id="fake://provider",
        context_tokens=4096,
        latency_ms=7,
    )


def _seed_note(fresh_db) -> str:
    return service.create_node(
        type="note",
        title="Log compaction",
        content="A compacted topic keeps the newest value per key, so it works as a state store.",
        principal=owner(),
    ).id


def test_ask_prints_one_object_with_citations_and_writes_nothing(fresh_db):
    node_id = _seed_note(fresh_db)
    before = max(event.seq for event in service.list_events(owner(), limit=5000))
    llm.set_provider(
        _FakeLLM(_completion({"answer": "It keeps the newest value.", "cited": ["1"]}))
    )

    payload = _run_json("ask", "compacted topic state store")

    assert payload["answered"] is True
    assert [citation["node_id"] for citation in payload["citations"]] == [node_id]
    assert payload["used"]["model_id"] == "fake-model"
    after = max(event.seq for event in service.list_events(owner(), limit=5000))
    assert after == before


def test_an_unanswered_question_is_exit_zero_and_a_stated_refusal(fresh_db):
    """Not answering is an outcome, not a failure. Exit 1 would tell a script the
    command broke, and the envelope on stdout is the whole answer."""
    _seed_note(fresh_db)
    llm.set_provider(_FakeLLM(_completion({"answer": "Yes, certainly.", "cited": ["id=n0"]})))

    result = runner.invoke(app, ["ask", "compacted topic state store", "--as", "owner"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["answered"] is False
    assert payload["answer"] is None
    assert payload["unresolved"] == ["id=n0"]
    assert payload["refusal"]


def test_ask_with_no_provider_names_the_variable_to_set(fresh_db, monkeypatch):
    monkeypatch.delenv(llm.ENV_MODEL, raising=False)
    llm.reset_provider()
    _seed_note(fresh_db)

    payload = _run_json("ask", "compacted topic state store")

    assert payload["answered"] is False
    assert "NODUM_LLM_MODEL" in payload["refusal"]


def test_a_blank_question_is_one_line_on_stderr_and_exit_one(fresh_db):
    llm.set_provider(_FakeLLM(_completion({"answer": "x", "cited": ["1"]})))
    result = runner.invoke(app, ["ask", "   ", "--as", "owner"])
    assert result.exit_code == 1
    assert result.stdout.strip() == ""


def test_summarize_prints_one_object_and_writes_nothing(fresh_db):
    node_id = _seed_note(fresh_db)
    before = max(event.seq for event in service.list_events(owner(), limit=5000))
    llm.set_provider(_FakeLLM(_completion({"summary": "Compaction, briefly.", "cited": ["1"]})))

    payload = _run_json("summarize", node_id)

    assert payload["summarized"] is True
    assert [citation["node_id"] for citation in payload["citations"]] == [node_id]
    after = max(event.seq for event in service.list_events(owner(), limit=5000))
    assert after == before


def test_summarizing_a_node_that_does_not_exist_is_the_ordinary_refusal(fresh_db):
    result = runner.invoke(app, ["summarize", "nope", "--as", "owner"])
    assert result.exit_code == 1
    assert result.stdout.strip() == ""


def test_search_nl_adds_a_rewrite_and_plain_search_does_not(fresh_db):
    node_id = _seed_note(fresh_db)
    llm.set_provider(_FakeLLM(_completion({"terms": ["compacted", "topic"]})))

    rewritten = _run_json("search", "What did I write about compacted topics?", "--nl")
    assert rewritten["rewrite"]["applied"] is True
    assert rewritten["query"] == "compacted topic"
    assert [hit["node_id"] for hit in rewritten["hits"]] == [node_id]

    plain = _run_json("search", "compacted")
    assert "rewrite" not in plain


def test_search_nl_without_a_provider_still_searches(fresh_db, monkeypatch):
    monkeypatch.delenv(llm.ENV_MODEL, raising=False)
    llm.reset_provider()
    node_id = _seed_note(fresh_db)

    payload = _run_json("search", "compacted topic state store", "--nl")

    assert payload["rewrite"]["applied"] is False
    assert "NODUM_LLM_MODEL" in payload["rewrite"]["refusal"]
    assert [hit["node_id"] for hit in payload["hits"]] == [node_id]


def test_llm_status_reports_an_unconfigured_install_without_failing(fresh_db, monkeypatch):
    """No provider is a perfectly good install — the smart features are off."""
    monkeypatch.delenv(llm.ENV_MODEL, raising=False)
    llm.reset_provider()

    payload = _run_json("llm", "status")

    assert payload["configured"] is False
    assert payload["reachable"] is None, "nothing was configured, so nothing was asked"
    assert "NODUM_LLM_MODEL" in payload["detail"]


def test_llm_status_probes_a_configured_provider(fresh_db):
    provider = _FakeLLM(_completion({"pong": True}))
    llm.set_provider(provider)

    payload = _run_json("llm", "status")

    assert (payload["configured"], payload["reachable"]) == (True, True)
    assert payload["model"] == "fake-model"
    assert len(provider.calls) == 1
    assert payload["probe_ms"] is not None


def test_llm_status_separates_configured_from_reachable(fresh_db):
    """The two facts `nodum.llm` deliberately keeps apart. A configured provider
    that cannot be reached is not an unconfigured one, and reporting it as one
    would send a human to edit an environment variable that is already right."""
    llm.set_provider(_FakeLLM(llm.ProviderUnavailable("connection refused: localhost:11434")))

    payload = _run_json("llm", "status")

    assert payload["configured"] is True
    assert payload["reachable"] is False
    assert "connection refused" in payload["detail"]


def test_llm_status_can_decline_the_probe(fresh_db):
    provider = _FakeLLM(_completion({"pong": True}))
    llm.set_provider(provider)

    payload = _run_json("llm", "status", "--no-probe")

    assert payload["configured"] is True
    assert payload["reachable"] is None
    assert provider.calls == [], "--no-probe spends nothing"


def _declared_help() -> dict[str, str]:
    """Every help string this CLI declares, keyed by where it is declared.

    The *declared* strings, read off the command objects — never the rendered
    ``--help`` panel, which is Rich output wrapped to the terminal's width and
    styled by its colour support, so an assertion on it tests the runner's
    environment rather than the CLI. ``schema-dump`` cannot serve here: it
    carries an option's help and an argument's name alone, and the sentence
    under test is on an argument.
    """
    texts: dict[str, str] = {}

    def walk(prefix: str, command) -> None:
        texts[prefix] = command.help or command.short_help or ""
        for param in command.params:
            texts[f"{prefix} <{param.name}>"] = getattr(param, "help", "") or ""
        for name, sub in (getattr(command, "commands", None) or {}).items():
            walk(f"{prefix} {name}".strip(), sub)

    walk("nodum", typer.main.get_command(app))
    return texts


def test_no_help_text_anywhere_still_states_the_conjunctive_rule():
    """`search --help` told a reader "terms are ANDed", which the quorum ended.

    Swept over the whole adapter rather than the one string that was wrong: the
    rule was stated in two places (here and the search view's empty state) and
    finding the second one was luck. A sweep is what makes the third one a test
    failure.
    """
    texts = _declared_help()
    query_help = texts.get("nodum search <query>")
    assert query_help, "the search query's help is gone; this sweep guards nothing"
    # Word-bounded: "landed" is not a claim about the matcher, and `ingest
    # file`'s help says it four times.
    stale = re.compile(r"\bANDed\b|\bevery term\b", re.I)
    offenders = {where: text for where, text in texts.items() if stale.search(text)}
    assert offenders == {}, f"help text still states the conjunctive rule: {offenders}"
    assert "quorum" in query_help.casefold(), "and it should say what replaced it"


# ── The docs name commands that exist ─────────────────────────────────────────

#: Every prose file that spells commands at a reader. `schema-dump` is the
#: authority they are checked against, which is the point: the CLI describes
#: itself, so the docs can be checked against the surface rather than against a
#: second list of it. The split docs are named explicitly so the coverage is
#: visible: the `docs` directory sweep already includes them, and a future
#: split must add its new files here too or the sweep silently stops covering
#: them.
DOC_SOURCES = ("README.md", "AGENTS.md", "docs", "docs/decisions.md", "docs/http-api.md")

#: Hyphenated lowercase tokens the docs write in backticks that are *not*
#: commands — a CSP directive, an HTTP header, a package, an agent id, a CI job.
#: They share `cycle-rollback`'s exact shape, which is why the check needs them
#: named; the test asserts every one of them is still in the docs, so an entry
#: that stops being needed fails here instead of quietly widening the check.
NOT_COMMANDS = frozenset(
    {
        "build-and-publish",  # the release workflow's job name
        "builtin-gardener",  # the internal agent's id (`nodum grant` takes it)
        "content-disposition",  # an HTTP response header
        "cross-space",  # prose
        "deepseek-v4-flash",  # a model name (NODUM_LLM_MODEL), not a verb
        # ollama library model ids, named in the docs because they are the
        # evidence for why `profile_for` matches an exact hosted id: each one
        # shares the `deepseek-` prefix with a hosted API and is served locally.
        "deepseek-coder",
        "deepseek-coder-v2",
        "deepseek-llm",
        "deepseek-r1",
        "deepseek-v2",
        "deepseek-v3",
        "faster-whisper",  # the audio extra's package
        "no-store",  # a Cache-Control directive
        "off-only",  # a rung of the reasoning-capability ladder in `nodum.llm`
        "script-src",  # a CSP directive
        # the systemd component whose `D /tmp …` rule empties the directory on
        # boot — named in the docs as the reason the embedding cache must not
        # default to temporary storage.
        "systemd-tmpfiles",
    }
)

#: A backticked token shaped like a command name: lowercase, hyphenated. The
#: shape is what makes a wrong one invisible — `cycle-rollback` reads exactly
#: like `cycle-abandon` and `cycle-list` beside it.
_COMMAND_SHAPED = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")

#: An inline code span or a fenced block — the only places a command name is
#: spelled. Scanning prose instead would trip over every sentence that says
#: "nodum is a…", which is not a command and never was.
_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]+`", re.S)

#: `nodum <command> [<subcommand>]` inside one of those spans. The trailing `*`
#: is captured rather than cut off so a family reference (`nodum space-*`) can be
#: recognised as one and skipped instead of resolving to `space-`.
_INVOCATION = re.compile(r"\bnodum\s+([a-z][a-z0-9-]*\*?)(?:\s+([a-z][a-z0-9-]*\*?))?")


def _doc_files() -> list[Path]:
    """Every documentation file a reader could copy a command out of."""
    repo = Path(__file__).resolve().parent.parent
    files: list[Path] = []
    for source in DOC_SOURCES:
        target = repo / source
        files.extend(sorted(target.rglob("*.md")) if target.is_dir() else [target])
    files.append(repo / "docs" / "llms.txt")
    return [path for path in files if path.exists()]


def _command_tree() -> dict[str, dict]:
    """The CLI's own command tree, keyed by name, from ``schema-dump``."""

    def index(commands: list[dict]) -> dict[str, dict]:
        return {c["name"]: index(c.get("subcommands", [])) for c in commands}

    return index(_run_json("schema-dump")["commands"])


def test_every_command_the_docs_name_exists():
    """A documented command that does not resolve is worse than an undocumented one.

    ``docs/commands.md`` told a reader that "an interrupted run is still one a
    ``cycle-rollback`` can take back". There is no ``cycle-rollback``: the verb
    is ``nodum rollback``, and the name appears nowhere in ``nodum/``. It reads
    like a command because it is shaped like three real ones sitting beside it
    (``cycle-list``, ``cycle-get``, ``cycle-abandon``), which is exactly why
    nobody caught it — and a reader who types it gets "No such command", after
    which the paragraph's actual advice is worth nothing.

    Two passes, because the docs spell a command two ways. Full invocations
    (``nodum grant builtin-gardener <space> edit``) are checked head and
    subcommand against the tree. Bare, command-shaped names in backticks
    (``space-list``, ``bulk-relink``) are checked against every name in the tree
    at any depth, since the docs write ``token-rotate`` for ``agent
    token-rotate``. Only code spans are read: prose says "nodum is a DB-native
    knowledge graph", and "is" is not a command.
    """
    tree = _command_tree()
    every_name = set(tree)
    for group in tree.values():
        every_name |= set(group)

    unknown: list[str] = []
    seen_exemptions: set[str] = set()
    for path in _doc_files():
        text = path.read_text(encoding="utf-8")
        for span in _CODE_SPAN.finditer(text):
            body = span.group(0)
            for match in _INVOCATION.finditer(body):
                head, sub = match.group(1), match.group(2)
                # `pipx install nodum` on the line above a real invocation, and
                # `nodum space-*`, which names a family rather than a command.
                if head == "nodum" or head.endswith("*"):
                    continue
                sub = None if sub and sub.endswith("*") else sub
                if head not in tree:
                    unknown.append(f"{path.name}: nodum {head}")
                elif tree[head] and sub and sub not in tree[head]:
                    unknown.append(f"{path.name}: nodum {head} {sub}")
            token = body.strip("`").strip().split(" ")[0] if "\n" not in body else ""
            if not _COMMAND_SHAPED.match(token):
                continue
            if token in NOT_COMMANDS:
                seen_exemptions.add(token)
            elif token not in every_name:
                unknown.append(f"{path.name}: `{token}`")

    assert not unknown, f"the docs name commands that do not exist: {sorted(set(unknown))}"
    assert seen_exemptions == set(NOT_COMMANDS), (
        f"NOT_COMMANDS lists names the docs no longer use: {sorted(NOT_COMMANDS - seen_exemptions)}"
    )
