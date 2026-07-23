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

    runs = _run_json("projector", "run")
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
    result = runner.invoke(
        app, ["edge", "create-batch", "-", "--actor", "agent:researcher"], input=suggestions
    )
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.stdout)
    assert outcome["created"][0]["state"] == "proposed"
    assert outcome["failed"][0]["index"] == 1


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
    version = _run_json(
        "node", "update", note["id"], "--content", "bot rewrite", "--actor", "agent:researcher"
    )
    assert version["state"] == "proposed"

    queue = _run_json("review", "queue", "--kind", "update")
    assert queue["proposals"][0]["id"] == str(version["id"])

    accepted = _run_json("accept", str(version["id"]))
    assert accepted["state"] == "applied"
    assert _run_json("node", "get", note["id"])["content"] == "bot rewrite"


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
    assert "data_base64" not in rendition or rendition["data_base64"] is None

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

    result = runner.invoke(app, ["asset", "rendition", asset["hash"]])
    assert result.exit_code == 1
    assert "only supported for image assets" in result.stderr
