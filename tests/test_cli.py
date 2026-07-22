"""CLI smoke tests: every command emits one parseable JSON object on stdout."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from nodum.cli import app

runner = CliRunner()


def _run_json(*args, input_text=None):
    result = runner.invoke(app, list(args), input=input_text)
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
    proposed = _run_json(
        "node", "create", "--type", "note", "--title", "bot", "--actor", "agent:test"
    )
    assert proposed["state"] == "proposed"
    accepted = _run_json("accept", proposed["id"])
    assert accepted["state"] == "active"
    archived = _run_json("archive", accepted["id"])
    assert archived["state"] == "archived"


def test_invalid_transition_exits_1(fresh_db):
    node = _run_json("node", "create", "--type", "note", "--title", "active")
    result = runner.invoke(app, ["accept", node["id"]])
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
    result = runner.invoke(app, ["node", "get", "missing"])
    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_bad_set_pair_exits_1(fresh_db):
    result = runner.invoke(app, ["node", "create", "--type", "note", "--set", "nokey"])
    assert result.exit_code == 1
    assert "--set expects key=value" in result.stderr
