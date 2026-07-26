"""Proposed updates (design §8.1): agent update_node → proposed version lifecycle."""

from __future__ import annotations

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import db, service
from nodum.service import InvalidTransition, VersionNotFound

AGENT = "agent:researcher"


def _graph():
    """An active concept and an active note linking to it."""
    concept = service.create_node(type="concept", title="Graph Theory", principal=owner())
    note = service.create_node(
        type="note",
        title="My note",
        content="See [[Graph Theory]].",
        principal=owner(),
    )
    return concept, note


# ── Proposing ─────────────────────────────────────────────────────────────────


def test_agent_update_stages_a_proposed_version_and_leaves_the_node(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="Rewritten by the bot.", principal=agent(AGENT))

    assert version.state == "proposed"
    assert version.node_id == note.id
    assert version.content == "Rewritten by the bot."
    assert version.actor == AGENT
    # Untouched fields carry over from the current node.
    assert version.title == "My note"

    after = service.get_node(note.id, principal=owner())
    assert after.content == "See [[Graph Theory]]."
    assert after.updated_at == note.updated_at


def test_agent_update_emits_version_propose(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, title="Bot title", principal=agent(AGENT))

    events = service.list_events(limit=1, principal=owner())
    assert events[0].op == "version.propose"
    assert events[0].actor == AGENT
    assert events[0].seq == version.event_seq
    assert events[0].payload["node_id"] == note.id
    assert events[0].payload["proposed"]["title"] == "Bot title"


def test_human_update_still_applies_in_place(fresh_db):
    _, note = _graph()
    updated = service.update_node(note.id, content="Direct edit.", principal=owner())
    assert updated.content == "Direct edit."
    versions = service.history(note.id, principal=owner())
    assert [v.state for v in versions] == ["applied", "applied"]


def test_history_marks_proposed_versions(fresh_db):
    _, note = _graph()
    service.update_node(note.id, content="v2", principal=agent(AGENT))
    states = [v.state for v in service.history(note.id, principal=owner())]
    assert states == ["applied", "proposed"]


# ── Accept ────────────────────────────────────────────────────────────────────


def test_accept_applies_the_version(fresh_db):
    _, note = _graph()
    version = service.update_node(
        note.id,
        title="Better title",
        content="See [[Graph Theory]] deeply.",
        principal=agent(AGENT),
    )
    accepted = service.transition(str(version.id), "accept", principal=owner())

    assert accepted.state == "applied"
    node = service.get_node(note.id, principal=owner())
    assert node.title == "Better title"
    assert node.content == "See [[Graph Theory]] deeply."

    event = service.list_events(limit=1, principal=owner())[0]
    assert event.op == "node.update"
    assert event.actor == OWNER_ACTOR
    assert event.payload["applied_version_id"] == version.id
    assert event.payload["proposed_event_seq"] == version.event_seq


def test_accept_rematerializes_wikilinks(fresh_db):
    concept, note = _graph()
    other = service.create_node(type="concept", title="Topology", principal=owner())
    version = service.update_node(
        note.id, content="Now about [[Topology]] only.", principal=agent(AGENT)
    )
    service.transition(str(version.id), "accept", principal=owner())

    mentions = service.list_edges(
        node_id=note.id,
        type="mentions",
        state="active",
        principal=owner(),
    )
    assert [edge.dst_id for edge in mentions] == [other.id]
    archived = service.list_edges(
        node_id=note.id,
        type="mentions",
        state="archived",
        principal=owner(),
    )
    assert [edge.dst_id for edge in archived] == [concept.id]


def test_accepted_update_is_indexed_for_search(fresh_db):
    _, note = _graph()
    version = service.update_node(
        note.id, content="quixotic uniqueness token", principal=agent(AGENT)
    )
    from nodum import search as search_module

    assert search_module.search("quixotic", principal=owner()).hits == []
    service.transition(str(version.id), "accept", principal=owner())
    hits = search_module.search("quixotic", principal=owner()).hits
    assert [hit.node_id for hit in hits] == [note.id]


