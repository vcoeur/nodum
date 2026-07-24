"""Review queue (design §8.1): listing proposals and batch accept/reject."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nodum import service
from nodum.cli import app

runner = CliRunner()


def _run_json(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _run_fail(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 1
    return result


def _seed_proposals():
    """Two active concepts plus an agent's proposed node and proposed edge."""
    a = service.create_node(type="concept", title="Alpha")
    b = service.create_node(type="concept", title="Beta")
    note = service.create_node(
        type="note", title="Bot note", content="draft", actor="agent:researcher"
    )
    edge = service.create_edge(a.id, b.id, "supports", confidence=0.8, actor="agent:researcher")
    return a, b, note, edge


# ── Listing ───────────────────────────────────────────────────────────────────


def test_list_proposals_returns_nodes_and_edges(fresh_db):
    _, _, note, edge = _seed_proposals()
    proposals = service.list_proposals()
    assert {p.id for p in proposals} == {note.id, edge.id}
    by_kind = {p.kind: p for p in proposals}
    assert by_kind["node"].node.title == "Bot note"
    assert by_kind["edge"].edge.confidence == 0.8
    assert by_kind["edge"].type == "supports"


def test_list_proposals_edge_context_has_endpoints(fresh_db):
    a, b, _, edge = _seed_proposals()
    (proposal,) = service.list_proposals(kind="edge")
    assert proposal.id == edge.id
    assert proposal.context["src"] == {"id": a.id, "title": "Alpha"}
    assert proposal.context["dst"] == {"id": b.id, "title": "Beta"}


def test_list_proposals_node_context_has_parent(fresh_db):
    page = service.create_node(type="page", title="Page")
    child = service.create_node(
        type="block", content="x", parent_id=page.id, actor="agent:researcher"
    )
    (proposal,) = service.list_proposals(kind="node")
    assert proposal.id == child.id
    assert proposal.context["parent"] == {"id": page.id, "title": "Page"}


def test_list_proposals_filters(fresh_db):
    _, _, note, edge = _seed_proposals()
    service.create_node(type="note", title="Other bot", actor="agent:other")

    assert {p.id for p in service.list_proposals(created_by="agent:researcher")} == {
        note.id,
        edge.id,
    }
    assert [p.id for p in service.list_proposals(kind="edge")] == [edge.id]
    assert {p.id for p in service.list_proposals(type="note")} == {
        p.id for p in service.list_proposals(kind="node")
    }
    assert [p.id for p in service.list_proposals(type="supports")] == [edge.id]
    # Far-future bound excludes everything; far-past bound includes everything.
    assert service.list_proposals(created_before="2000-01-01 00:00:00") == []
    assert len(service.list_proposals(created_after="2000-01-01 00:00:00")) == 3
    with pytest.raises(service.TypeNotFound):
        service.list_proposals(type="no-such-type")
    with pytest.raises(ValueError, match="kind"):
        service.list_proposals(kind="widget")


def test_accepted_proposals_leave_the_queue(fresh_db):
    _, _, note, edge = _seed_proposals()
    service.transition(note.id, "accept")
    assert [p.id for p in service.list_proposals()] == [edge.id]


# ── Batch accept/reject by id ─────────────────────────────────────────────────


def test_accept_proposals_transitions_each_with_event(fresh_db):
    _, _, note, edge = _seed_proposals()
    result = service.accept_proposals([note.id, edge.id], actor="human")
    assert result.action == "accept"
    assert set(result.transitioned) == {note.id, edge.id}
    assert result.failed == []
    assert service.get_node(note.id).state == "active"
    assert service.list_edges(node_id=edge.src_id)[0].state == "active"
    ops = [e.op for e in service.list_events(limit=2)]
    assert sorted(ops) == ["edge.accept", "node.accept"]
    assert all(e.actor == "human" for e in service.list_events(limit=2))


def test_single_item_reject_records_the_same_reason_as_a_batch(fresh_db):
    """`transition` carries a reason for every kind, exactly as the batch does.

    Without it the two spellings of one operation had different audit
    guarantees: `nodum reject <id>` dropped the reviewer's reason on the floor.
    """
    _, _, note, edge = _seed_proposals()
    version = service.update_node(note.id, content="v2", actor="agent:researcher")

    service.transition(str(version.id), "reject", reason="wrong claim")
    service.transition(note.id, "reject", reason="off topic")
    service.reject_proposals([edge.id], reason="off topic")

    events = {event.op: event for event in service.list_events(limit=3)}
    assert set(events) == {"version.reject", "node.reject", "edge.reject"}
    assert events["version.reject"].payload["reason"] == "wrong claim"
    assert events["node.reject"].payload["reason"] == "off topic"
    # The batch path's payload is the same shape — one operation, one record.
    assert events["edge.reject"].payload["reason"] == "off topic"


