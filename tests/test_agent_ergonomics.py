"""Agent-ergonomics tests — multi-target get, filters, tags, titles, batch ops.

Covers the service layer (get filters, get_many, tag search, batch writes,
derived titles), the CLI shaping surface (snippets, --fields, --max-body-chars,
--batch, skill install), and the matching API query params.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nodum import metamodel, service
from nodum.cli import app as cli_app
from nodum.models import derive_title

# ── Service: get neighbourhood filters (gap 4) ───────────────────────────────


def _note_pair_with_edges() -> tuple:
    """Two notes linked both ways plus an inbound cite, for direction tests."""
    one = service.add_node("Note", "claim one", data={"role": "claim"})
    two = service.add_node("Note", "claim two", data={"role": "claim"})
    service.add_edge("supports", one.uuid, two.uuid)
    service.add_edge("refines", two.uuid, one.uuid)
    return one, two


def test_get_direction_filter() -> None:
    """``direction`` picks outbound, inbound, or both incident edges."""
    one, two = _note_pair_with_edges()
    out = service.get(one.uuid, direction="out")
    assert {edge.kind for edge in out.edges} == {"supports"}
    inbound = service.get(one.uuid, direction="in")
    assert {edge.kind for edge in inbound.edges} == {"refines"}
    both = service.get(one.uuid)
    assert {edge.kind for edge in both.edges} == {"supports", "refines"}
    assert service.get(two.uuid, direction="both").edges != []


def test_get_edge_kind_filter() -> None:
    """``edge_kinds`` restricts which incident edges are returned."""
    one, _two = _note_pair_with_edges()
    filtered = service.get(one.uuid, edge_kinds=["refines"])
    assert {edge.kind for edge in filtered.edges} == {"refines"}


def test_get_rejects_bad_direction() -> None:
    """An unknown direction is a validation error."""
    one, _two = _note_pair_with_edges()
    with pytest.raises(metamodel.ValidationError):
        service.get(one.uuid, direction="sideways")


# ── Service: multi-target get (gap 1) ─────────────────────────────────────────


def test_get_many_partial_success() -> None:
    """``get_many`` resolves what it can; misses and bad UUIDs land in failed."""
    person = service.add_node("Person", "Ada Lovelace")
    missing = uuid.uuid4()
    result = service.get_many([person.uuid, missing, "not-a-uuid"])
    assert result.targets == [str(person.uuid), str(missing), "not-a-uuid"]
    assert [entry.node.uuid for entry in result.nodes] == [person.uuid]
    assert len(result.failed) == 2
    assert result.failed[0].uuid == str(missing)
    assert "invalid UUID" in result.failed[1].error


def test_get_many_requires_targets() -> None:
    """An empty target list is a validation error."""
    with pytest.raises(metamodel.ValidationError):
        service.get_many([])


# ── Service: tags query surface (gap 3) ───────────────────────────────────────


def test_search_tag_filter_and_semantics() -> None:
    """``tags`` filters hits by JSONB containment on data.tags (AND semantics)."""
    tagged = service.add_node(
        "Note",
        "spaced repetition improves retention",
        data={"role": "claim", "tags": ["learning", "memory"]},
    )
    partial = service.add_node(
        "Note",
        "spaced repetition in classrooms",
        data={"role": "claim", "tags": ["learning"]},
    )
    service.add_node("Note", "spaced repetition untagged", data={"role": "claim"})

    learning = service.search("spaced", tags=["learning"])
    assert {hit.uuid for hit in learning.hits} == {tagged.uuid, partial.uuid}
    both = service.search("spaced", tags=["learning", "memory"])
    assert {hit.uuid for hit in both.hits} == {tagged.uuid}
    absent = service.search("spaced", tags=["no-such-tag"])
    assert absent.hits == []


# ── Service: derived titles (gap 2) ───────────────────────────────────────────


def test_title_derived_from_first_line() -> None:
    """The title is the first non-blank content line, capped at 80 chars."""
    node = service.add_node("Note", "\n\nFirst line here.\nSecond line.")
    assert node.title == "First line here."
    long_line = "x" * 120
    capped = service.add_node("Note", long_line)
    assert capped.title == "x" * 80
    assert derive_title("") == ""


# ── Service: batch writes (gap 1) ─────────────────────────────────────────────


def test_add_nodes_batch_partial_success() -> None:
    """A batch creates the valid items and reports the bad one with its error."""
    result = service.add_nodes_batch(
        [
            {"kind": "Person", "content": "Ada Lovelace", "data": {"born": 1815}},
            {"kind": "Ghost", "content": "unknown kind"},
            {"kind": "Note", "content": "a claim", "data": {"role": "claim"}},
        ]
    )
    assert result.operation == "add-batch"
    assert result.count == 3
    assert result.succeeded == 2
    assert result.failed == 1
    assert result.results[1].ok is False
    assert "unknown node kind" in result.results[1].error
    assert result.results[0].uuid is not None
    # The good items were really persisted.
    assert service.get(result.results[0].uuid).node.content == "Ada Lovelace"


def test_add_nodes_batch_dry_run_writes_nothing() -> None:
    """``dry_run`` validates every item and rolls the whole pass back."""
    result = service.add_nodes_batch([{"kind": "Person", "content": "Ada Lovelace"}], dry_run=True)
    assert result.dry_run is True
    assert result.succeeded == 1
    assert service.search("Lovelace").hits == []


def test_update_nodes_batch_partial_success() -> None:
    """A batch edit merges valid items and records the missing/bad ones."""
    note = service.add_node("Note", "draft", data={"role": "claim"})
    missing = uuid.uuid4()
    result = service.update_nodes_batch(
        [
            {"uuid": str(note.uuid), "content": "final", "data": {"confidence": 0.9}},
            {"uuid": str(missing), "content": "nope"},
            {"uuid": "not-a-uuid", "content": "nope"},
        ]
    )
    assert result.operation == "edit-batch"
    assert result.succeeded == 1
    assert result.failed == 2
    assert "not found" in result.results[1].error
    assert "invalid UUID" in result.results[2].error
    updated = service.get(note.uuid).node
    assert updated.content == "final"
    assert updated.data["confidence"] == 0.9
    assert updated.data["role"] == "claim"  # merge preserved untouched keys


def test_update_nodes_batch_dry_run_writes_nothing() -> None:
    """A dry-run edit validates against the stored node and rolls back."""
    note = service.add_node("Note", "draft", data={"role": "claim"})
    result = service.update_nodes_batch(
        [{"uuid": str(note.uuid), "data": {"role": "not-a-role"}}], dry_run=True
    )
    assert result.failed == 1  # the bad enum value was caught
    assert service.get(note.uuid).node.data["role"] == "claim"


# ── CLI: shaping surface (gap 1) ──────────────────────────────────────────────


def test_cli_get_multi_target(run_cli: Callable[..., dict]) -> None:
    """Multi-target get prints {targets, nodes, failed}; single keeps its shape."""
    one = run_cli("add", "Person", "Ada Lovelace")["uuid"]
    two = run_cli("add", "Reference", "Lovelace 1843")
    single = run_cli("get", one)
    assert set(single) == {"node", "edges"}
    missing = str(uuid.uuid4())
    multi = run_cli("get", one, two["uuid"], missing)
    assert multi["targets"] == [one, two["uuid"], missing]
    assert [entry["node"]["uuid"] for entry in multi["nodes"]] == [one, two["uuid"]]
    assert [entry["uuid"] for entry in multi["failed"]] == [missing]


def test_cli_get_filters_and_fields(run_cli: Callable[..., dict]) -> None:
    """get honours --edge-kind/--direction, --fields minimal, --max-body-chars."""
    one = run_cli("add", "Note", "claim one", "--set", "role=claim")["uuid"]
    two = run_cli("add", "Note", "claim two", "--set", "role=claim")["uuid"]
    run_cli("link", one, two, "supports")

    outbound = run_cli("get", one, "--direction", "out")
    assert {edge["kind"] for edge in outbound["edges"]} == {"supports"}
    nothing = run_cli("get", one, "--direction", "out", "--edge-kind", "cites")
    assert nothing["edges"] == []

    minimal = run_cli("get", one, "--fields", "minimal")
    assert set(minimal["node"]) == {"uuid", "kind", "title"}

    long_body = "y" * 300
    three = run_cli("add", "Note", long_body, "--set", "role=claim")["uuid"]
    truncated = run_cli("get", three, "--max-body-chars", "50")
    assert truncated["node"]["content"] == "y" * 50
    assert truncated["node"]["content_truncated"] is True
    assert truncated["node"]["content_total_chars"] == 300


def test_cli_search_snippets_and_fields(run_cli: Callable[..., dict]) -> None:
    """Search returns 200-char snippets by default; --fields restores/projects."""
    body = "alpha " + "z" * 400
    run_cli("add", "Note", body, "--set", "role=claim")

    default = run_cli("search", "alpha")
    hit = default["hits"][0]
    assert hit["content"] == body[:200]
    assert hit["content_truncated"] is True
    assert hit["content_total_chars"] == len(body)

    full = run_cli("search", "alpha", "--fields", "full")
    assert full["hits"][0]["content"] == body
    assert "content_truncated" not in full["hits"][0]

    minimal = run_cli("search", "alpha", "--fields", "minimal")
    assert set(minimal["hits"][0]) == {"uuid", "kind", "title", "score"}

    explicit = run_cli("search", "alpha", "--max-body-chars", "10")
    assert explicit["hits"][0]["content"] == body[:10]


def test_cli_search_tag_filter(run_cli: Callable[..., dict]) -> None:
    """--tag narrows search hits (repeatable, AND semantics)."""
    run_cli(
        "add",
        "Note",
        "gamma tagged both",
        "--set",
        "role=claim",
        "--set",
        'tags=["a","b"]',
    )
    run_cli("add", "Note", "gamma tagged one", "--set", "role=claim", "--set", 'tags=["a"]')
    hits = run_cli("search", "gamma", "--tag", "a", "--tag", "b", "--fields", "full")["hits"]
    assert len(hits) == 1
    assert hits[0]["data"]["tags"] == ["a", "b"]


def test_cli_add_batch_and_dry_run(run_cli: Callable[..., dict], tmp_path: Path) -> None:
    """add --batch creates valid items, reports the bad one, exits 1 on failure."""
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(
        json.dumps(
            [
                {"kind": "Person", "content": "Batch Ada"},
                {"kind": "Ghost", "content": "bad kind"},
            ]
        )
    )
    result = CliRunner().invoke(cli_app, ["add", "--batch", str(batch_file)])
    assert result.exit_code == 1  # partial failure
    payload = json.loads(result.stdout)
    assert payload["succeeded"] == 1
    assert payload["failed"] == 1
    assert payload["results"][1]["ok"] is False


def test_cli_add_batch_stdin_and_dry_run() -> None:
    """add --batch - reads the array from stdin; --dry-run writes nothing."""
    items = json.dumps([{"kind": "Person", "content": "Stdin Ada"}])
    dry = CliRunner().invoke(cli_app, ["add", "--batch", "-", "--dry-run"], input=items)
    assert dry.exit_code == 0
    assert json.loads(dry.stdout)["dry_run"] is True
    assert json.loads(dry.stdout)["succeeded"] == 1

    real = CliRunner().invoke(cli_app, ["add", "--batch", "-"], input=items)
    assert real.exit_code == 0
    created = json.loads(real.stdout)["results"][0]["uuid"]
    fetched = CliRunner().invoke(cli_app, ["get", created])
    assert json.loads(fetched.stdout)["node"]["content"] == "Stdin Ada"


def test_cli_add_batch_rejects_mixed_form() -> None:
    """--batch is mutually exclusive with the positional KIND/CONTENT form."""
    result = CliRunner().invoke(cli_app, ["add", "Person", "Ada", "--batch", "-"])
    assert result.exit_code == 1


def test_cli_edit_node_batch() -> None:
    """edit-node --batch merges items, records failures, exits 1 on any failure."""
    runner = CliRunner()
    created = runner.invoke(cli_app, ["add", "Note", "draft", "--set", "role=claim"])
    note_uuid = json.loads(created.stdout)["uuid"]
    items = json.dumps(
        [
            {"uuid": note_uuid, "content": "final"},
            {"uuid": str(uuid.uuid4()), "content": "nope"},
        ]
    )
    result = runner.invoke(cli_app, ["edit-node", "--batch", "-"], input=items)
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["operation"] == "edit-batch"
    assert payload["succeeded"] == 1
    assert payload["failed"] == 1
    fetched = runner.invoke(cli_app, ["get", note_uuid])
    assert json.loads(fetched.stdout)["node"]["content"] == "final"


# ── CLI: bundled skill (gap 5) ────────────────────────────────────────────────


def test_cli_skill_install_and_status(run_cli: Callable[..., dict], tmp_path: Path) -> None:
    """skill install --dest copies the bundled SKILL.md; status reports targets."""
    dest = tmp_path / "skills"
    installed = run_cli("skill", "install", "--dest", str(dest))
    target = dest / "SKILL.md"
    assert installed["installed"] == str(target)
    assert installed["overwritten"] is False
    assert target.read_text(encoding="utf-8").startswith("---\nname: nodum")

    again = run_cli("skill", "install", "--dest", str(dest), "--force")
    assert again["overwritten"] is True

    refused = CliRunner().invoke(cli_app, ["skill", "install", "--dest", str(dest)])
    assert refused.exit_code == 1

    status = run_cli("skill", "status")
    assert status["bundled_bytes"] > 0
    assert {row["scope"] for row in status["targets"]} == {"user", "project"}


# ── API: matching query params (gaps 3 + 4) ───────────────────────────────────


def test_api_get_filters(client: TestClient) -> None:
    """GET /nodes/{uuid} honours edge_kind and direction query params."""
    one = client.post(
        "/nodes", json={"kind": "Note", "content": "api claim one", "data": {"role": "claim"}}
    ).json()
    two = client.post(
        "/nodes", json={"kind": "Note", "content": "api claim two", "data": {"role": "claim"}}
    ).json()
    client.post(
        "/edges",
        json={"kind": "supports", "from_uuid": one["uuid"], "to_uuid": two["uuid"]},
    )
    outbound = client.get(f"/nodes/{one['uuid']}", params={"direction": "out"})
    assert outbound.status_code == 200
    assert {edge["kind"] for edge in outbound.json()["edges"]} == {"supports"}
    filtered = client.get(
        f"/nodes/{one['uuid']}", params={"direction": "in", "edge_kind": "supports"}
    )
    assert filtered.json()["edges"] == []
    bad = client.get(f"/nodes/{one['uuid']}", params={"direction": "sideways"})
    assert bad.status_code == 422


def test_api_search_tag_filter_and_title(client: TestClient) -> None:
    """GET /search honours repeatable tag params and every hit carries a title."""
    response = client.post(
        "/nodes",
        json={
            "kind": "Note",
            "content": "delta retention claim\nwith a second line",
            "data": {"role": "claim", "tags": ["x", "y"]},
        },
    )
    assert response.status_code == 200
    tagged = client.get("/search", params=[("q", "delta"), ("tag", "x"), ("tag", "y")])
    assert {hit["uuid"] for hit in tagged.json()["hits"]} == {response.json()["uuid"]}
    assert tagged.json()["hits"][0]["title"] == "delta retention claim"
    unmatched = client.get("/search", params=[("q", "delta"), ("tag", "nope")])
    assert unmatched.json()["hits"] == []