def test_accept_of_applied_version_fails(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", principal=agent(AGENT))
    service.transition(str(version.id), "accept", principal=owner())
    with pytest.raises(InvalidTransition):
        service.transition(str(version.id), "accept", principal=owner())


def test_applied_update_is_undoable(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="Bot rewrite.", principal=agent(AGENT))
    service.transition(str(version.id), "accept", principal=owner())
    # The accept's node.update is undoable like any other update. (Accepting
    # also archives the wikilink the rewrite dropped, so the latest event is
    # that edge.archive — name the node.update seq explicitly.)
    update_event = next(e for e in service.list_events(owner(), limit=10) if e.op == "node.update")
    service.undo(update_event.seq, principal=owner())
    assert service.get_node(note.id, principal=owner()).content == "See [[Graph Theory]]."


# ── Accepting applies only what the agent proposed ───────────────────────────


def test_accept_does_not_revert_edits_made_after_the_proposal(fresh_db):
    """The contract is "only the given fields change" — at accept time too.

    An agent proposes a content-only change; a human fixes the title while the
    proposal waits. Accepting must not replay the title the node had when the
    proposal was staged.
    """
    _, note = _graph()
    version = service.update_node(note.id, content="Bot rewrite.", principal=agent(AGENT))
    service.update_node(note.id, title="Human-corrected title", principal=owner())

    service.transition(str(version.id), "accept", principal=owner())

    node = service.get_node(note.id, principal=owner())
    assert node.title == "Human-corrected title"
    assert node.content == "Bot rewrite."


def test_two_queued_proposals_do_not_clobber_each_other(fresh_db):
    _, note = _graph()
    content_proposal = service.update_node(note.id, content="New body.", principal=agent(AGENT))
    title_proposal = service.update_node(note.id, title="New title", principal=agent("other"))

    service.transition(str(content_proposal.id), "accept", principal=owner())
    service.transition(str(title_proposal.id), "accept", principal=owner())

    node = service.get_node(note.id, principal=owner())
    assert (node.title, node.content) == ("New title", "New body.")


def test_props_proposal_leaves_title_and_content_alone(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, props={"reviewed": True}, principal=agent(AGENT))
    service.update_node(note.id, content="Human body.", principal=owner())

    service.transition(str(version.id), "accept", principal=owner())

    node = service.get_node(note.id, principal=owner())
    assert node.props == {"reviewed": True}
    assert node.content == "Human body."


def test_proposal_records_the_fields_it_names(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", principal=agent(AGENT))
    assert version.proposed_fields == ["content"]
    # The unnamed fields are still snapshotted as reviewer context.
    assert version.title == "My note"

    event = service.list_events(limit=1, principal=owner())[0]
    assert event.payload["fields"] == ["content"]

    (proposal,) = service.list_proposals(kind="update", principal=owner())
    assert proposal.version.proposed_fields == ["content"]
    # Applied snapshots are not proposals and name no fields.
    assert service.history(note.id, principal=owner())[0].proposed_fields is None


def test_accept_event_records_the_fields_it_applied(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, title="T2", props={"k": 1}, principal=agent(AGENT))
    service.transition(str(version.id), "accept", principal=owner())
    event = service.list_events(limit=1, principal=owner())[0]
    assert event.op == "node.update"
    assert event.payload["applied_fields"] == ["title", "props"]


def test_a_proposal_predating_the_column_still_applies_whole(fresh_db):
    """`proposed_fields` NULL means "staged before migration 0008" — apply all."""
    _, note = _graph()
    version = service.update_node(
        note.id, title="Legacy title", content="Legacy body.", principal=agent(AGENT)
    )
    conn = db.connect()
    try:
        conn.execute("UPDATE versions SET proposed_fields = NULL WHERE id = ?", (version.id,))
        conn.commit()
    finally:
        conn.close()

    service.transition(str(version.id), "accept", principal=owner())
    node = service.get_node(note.id, principal=owner())
    assert (node.title, node.content) == ("Legacy title", "Legacy body.")


# ── Reject ────────────────────────────────────────────────────────────────────


def test_reject_archives_the_version_and_records_reason(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="Bot rewrite.", principal=agent(AGENT))
    rejected = service.transition(str(version.id), "reject", principal=owner())

    assert rejected.state == "archived"
    assert service.get_node(note.id, principal=owner()).content == "See [[Graph Theory]]."
    event = service.list_events(limit=1, principal=owner())[0]
    assert event.op == "version.reject"


def test_archive_action_does_not_apply_to_versions(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", principal=agent(AGENT))
    with pytest.raises(InvalidTransition):
        service.transition(str(version.id), "archive", principal=owner())


def test_unknown_version_id(fresh_db):
    with pytest.raises(VersionNotFound):
        service.diff_versions(999, 1000, principal=owner())


# ── Review queue integration ──────────────────────────────────────────────────


def test_update_proposals_appear_in_the_queue(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", principal=agent(AGENT))
    proposals = service.list_proposals(principal=owner())

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
    service.update_node(note.id, content="v2", principal=agent(AGENT))
    service.update_node(note.id, content="v3", principal=agent("other"))

    assert len(service.list_proposals(kind="update", principal=owner())) == 2
    assert len(service.list_proposals(kind="node", principal=owner())) == 0
    assert len(service.list_proposals(kind="update", created_by=AGENT, principal=owner())) == 1
    assert len(service.list_proposals(kind="update", type="note", principal=owner())) == 2
    assert len(service.list_proposals(kind="update", type="mentions", principal=owner())) == 0


def test_batch_accept_applies_updates(fresh_db):
    _, note = _graph()
    version = service.update_node(note.id, content="v2", principal=agent(AGENT))
    result = service.accept_proposals([str(version.id), "missing-id"], principal=owner())

    assert result.transitioned == [str(version.id)]
    assert len(result.failed) == 1
    assert service.get_node(note.id, principal=owner()).content == "v2"


def test_batch_reject_by_filter_covers_updates(fresh_db):
    _, note = _graph()
    service.update_node(note.id, content="v2", principal=agent(AGENT))
    result = service.reject_matching(reason="not good enough", created_by=AGENT, principal=owner())

    assert len(result.transitioned) == 1
    assert service.list_proposals(principal=owner()) == []
    event = service.list_events(limit=1, principal=owner())[0]
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
