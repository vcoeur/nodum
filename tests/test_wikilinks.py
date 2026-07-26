"""Wikilink materialisation: [[Target]] in content ⇄ active mentions edges."""

from __future__ import annotations

from helpers import OWNER_ACTOR, agent, owner

from nodum import service


def _mentions(src_id):
    return service.list_edges(node_id=src_id, type="mentions", state="active", principal=owner())


def test_link_by_title_materializes_mentions_edge(fresh_db):
    target = service.create_node(type="concept", title="Graph Theory", principal=owner())
    source = service.create_node(
        type="note",
        title="S",
        content="See [[Graph Theory]].",
        principal=owner(),
    )
    edges = _mentions(source.id)
    assert len(edges) == 1
    assert edges[0].src_id == source.id
    assert edges[0].dst_id == target.id
    assert edges[0].created_by == OWNER_ACTOR
    assert edges[0].state == "active"


def test_link_by_id_resolves(fresh_db):
    target = service.create_node(type="concept", title="T", principal=owner())
    source = service.create_node(
        type="note",
        title="S",
        content=f"See [[{target.id}]].",
        principal=owner(),
    )
    edges = _mentions(source.id)
    assert [edge.dst_id for edge in edges] == [target.id]


def test_unresolvable_target_is_skipped_silently(fresh_db):
    source = service.create_node(
        type="note",
        title="S",
        content="See [[No Such Node]].",
        principal=owner(),
    )
    assert _mentions(source.id) == []


def test_self_link_is_ignored(fresh_db):
    node = service.create_node(type="note", title="Me", principal=owner())
    updated = service.update_node(
        node.id,
        content=f"I link to [[Me]] and [[{node.id}]].",
        principal=owner(),
    )
    assert _mentions(updated.id) == []


def test_update_re_resolves_new_links(fresh_db):
    target = service.create_node(type="concept", title="New Concept", principal=owner())
    source = service.create_node(type="note", title="S", content="plain text", principal=owner())
    assert _mentions(source.id) == []
    service.update_node(source.id, content="now links to [[New Concept]]", principal=owner())
    assert [edge.dst_id for edge in _mentions(source.id)] == [target.id]


def test_removing_link_text_archives_the_edge(fresh_db):
    service.create_node(type="concept", title="Gone Soon", principal=owner())
    source = service.create_node(
        type="note",
        title="S",
        content="link [[Gone Soon]]",
        principal=owner(),
    )
    (edge,) = _mentions(source.id)
    service.update_node(source.id, content="no more link", principal=owner())
    assert _mentions(source.id) == []
    # The edge still exists, archived — not deleted.
    archived = service.list_edges(
        node_id=source.id,
        type="mentions",
        state="archived",
        principal=owner(),
    )
    assert [item.id for item in archived] == [edge.id]


def test_partial_edit_only_touches_changed_links(fresh_db):
    keep = service.create_node(type="concept", title="Keep", principal=owner())
    service.create_node(type="concept", title="Drop", principal=owner())
    source = service.create_node(
        type="note",
        title="S",
        content="[[Keep]] and [[Drop]]",
        principal=owner(),
    )
    (keep_edge,) = [e for e in _mentions(source.id) if e.dst_id == keep.id]
    service.update_node(source.id, content="[[Keep]] only", principal=owner())
    remaining = _mentions(source.id)
    # The surviving edge is the original row, not a re-created one.
    assert [edge.id for edge in remaining] == [keep_edge.id]


def test_unchanged_content_does_not_duplicate_edges(fresh_db):
    service.create_node(type="concept", title="Dup", principal=owner())
    source = service.create_node(
        type="note",
        title="S",
        content="[[Dup]] and [[Dup]] again",
        principal=owner(),
    )
    assert len(_mentions(source.id)) == 1
    service.update_node(source.id, title="S2", principal=owner())  # no content change
    assert len(_mentions(source.id)) == 1


def test_wikilink_events_are_logged(fresh_db):
    service.create_node(type="concept", title="C", principal=owner())
    source = service.create_node(type="note", title="S", content="[[C]]", principal=owner())
    events = service.list_events(limit=10, principal=owner())
    edge_ops = [e.op for e in events if e.op.startswith("edge.")]
    assert "edge.create" in edge_ops
    service.update_node(source.id, content="cleared", principal=owner())
    events = service.list_events(limit=10, principal=owner())
    edge_ops = [e.op for e in events if e.op.startswith("edge.")]
    assert "edge.archive" in edge_ops


# ── Materialised edges inherit the actor's state ──────────────────────────────

AGENT = "agent:researcher"


def _mentions_in(src_id, state):
    return service.list_edges(node_id=src_id, type="mentions", state=state, principal=owner())