def test_accept_writes_no_reason_key(fresh_db):
    """A reason belongs to a refusal; accepting one leaves the payload clean."""
    _, _, note, _ = _seed_proposals()
    service.transition(note.id, "accept")
    assert "reason" not in service.list_events(limit=1)[0].payload


def test_reject_proposals_archives_with_reason(fresh_db):
    _, _, note, edge = _seed_proposals()
    result = service.reject_proposals([note.id, edge.id], reason="spam run", actor="human")
    assert result.reason == "spam run"
    assert set(result.transitioned) == {note.id, edge.id}
    assert service.get_node(note.id).state == "archived"
    events = service.list_events(limit=2)
    assert sorted(e.op for e in events) == ["edge.reject", "node.reject"]
    assert all(e.payload["reason"] == "spam run" for e in events)
    assert all(e.actor == "human" for e in events)


# ── Review is the human tier: no agent actor may accept or reject ─────────────


def test_agent_cannot_accept_its_own_proposal(fresh_db):
    _, _, note, edge = _seed_proposals()
    with pytest.raises(service.ReviewNotPermitted, match="only the 'human' actor"):
        service.accept_proposals([note.id, edge.id], actor="agent:researcher")
    # Nothing moved: the batch is refused whole, not id by id.
    assert service.get_node(note.id).state == "proposed"
    assert {p.id for p in service.list_proposals()} == {note.id, edge.id}


def test_agent_cannot_reject_another_agents_proposal(fresh_db):
    _, _, note, _ = _seed_proposals()
    with pytest.raises(service.ReviewNotPermitted, match="only the 'human' actor"):
        service.reject_proposals([note.id], reason="turf war", actor="agent:curator")
    assert service.get_node(note.id).state == "proposed"


def test_agent_cannot_review_through_any_service_entry_point(fresh_db):
    _, _, note, _ = _seed_proposals()
    version = service.update_node(note.id, content="v2", actor="agent:researcher")
    agent = "agent:researcher"
    for call in (
        lambda: service.transition(note.id, "accept", actor=agent),
        lambda: service.transition(note.id, "reject", actor=agent),
        lambda: service.transition(str(version.id), "accept", actor=agent),
        lambda: service.accept_matching(created_by=agent, actor=agent),
        lambda: service.reject_matching(reason="all mine", created_by=agent, actor=agent),
        lambda: service.accept_matching(created_by="agent:nobody", actor=agent),
    ):
        with pytest.raises(service.ReviewNotPermitted):
            call()
    assert service.get_node(note.id).state == "proposed"
    assert [v.state for v in service.history(note.id)][-1] == "proposed"


def test_agent_cannot_archive_live_state(fresh_db):
    """Archiving retires live structure, so it is the human tier too."""
    a, _, _, _ = _seed_proposals()
    with pytest.raises(service.ReviewNotPermitted, match="only the 'human' actor"):
        service.transition(a.id, "archive", actor="agent:researcher")
    assert service.get_node(a.id).state == "active"
    assert service.transition(a.id, "archive").state == "archived"


def test_agent_cannot_undo(fresh_db):
    """Undo restores an event's payload verbatim — `state = 'active'` included.

    Leaving it open would let an agent write live state through the back
    door, defeating the propose-only rule the rest of the tier enforces.
    """
    a, _, _, _ = _seed_proposals()
    archived = service.transition(a.id, "archive")
    archive_seq = service.list_events(limit=1)[0].seq

    with pytest.raises(service.ReviewNotPermitted, match="only the 'human' actor"):
        service.undo(archive_seq, actor="agent:researcher")
    assert service.get_node(a.id).state == archived.state == "archived"
    # No undo event was written either: the refusal is before any work.
    assert service.list_events(limit=1)[0].seq == archive_seq

    service.undo(archive_seq)
    assert service.get_node(a.id).state == "active"


def test_cli_undo_and_archive_refuse_an_agent_actor(fresh_db):
    node = _run_json("node", "create", "--type", "note", "--title", "Live")
    for args in (
        ("archive", node["id"], "--actor", "agent:researcher"),
        ("undo", "--actor", "agent:researcher"),
    ):
        result = _run_fail(*args)
        assert "only the 'human' actor" in result.output
    assert _run_json("node", "get", node["id"])["state"] == "active"


