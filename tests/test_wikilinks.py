"""Wikilink materialisation: [[Target]] in content ⇄ active mentions edges."""

from __future__ import annotations

from nodum import service


def _mentions(src_id):
    return service.list_edges(node_id=src_id, type="mentions", state="active")


def test_link_by_title_materializes_mentions_edge(fresh_db):
    target = service.create_node(type="concept", title="Graph Theory")
    source = service.create_node(type="note", title="S", content="See [[Graph Theory]].")
    edges = _mentions(source.id)
    assert len(edges) == 1
    assert edges[0].src_id == source.id
    assert edges[0].dst_id == target.id
    assert edges[0].created_by == "human"
    assert edges[0].state == "active"


def test_link_by_id_resolves(fresh_db):
    target = service.create_node(type="concept", title="T")
    source = service.create_node(type="note", title="S", content=f"See [[{target.id}]].")
    edges = _mentions(source.id)
    assert [edge.dst_id for edge in edges] == [target.id]


def test_unresolvable_target_is_skipped_silently(fresh_db):
    source = service.create_node(type="note", title="S", content="See [[No Such Node]].")
    assert _mentions(source.id) == []


def test_self_link_is_ignored(fresh_db):
    node = service.create_node(type="note", title="Me")
    updated = service.update_node(node.id, content=f"I link to [[Me]] and [[{node.id}]].")
    assert _mentions(updated.id) == []


def test_update_re_resolves_new_links(fresh_db):
    target = service.create_node(type="concept", title="New Concept")
    source = service.create_node(type="note", title="S", content="plain text")
    assert _mentions(source.id) == []
    service.update_node(source.id, content="now links to [[New Concept]]")
    assert [edge.dst_id for edge in _mentions(source.id)] == [target.id]


def test_removing_link_text_archives_the_edge(fresh_db):
    service.create_node(type="concept", title="Gone Soon")
    source = service.create_node(type="note", title="S", content="link [[Gone Soon]]")
    (edge,) = _mentions(source.id)
    service.update_node(source.id, content="no more link")
    assert _mentions(source.id) == []
    # The edge still exists, archived — not deleted.
    archived = service.list_edges(node_id=source.id, type="mentions", state="archived")
    assert [item.id for item in archived] == [edge.id]


def test_partial_edit_only_touches_changed_links(fresh_db):
    keep = service.create_node(type="concept", title="Keep")
    service.create_node(type="concept", title="Drop")
    source = service.create_node(type="note", title="S", content="[[Keep]] and [[Drop]]")
    (keep_edge,) = [e for e in _mentions(source.id) if e.dst_id == keep.id]
    service.update_node(source.id, content="[[Keep]] only")
    remaining = _mentions(source.id)
    # The surviving edge is the original row, not a re-created one.
    assert [edge.id for edge in remaining] == [keep_edge.id]


def test_unchanged_content_does_not_duplicate_edges(fresh_db):
    service.create_node(type="concept", title="Dup")
    source = service.create_node(type="note", title="S", content="[[Dup]] and [[Dup]] again")
    assert len(_mentions(source.id)) == 1
    service.update_node(source.id, title="S2")  # no content change
    assert len(_mentions(source.id)) == 1


def test_wikilink_events_are_logged(fresh_db):
    service.create_node(type="concept", title="C")
    source = service.create_node(type="note", title="S", content="[[C]]")
    events = service.list_events(limit=10)
    edge_ops = [e.op for e in events if e.op.startswith("edge.")]
    assert "edge.create" in edge_ops
    service.update_node(source.id, content="cleared")
    events = service.list_events(limit=10)
    edge_ops = [e.op for e in events if e.op.startswith("edge.")]
    assert "edge.archive" in edge_ops


def test_title_becomes_resolvable_after_the_fact(fresh_db):
    # A link written before the target exists is skipped; it materialises only
    # if the source content is (re-)written after the target exists.
    source = service.create_node(type="note", title="S", content="[[Late Target]]")
    assert _mentions(source.id) == []
    target = service.create_node(type="concept", title="Late Target")
    assert _mentions(source.id) == []  # no retroactive scan on target create
    service.update_node(source.id, content="[[Late Target]]")
    assert [edge.dst_id for edge in _mentions(source.id)] == [target.id]