def test_agent_create_materializes_a_proposed_edge(fresh_db):
    """An agent may not attach live structure to a human's active node."""
    target = service.create_node(type="concept", title="Human Concept", principal=owner())
    source = service.create_node(
        type="note", title="Bot note", content="See [[Human Concept]].", principal=agent(AGENT)
    )
    assert _mentions_in(source.id, "active") == []
    (edge,) = _mentions_in(source.id, "proposed")
    assert edge.dst_id == target.id
    assert edge.created_by == AGENT
    # The human's node shows no new live neighbour.
    assert service.get_neighborhood(target.id, depth=1, principal=owner()).edges == []


def test_agent_mentions_edge_is_a_propose_event(fresh_db):
    service.create_node(type="concept", title="C", principal=owner())
    service.create_node(type="note", title="S", content="[[C]]", principal=agent(AGENT))
    ops = [e.op for e in service.list_events(owner(), limit=10) if e.op.startswith("edge.")]
    assert ops == ["edge.propose"]


def test_accepting_the_node_activates_its_pending_mentions(fresh_db):
    target = service.create_node(type="concept", title="Human Concept", principal=owner())
    source = service.create_node(
        type="note", title="Bot note", content="See [[Human Concept]].", principal=agent(AGENT)
    )
    (pending,) = _mentions_in(source.id, "proposed")

    service.transition(source.id, "accept", principal=owner())
    assert _mentions_in(source.id, "proposed") == []
    (live,) = _mentions_in(source.id, "active")
    assert live.id == pending.id
    assert live.dst_id == target.id
    # The accept is the human's event, not the proposer's.
    accept_event = next(e for e in service.list_events(owner(), limit=10) if e.op == "edge.accept")
    assert accept_event.actor == OWNER_ACTOR


def test_accepting_a_node_leaves_another_agents_pending_edge_alone(fresh_db):
    service.create_node(type="concept", title="C", principal=owner())
    source = service.create_node(type="note", title="S", content="[[C]]", principal=agent(AGENT))
    other = service.create_node(type="concept", title="Other", principal=owner())
    outsider = service.create_edge(source.id, other.id, "mentions", principal=agent("outsider"))

    service.transition(source.id, "accept", principal=owner())
    assert [e.id for e in _mentions_in(source.id, "proposed")] == [outsider.id]


def test_human_rewrite_does_not_duplicate_a_pending_edge(fresh_db):
    service.create_node(type="concept", title="C", principal=owner())
    source = service.create_node(type="note", title="S", content="[[C]]", principal=agent(AGENT))
    (pending,) = _mentions_in(source.id, "proposed")

    service.update_node(source.id, content="still about [[C]]", principal=owner())
    assert [e.id for e in _mentions_in(source.id, "proposed")] == [pending.id]
    assert _mentions_in(source.id, "active") == []


def test_dropping_the_text_archives_a_pending_edge(fresh_db):
    service.create_node(type="concept", title="C", principal=owner())
    source = service.create_node(type="note", title="S", content="[[C]]", principal=agent(AGENT))
    service.update_node(source.id, content="no link now", principal=owner())
    assert _mentions_in(source.id, "proposed") == []
    assert len(_mentions_in(source.id, "archived")) == 1
    # `proposed → archived` is a reject, not an archive (design §6).
    assert [e.op for e in service.list_events(owner(), limit=2) if e.op.startswith("edge.")] == [
        "edge.reject"
    ]


def test_accepting_an_agent_update_materializes_as_the_reviewer(fresh_db):
    concept = service.create_node(type="concept", title="Graph Theory", principal=owner())
    other = service.create_node(type="concept", title="Topology", principal=owner())
    note = service.create_node(
        type="note",
        title="N",
        content="See [[Graph Theory]].",
        principal=owner(),
    )

    version = service.update_node(note.id, content="Now [[Topology]].", principal=agent(AGENT))
    service.transition(str(version.id), "accept", principal=owner())

    (live,) = _mentions_in(note.id, "active")
    assert live.dst_id == other.id
    # Both the new edge and the archived one are the reviewer's doing.
    assert live.created_by == OWNER_ACTOR
    edge_events = [e for e in service.list_events(owner(), limit=10) if e.op.startswith("edge.")]
    assert {e.actor for e in edge_events} == {OWNER_ACTOR}
    assert [e.dst_id for e in _mentions_in(note.id, "archived")] == [concept.id]


def test_title_becomes_resolvable_after_the_fact(fresh_db):
    # A link written before the target exists is skipped; it materialises only
    # if the source content is (re-)written after the target exists.
    source = service.create_node(
        type="note",
        title="S",
        content="[[Late Target]]",
        principal=owner(),
    )
    assert _mentions(source.id) == []
    target = service.create_node(type="concept", title="Late Target", principal=owner())
    assert _mentions(source.id) == []  # no retroactive scan on target create
    service.update_node(source.id, content="[[Late Target]]", principal=owner())
    assert [edge.dst_id for edge in _mentions(source.id)] == [target.id]