def test_batch_collects_failures_without_aborting(fresh_db):
    active = service.create_node(type="note", title="already active")
    _, _, note, _ = _seed_proposals()
    result = service.accept_proposals([note.id, active.id, "missing-id"])
    assert result.transitioned == [note.id]
    assert {f.id for f in result.failed} == {active.id, "missing-id"}
    assert any("cannot accept" in f.error for f in result.failed)
    assert any("no node, edge, or version" in f.error for f in result.failed)


# ── Batch by filter ───────────────────────────────────────────────────────────


def test_accept_matching_by_agent(fresh_db):
    _, _, note, edge = _seed_proposals()
    other = service.create_node(type="note", title="keep", actor="agent:other")
    result = service.accept_matching(created_by="agent:researcher")
    assert set(result.transitioned) == {note.id, edge.id}
    assert service.get_node(other.id).state == "proposed"


def test_reject_matching_by_type_and_kind(fresh_db):
    _, _, note, edge = _seed_proposals()
    result = service.reject_matching(reason="bad links", kind="edge", type="supports")
    assert result.transitioned == [edge.id]
    assert service.get_node(note.id).state == "proposed"


def test_matching_with_no_match_is_a_noop(fresh_db):
    _seed_proposals()
    result = service.accept_matching(created_by="agent:nobody")
    assert result.transitioned == []
    assert result.failed == []


def test_reject_matching_records_actor_and_reason(fresh_db):
    _seed_proposals()
    service.reject_matching(reason="cleanup", created_by="agent:researcher", actor="human")
    assert service.list_proposals() == []
    events = service.list_events(limit=2)
    assert all(e.op.endswith(".reject") for e in events)
    assert all(e.payload["reason"] == "cleanup" for e in events)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli_seed():
    a = _run_json("node", "create", "--type", "concept", "--title", "Alpha")
    b = _run_json("node", "create", "--type", "concept", "--title", "Beta")
    note = _run_json(
        "node", "create", "--type", "note", "--title", "Bot note", "--actor", "agent:researcher"
    )
    edge = _run_json(
        "edge", "create", a["id"], b["id"], "--type", "supports", "--actor", "agent:researcher"
    )
    return note, edge


def test_cli_review_queue(fresh_db):
    note, edge = _cli_seed()
    listing = _run_json("review", "queue")
    assert listing["count"] == 2
    assert {p["id"] for p in listing["proposals"]} == {note["id"], edge["id"]}
    edge_proposal = next(p for p in listing["proposals"] if p["kind"] == "edge")
    assert edge_proposal["context"]["src"]["title"] == "Alpha"

    filtered = _run_json("review", "queue", "--kind", "edge")
    assert filtered["count"] == 1


def test_cli_review_accept_and_reject(fresh_db):
    note, edge = _cli_seed()
    accepted = _run_json("review", "accept", note["id"])
    assert accepted["transitioned"] == [note["id"]]
    assert _run_json("node", "get", note["id"])["state"] == "active"

    rejected = _run_json("review", "reject", edge["id"], "--reason", "not convinced")
    assert rejected["transitioned"] == [edge["id"]]
    assert rejected["reason"] == "not convinced"

    events = _run_json("events", "--limit", "1")
    assert events["events"][0]["op"] == "edge.reject"
    assert events["events"][0]["payload"]["reason"] == "not convinced"


def test_cli_review_refuses_an_agent_actor(fresh_db):
    note, _ = _cli_seed()
    for args in (
        ("review", "accept", note["id"], "--actor", "agent:researcher"),
        ("review", "reject", note["id"], "--reason", "mine", "--actor", "agent:researcher"),
        ("accept", note["id"], "--actor", "agent:researcher"),
    ):
        result = _run_fail(*args)
        assert "only the 'human' actor" in result.output
    assert _run_json("node", "get", note["id"])["state"] == "proposed"


def test_cli_review_reject_requires_reason(fresh_db):
    note, _ = _cli_seed()
    result = runner.invoke(app, ["review", "reject", note["id"]])
    assert result.exit_code == 2  # typer: missing required --reason


def test_cli_review_accept_all_by_agent(fresh_db):
    note, edge = _cli_seed()
    result = _run_json("review", "accept-all", "--created-by", "agent:researcher")
    assert set(result["transitioned"]) == {note["id"], edge["id"]}
    assert _run_json("review", "queue")["count"] == 0


def test_cli_review_reject_all_with_filters(fresh_db):
    note, edge = _cli_seed()
    result = _run_json("review", "reject-all", "--reason", "bad links", "--kind", "edge")
    assert result["transitioned"] == [edge["id"]]
    queue = _run_json("review", "queue")
    assert [p["id"] for p in queue["proposals"]] == [note["id"]]
