"""Review queue (design §8.1): listing proposals and batch accept/reject."""

from __future__ import annotations

import json

import pytest
from helpers import OWNER_ACTOR, agent, owner
from typer.testing import CliRunner

from nodum import service
from nodum.cli import app

runner = CliRunner()


NO_AS_GROUPS = {"init", "schema-dump", "projector", "asset", "mcp", "serve"}


def _maybe_as(args):
    args = list(args)
    if args and args[0] not in NO_AS_GROUPS and "--as" not in args:
        args += ["--as", "owner"]
    return args


def _run_json(*args):
    result = runner.invoke(app, _maybe_as(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _run_fail(*args):
    result = runner.invoke(app, _maybe_as(args))
    assert result.exit_code == 1
    return result


def _seed_proposals():
    """Two active concepts plus an agent's proposed node and proposed edge."""
    a = service.create_node(type="concept", title="Alpha", principal=owner())
    b = service.create_node(type="concept", title="Beta", principal=owner())
    note = service.create_node(
        type="note", title="Bot note", content="draft", principal=agent("researcher")
    )
    edge = service.create_edge(
        a.id, b.id, "supports", confidence=0.8, principal=agent("researcher")
    )
    return a, b, note, edge


# ── Listing ───────────────────────────────────────────────────────────────────


def test_list_proposals_returns_nodes_and_edges(fresh_db):
    _, _, note, edge = _seed_proposals()
    proposals = service.list_proposals(principal=owner())
    assert {p.id for p in proposals} == {note.id, edge.id}
    by_kind = {p.kind: p for p in proposals}
    assert by_kind["node"].node.title == "Bot note"
    assert by_kind["edge"].edge.confidence == 0.8
    assert by_kind["edge"].type == "supports"


def test_list_proposals_edge_context_has_endpoints(fresh_db):
    a, b, _, edge = _seed_proposals()
    (proposal,) = service.list_proposals(kind="edge", principal=owner())
    assert proposal.id == edge.id
    assert proposal.context["src"] == {"id": a.id, "title": "Alpha"}
    assert proposal.context["dst"] == {"id": b.id, "title": "Beta"}


def test_list_proposals_node_context_has_parent(fresh_db):
    page = service.create_node(type="page", title="Page", principal=owner())
    child = service.create_node(
        type="block", content="x", parent_id=page.id, principal=agent("researcher")
    )
    (proposal,) = service.list_proposals(kind="node", principal=owner())
    assert proposal.id == child.id
    assert proposal.context["parent"] == {"id": page.id, "title": "Page"}


def test_list_proposals_filters(fresh_db):
    _, _, note, edge = _seed_proposals()
    service.create_node(type="note", title="Other bot", principal=agent("other"))

    assert {
        p.id for p in service.list_proposals(created_by="agent:researcher", principal=owner())
    } == {note.id, edge.id}
    assert [p.id for p in service.list_proposals(kind="edge", principal=owner())] == [edge.id]
    assert {p.id for p in service.list_proposals(type="note", principal=owner())} == {
        p.id for p in service.list_proposals(kind="node", principal=owner())
    }
    assert [p.id for p in service.list_proposals(type="supports", principal=owner())] == [edge.id]
    # Far-future bound excludes everything; far-past bound includes everything.
    assert service.list_proposals(created_before="2000-01-01 00:00:00", principal=owner()) == []
    assert len(service.list_proposals(created_after="2000-01-01 00:00:00", principal=owner())) == 3
    with pytest.raises(service.TypeNotFound):
        service.list_proposals(type="no-such-type", principal=owner())
    with pytest.raises(ValueError, match="kind"):
        service.list_proposals(kind="widget", principal=owner())


def test_accepted_proposals_leave_the_queue(fresh_db):
    _, _, note, edge = _seed_proposals()
    service.transition(note.id, "accept", principal=owner())
    assert [p.id for p in service.list_proposals(principal=owner())] == [edge.id]


# ── Batch accept/reject by id ─────────────────────────────────────────────────


def test_accept_proposals_transitions_each_with_event(fresh_db):
    _, _, note, edge = _seed_proposals()
    result = service.accept_proposals([note.id, edge.id], principal=owner())
    assert result.action == "accept"
    assert set(result.transitioned) == {note.id, edge.id}
    assert result.failed == []
    assert service.get_node(note.id, principal=owner()).state == "active"
    assert service.list_edges(node_id=edge.src_id, principal=owner())[0].state == "active"
    ops = [e.op for e in service.list_events(limit=2, principal=owner())]
    assert sorted(ops) == ["edge.accept", "node.accept"]
    assert all(e.actor == OWNER_ACTOR for e in service.list_events(limit=2, principal=owner()))


def test_single_item_reject_records_the_same_reason_as_a_batch(fresh_db):
    """`transition` carries a reason for every kind, exactly as the batch does.

    Without it the two spellings of one operation had different audit
    guarantees: `nodum reject <id>` dropped the reviewer's reason on the floor.
    """
    _, _, note, edge = _seed_proposals()
    version = service.update_node(note.id, content="v2", principal=agent("researcher"))

    service.transition(str(version.id), "reject", reason="wrong claim", principal=owner())
    service.transition(note.id, "reject", reason="off topic", principal=owner())
    service.reject_proposals([edge.id], reason="off topic", principal=owner())

    events = {event.op: event for event in service.list_events(limit=3, principal=owner())}
    assert set(events) == {"version.reject", "node.reject", "edge.reject"}
    assert events["version.reject"].payload["reason"] == "wrong claim"
    assert events["node.reject"].payload["reason"] == "off topic"
    # The batch path's payload is the same shape — one operation, one record.
    assert events["edge.reject"].payload["reason"] == "off topic"


def test_accept_writes_no_reason_key(fresh_db):
    """A reason belongs to a refusal; accepting one leaves the payload clean."""
    _, _, note, _ = _seed_proposals()
    service.transition(note.id, "accept", principal=owner())
    assert "reason" not in service.list_events(limit=1, principal=owner())[0].payload


def test_reject_proposals_archives_with_reason(fresh_db):
    _, _, note, edge = _seed_proposals()
    result = service.reject_proposals([note.id, edge.id], reason="spam run", principal=owner())
    assert result.reason == "spam run"
    assert set(result.transitioned) == {note.id, edge.id}
    assert service.get_node(note.id, principal=owner()).state == "archived"
    events = service.list_events(limit=2, principal=owner())
    assert sorted(e.op for e in events) == ["edge.reject", "node.reject"]
    assert all(e.payload["reason"] == "spam run" for e in events)
    assert all(e.actor == OWNER_ACTOR for e in events)


# ── Review authority: human, or edit on the item's space (Q13 note 03 Q1) ────


def test_suggest_agent_cannot_accept_its_own_proposal(fresh_db):
    """A suggest grant proposes; it never reviews — not even its own proposals."""
    _, _, note, edge = _seed_proposals()
    result = service.accept_proposals([note.id, edge.id], principal=agent("researcher"))
    assert result.transitioned == []
    assert len(result.failed) == 2
    assert all("edit" in f.error for f in result.failed)
    assert service.get_node(note.id, principal=owner()).state == "proposed"
    assert {p.id for p in service.list_proposals(principal=owner())} == {note.id, edge.id}


def test_suggest_agent_cannot_reject_another_agents_proposal(fresh_db):
    _, _, note, _ = _seed_proposals()
    result = service.reject_proposals([note.id], reason="turf war", principal=agent("curator"))
    assert result.transitioned == []
    assert len(result.failed) == 1
    assert service.get_node(note.id, principal=owner()).state == "proposed"


def test_suggest_agent_cannot_review_through_any_service_entry_point(fresh_db):
    _, _, note, _ = _seed_proposals()
    version = service.update_node(note.id, content="v2", principal=agent("researcher"))
    suggest = agent("researcher")
    for call in (
        lambda: service.transition(note.id, "accept", principal=suggest),
        lambda: service.transition(note.id, "reject", principal=suggest),
        lambda: service.transition(str(version.id), "accept", principal=suggest),
    ):
        with pytest.raises(service.GrantNotPermitted):
            call()
    # Batch-by-filter refusals are per-item failures, not a raised batch.
    refused = service.accept_matching(created_by="agent:researcher", principal=suggest)
    assert refused.transitioned == [] and refused.failed
    refused = service.reject_matching(
        reason="all mine", created_by="agent:researcher", principal=suggest
    )
    assert refused.transitioned == [] and refused.failed
    noop = service.accept_matching(created_by="agent:nobody", principal=suggest)
    assert noop.transitioned == [] and noop.failed == []
    assert service.get_node(note.id, principal=owner()).state == "proposed"
    assert [v.state for v in service.history(note.id, principal=owner())][-1] == "proposed"


def test_edit_agent_reviews_within_its_granted_space(fresh_db):
    """Q13 note 03 Q1: an edit grant carries full in-space state-machine authority."""
    _, _, note, edge = _seed_proposals()
    editor = agent("editor", grants={"meta": "read", "main": "edit"})
    result = service.accept_proposals([note.id, edge.id], principal=editor)
    assert set(result.transitioned) == {note.id, edge.id}
    assert service.get_node(note.id, principal=owner()).state == "active"
    assert result.actor == "agent:editor"


def test_suggest_agent_cannot_archive_live_state(fresh_db):
    """Archiving retires live structure, so it needs edit too."""
    a, _, _, _ = _seed_proposals()
    with pytest.raises(service.GrantNotPermitted):
        service.transition(a.id, "archive", principal=agent("researcher"))
    assert service.get_node(a.id, principal=owner()).state == "active"
    assert service.transition(a.id, "archive", principal=owner()).state == "archived"


def test_agent_cannot_undo(fresh_db):
    """Undo restores an event's payload verbatim — `state = 'active'` included.

    It stays human-only: no grant delegates it, because restoring arbitrary
    prior state across spaces is the live-state back door (Q13 note 01).
    """
    a, _, _, _ = _seed_proposals()
    archived = service.transition(a.id, "archive", principal=owner())
    archive_seq = service.list_events(limit=1, principal=owner())[0].seq

    with pytest.raises(service.GrantNotPermitted, match="only a human"):
        service.undo(archive_seq, principal=agent("researcher"))
    assert service.get_node(a.id, principal=owner()).state == archived.state == "archived"
    assert service.list_events(limit=1, principal=owner())[0].seq == archive_seq

    # Not even with edit on the space: undo is not a grantable authority.
    with pytest.raises(service.GrantNotPermitted, match="only a human"):
        service.undo(
            archive_seq, principal=agent("editor", grants={"meta": "read", "main": "edit"})
        )

    service.undo(archive_seq, principal=owner())
    assert service.get_node(a.id, principal=owner()).state == "active"


def test_batch_collects_failures_without_aborting(fresh_db):
    active = service.create_node(type="note", title="already active", principal=owner())
    _, _, note, _ = _seed_proposals()
    result = service.accept_proposals([note.id, active.id, "missing-id"], principal=owner())
    assert result.transitioned == [note.id]
    assert {f.id for f in result.failed} == {active.id, "missing-id"}
    assert any("cannot accept" in f.error for f in result.failed)
    assert any("no node, edge, or version" in f.error for f in result.failed)


# ── Batch by filter ───────────────────────────────────────────────────────────


def test_accept_matching_by_agent(fresh_db):
    _, _, note, edge = _seed_proposals()
    other = service.create_node(type="note", title="keep", principal=agent("other"))
    result = service.accept_matching(created_by="agent:researcher", principal=owner())
    assert set(result.transitioned) == {note.id, edge.id}
    assert service.get_node(other.id, principal=owner()).state == "proposed"


def test_reject_matching_by_type_and_kind(fresh_db):
    _, _, note, edge = _seed_proposals()
    result = service.reject_matching(
        reason="bad links",
        kind="edge",
        type="supports",
        principal=owner(),
    )
    assert result.transitioned == [edge.id]
    assert service.get_node(note.id, principal=owner()).state == "proposed"


def test_matching_with_no_match_is_a_noop(fresh_db):
    _seed_proposals()
    result = service.accept_matching(created_by="agent:nobody", principal=owner())
    assert result.transitioned == []
    assert result.failed == []


def test_reject_matching_records_actor_and_reason(fresh_db):
    _seed_proposals()
    service.reject_matching(reason="cleanup", created_by="agent:researcher", principal=owner())
    assert service.list_proposals(principal=owner()) == []
    events = service.list_events(limit=2, principal=owner())
    assert all(e.op.endswith(".reject") for e in events)
    assert all(e.payload["reason"] == "cleanup" for e in events)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli_seed():
    """Two proposals by a suggest-level agent (the CLI itself is human-only)."""
    a = _run_json("node", "create", "--type", "concept", "--title", "Alpha")
    b = _run_json("node", "create", "--type", "concept", "--title", "Beta")
    note = service.create_node(type="note", title="Bot note", principal=agent("researcher"))
    note = note.model_dump(mode="json")
    edge = service.create_edge(a["id"], b["id"], "supports", principal=agent("researcher"))
    edge = edge.model_dump(mode="json")
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


def test_cli_has_no_actor_option(fresh_db):
    """The CLI is human-only: there is no --actor to impersonate an agent with."""
    note, _ = _cli_seed()
    result = runner.invoke(app, ["review", "accept", note["id"], "--actor", "agent:researcher"])
    assert result.exit_code == 2  # typer: no such option
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
