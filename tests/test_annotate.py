"""The annotation write path — migration 0016's missing half (design §L1).

The table shipped with the learned-curation cycle's schema and no writer; the
tests here are that writer's contract: the exclusive arc resolves one target
column per kind, the review gate is the review queue's own, the read side
answers *not found* rather than *permission denied*, a second annotation on
the same target replaces the first instead of accumulating, and a cycle in
progress stamps the row it writes.
"""

from __future__ import annotations

import pytest
from helpers import agent, owner

from nodum import db, service


def _rows():
    """Every annotations row, as dicts, in insertion order."""
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM annotations").fetchall()]
    finally:
        conn.close()


def test_annotate_writes_the_exclusive_arc_row(fresh_db):
    """One target column per kind, and the read side picks the row up."""
    a = service.create_node(type="concept", title="Alpha", principal=owner())
    b = service.create_node(type="concept", title="Beta", principal=owner())
    node = service.create_node(type="note", title="Node", principal=agent("researcher"))
    edge = service.create_edge(a.id, b.id, "supports", principal=agent("researcher"))
    version = service.update_node(a.id, content="revised", principal=agent("researcher"))

    node_out = service.annotate("node", node.id, {"rate": 0.9}, principal=owner())
    edge_out = service.annotate("edge", edge.id, {"rate": 0.8}, principal=owner())
    version_out = service.annotate("version", version.id, {"rate": 0.7}, principal=owner())

    assert node_out.target_kind == "node" and node_out.target_id == node.id
    assert edge_out.target_kind == "edge" and edge_out.target_id == edge.id
    assert version_out.target_kind == "version" and version_out.target_id == str(version.id)
    assert {row["actor"] for row in _rows()} == {"human:owner"}

    by_kind = {p.kind: p for p in service.list_proposals(principal=owner())}
    assert by_kind["node"].annotation == {"rate": 0.9}
    assert by_kind["edge"].annotation == {"rate": 0.8}
    assert by_kind["update"].annotation == {"rate": 0.7}


def test_annotate_requires_review_authority(fresh_db):
    """A suggest grant can see the item and still cannot annotate it."""
    node = service.create_node(type="note", title="Bot note", principal=agent("researcher"))

    with pytest.raises(service.GrantNotPermitted, match="annotate"):
        service.annotate("node", node.id, {"rate": 0.9}, principal=agent("researcher"))

    assert _rows() == []


def test_annotate_does_not_probe_existence(fresh_db):
    """Unreadable and nonexistent answer identically: RecordNotFound."""
    from helpers import seed_space

    seed_space("b")
    hidden = service.create_node(type="note", title="Hidden", space="b", principal=owner())
    outsider = agent("outsider", grants={"meta": "read"})

    with pytest.raises(service.RecordNotFound) as unreadable_refusal:
        service.annotate("node", hidden.id, {"rate": 0.9}, principal=outsider)
    with pytest.raises(service.RecordNotFound) as nonexistent_refusal:
        service.annotate("node", "no-such-node", {"rate": 0.9}, principal=owner)

    # The same sentence for both: an id outside the read set does not exist.
    assert str(unreadable_refusal.value) == f"no readable node with id: {hidden.id}"
    assert str(nonexistent_refusal.value) == "no readable node with id: no-such-node"
    assert _rows() == []


def test_reannotating_replaces(fresh_db):
    """The unique index holds one annotation per target — the write deletes first."""
    node = service.create_node(type="note", title="Bot note", principal=agent("researcher"))

    first = service.annotate("node", node.id, {"rate": 0.4}, principal=owner())
    second = service.annotate("node", node.id, {"rate": 0.9}, principal=owner())

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["id"] == second.id != first.id
    (proposal,) = service.list_proposals(principal=owner())
    assert proposal.annotation == {"rate": 0.9}


def test_annotate_rejects_a_bad_kind(fresh_db):
    with pytest.raises(ValueError, match="target_kind"):
        service.annotate("widget", "anything", {"rate": 0.9}, principal=owner())


def test_annotate_rejects_a_non_json_body(fresh_db):
    node = service.create_node(type="note", title="Bot note", principal=owner())
    with pytest.raises(ValueError, match="JSON"):
        service.annotate("node", node.id, {"bad": object()}, principal=owner())
    assert _rows() == []


def test_annotate_rejects_a_nan_body_before_any_write(fresh_db):
    """NaN/Infinity pass ``json.dumps``' default ``allow_nan`` and would be
    stored as literal invalid JSON no strict ``JSON.parse`` accepts — the
    guard has to refuse them up front, like any other unserialisable value."""
    node = service.create_node(type="note", title="Bot note", principal=owner())
    with pytest.raises(ValueError, match="JSON"):
        service.annotate("node", node.id, {"rate": float("nan")}, principal=owner())
    assert _rows() == []


def test_annotate_rejects_a_non_string_body_key_before_any_write(fresh_db):
    """``json.dumps`` coerces a non-string key (``{1: "a"}`` serialises as
    ``{"1": "a"}``), but the model's ``dict[str, Any]`` does not accept it —
    so the failure must land before the commit, or the DELETE+INSERT pair
    would persist a row the caller was told did not write."""
    from pydantic import ValidationError

    node = service.create_node(type="note", title="Bot note", principal=owner())
    with pytest.raises(ValidationError):
        service.annotate("node", node.id, {1: "a"}, principal=owner())
    assert _rows() == []


def test_an_annotation_is_cycle_stamped_when_written_in_a_cycle(fresh_db):
    """`annotate` reads the ambient cycle like `_emit` does, so a night's
    annotations roll back with the night."""
    node = service.create_node(type="note", title="Bot note", principal=owner())
    cycle = service.open_cycle(trigger="curative", principal=owner())
    try:
        with service.in_cycle(cycle.id):
            service.annotate("node", node.id, {"rate": 0.9}, principal=owner())
    finally:
        service.close_cycle(cycle.id, status="completed", report={}, principal=owner())

    (row,) = _rows()
    assert row["cycle_id"] == cycle.id
