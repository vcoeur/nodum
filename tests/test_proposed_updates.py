"""Proposed updates (design §8.1): agent update_node → proposed version lifecycle."""

from __future__ import annotations

import pytest

from nodum import db, service
from nodum.service import InvalidTransition, VersionNotFound

AGENT = "agent:researcher"


def _graph():
    """An active concept and an active note linking to it."""
    concept = service.create_node(type="concept", title="Graph Theory")
    note = service.create_node(type="note", title="My note", content="See [[Graph Theory]].")
    return concept, note


# ── Proposing ─────────────────────────────────────────────────────────────────


def test_agent_update_stages_a_proposed_version_and_leaves_the_node(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="Rewritten by the bot.", actor=AGENT)

    assert version.state == "proposed"
    assert version.node_id == note.id
    assert version.content == "Rewritten by the bot."
    assert version.actor == AGENT
    # Untouched fields carry over from the current node.
    assert version.title == "My note"

    after = service.get_node(note.id)
    assert after.content == "See [[Graph Theory]]."
    assert after.updated_at == note.updated_at


def test_agent_update_emits_version_propose(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, title="Bot title", actor=AGENT)

    events = service.list_events(limit=1)
    assert events[0].op == "version.propose"
    assert events[0].actor == AGENT
    assert events[0].seq == version.event_seq
    assert events[0].payload["node_id"] == note.id
    assert events[0].payload["proposed"]["title"] == "Bot title"


def test_human_update_still_applies_in_place(fresh_db):
    _, note = _graph()
    updated = service.update_node(note.id, content="Direct edit.")
    assert updated.content == "Direct edit."
    versions = service.history(note.id)
    assert [v.state for v in versions] == ["applied", "applied"]


def test_history_marks_proposed_versions(fresh_db):
    _, note = _graph()
    service.update_node(note.id, content="v2", actor=AGENT)
    states = [v.state for v in service.history(note.id)]
    assert states == ["applied", "proposed"]


# ── Accept ────────────────────────────────────────────────────────────────────


def test_accept_applies_the_version(fresh_db):
    _, note = _graph()
    version = service.update_node(
        note.id, title="Better title", content="See [[Graph Theory]] deeply.", actor=AGENT
    )
    accepted = service.transition(str(version.id), "accept")

    assert accepted.state == "applied"
    node = service.get_node(note.id)
    assert node.title == "Better title"
    assert node.content == "See [[Graph Theory]] deeply."

    event = service.list_events(limit=1)[0]
    assert event.op == "node.update"
    assert event.actor == "human"
    assert event.payload["applied_version_id"] == version.id
    assert event.payload["proposed_event_seq"] == version.event_seq


def test_accept_rematerializes_wikilinks(fresh_db):
    concept, note = _graph()
    other = service.create_node(type="concept", title="Topology")
    version = service.update_node(note.id, content="Now about [[Topology]] only.", actor=AGENT)
    service.transition(str(version.id), "accept")

    mentions = service.list_edges(node_id=note.id, type="mentions", state="active")
    assert [edge.dst_id for edge in mentions] == [other.id]
    archived = service.list_edges(node_id=note.id, type="mentions", state="archived")
    assert [edge.dst_id for edge in archived] == [concept.id]


def test_accepted_update_is_indexed_for_search(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="quixotic uniqueness token", actor=AGENT)
    from nodum import search as search_module

    assert search_module.search("quixotic").hits == []
    service.transition(str(version.id), "accept")
    hits = search_module.search("quixotic").hits
    assert [hit.node_id for hit in hits] == [note.id]


def test_accept_of_applied_version_fails(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", actor=AGENT)
    service.transition(str(version.id), "accept")
    with pytest.raises(InvalidTransition):
        service.transition(str(version.id), "accept")


def test_applied_update_is_undoable(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="Bot rewrite.", actor=AGENT)
    service.transition(str(version.id), "accept")
    # The accept's node.update is undoable like any other update. (Accepting
    # also archives the wikilink the rewrite dropped, so the latest event is
    # that edge.archive — name the node.update seq explicitly.)
    update_event = next(e for e in service.list_events(limit=10) if e.op == "node.update")
    service.undo(update_event.seq)
    assert service.get_node(note.id).content == "See [[Graph Theory]]."


# ── Reject ────────────────────────────────────────────────────────────────────


def test_reject_archives_the_version_and_records_reason(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="Bot rewrite.", actor=AGENT)
    rejected = service.transition(str(version.id), "reject")

    assert rejected.state == "archived"
    assert service.get_node(note.id).content == "See [[Graph Theory]]."
    event = service.list_events(limit=1)[0]
    assert event.op == "version.reject"


def test_archive_action_does_not_apply_to_versions(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", actor=AGENT)
    with pytest.raises(InvalidTransition):
        service.transition(str(version.id), "archive")


def test_unknown_version_id(fresh_db):
    with pytest.raises(VersionNotFound):
        service.diff_versions(999, 1000)


# ── Review queue integration ──────────────────────────────────────────────────


def test_update_proposals_appear_in_the_queue(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", actor=AGENT)
    proposals = service.list_proposals()

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.kind == "update"
    assert proposal.id == str(version.id)
    assert proposal.type == "note"
    assert proposal.created_by == AGENT
    assert proposal.version is not None
    assert proposal.version.content == "v2"
    assert proposal.context["node"] == {"id": note.id, "title": "My note"}


def test_queue_kind_and_actor_filters_cover_updates(fresh_db):
    _, note = _graph()
    service.update_node(note.id, content="v2", actor=AGENT)
    service.update_node(note.id, content="v3", actor="agent:other")

    assert len(service.list_proposals(kind="update")) == 2
    assert len(service.list_proposals(kind="node")) == 0
    assert len(service.list_proposals(kind="update", created_by=AGENT)) == 1
    assert len(service.list_proposals(kind="update", type="note")) == 2
    assert len(service.list_proposals(kind="update", type="mentions")) == 0


def test_batch_accept_applies_updates(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", actor=AGENT)
    result = service.accept_proposals([str(version.id), "missing-id"])

    assert result.transitioned == [str(version.id)]
    assert len(result.failed) == 1
    assert service.get_node(note.id).content == "v2"


def test_batch_reject_by_filter_covers_updates(fresh_db):
    _, note = _graph()
    service.update_node(note.id, content="v2", actor=AGENT)
    result = service.reject_matching(reason="not good enough", created_by=AGENT)

    assert len(result.transitioned) == 1
    assert service.list_proposals() == []
    event = service.list_events(limit=1)[0]
    assert event.op == "version.reject"
    assert event.payload["reason"] == "not good enough"


# ── Migration ─────────────────────────────────────────────────────────────────


def test_versions_have_state_column_defaulting_to_applied(fresh_db):
    _, note = _graph()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT state FROM versions WHERE node_id = ?", (note.id,)).fetchall()
        assert [row["state"] for row in rows] == ["applied"]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(versions)")}
        assert "state" in columns
    finally:
        conn.close()
