"""Cross-space isolation: the cases a single-space suite cannot see.

Every other test file grants its agents both seeded spaces, so the scope
filters never actually filter (Q13 adversarial review, suite blind spot).
Here an agent always lacks a grant on some space that exists — which is what
turns the scoped store's filters, the mention lifecycle's authority checks
and the "unreadable does not exist" rule into something a test can fail.
"""

from __future__ import annotations

import pytest
from helpers import agent, owner, seed_space

from nodum import search, service
from nodum.store import GrantNotPermitted


def _edge_states(src_id):
    """Every mentions edge out of ``src_id``, as ``{dst_id: state}`` (human view)."""
    return {
        edge.dst_id: edge.state
        for edge in service.list_edges(node_id=src_id, type="mentions", principal=owner())
    }


def _far_space():
    """A second space ``b`` holding one node, both created by the owner."""
    seed_space("b")
    target = service.create_node(type="concept", title="B thing", space="b", principal=owner())
    return target


# ── B1: node accept must not activate edges into spaces it has no say over ────


def test_accepting_a_node_leaves_cross_space_mentions_proposed(fresh_db):
    target = _far_space()
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest", "b": "suggest"})
    note = service.create_node(
        type="note", title="S", content="See [[B thing]].", principal=proposer
    )
    acceptor = agent("acceptor", grants={"meta": "read", "main": "edit"})

    service.transition(note.id, "accept", principal=acceptor)

    # The node is live; the edge into b is not — the acceptor holds nothing
    # on b, and accepting the node must not be a way around that.
    assert service.get_node(note.id, principal=owner()).state == "active"
    assert _edge_states(note.id) == {target.id: "proposed"}


def test_accepting_a_node_activates_mentions_the_acceptor_may_review(fresh_db):
    target = _far_space()
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest", "b": "suggest"})
    note = service.create_node(
        type="note", title="S", content="See [[B thing]].", principal=proposer
    )
    acceptor = agent("acceptor", grants={"meta": "read", "main": "edit", "b": "edit"})

    service.transition(note.id, "accept", principal=acceptor)

    assert _edge_states(note.id) == {target.id: "active"}


# ── B2: re-materialisation must not retire edges the writer cannot adjudicate ─


def test_identical_save_keeps_a_cross_space_mention_the_writer_cannot_see(fresh_db):
    target = _far_space()
    content = "See [[B thing]]."
    note = service.create_node(type="note", title="S", content=content, principal=owner())
    assert _edge_states(note.id) == {target.id: "active"}

    editor = agent("editor", grants={"meta": "read", "main": "edit"})
    service.update_node(note.id, content=content, principal=editor)

    # The link text never changed; the editor simply cannot resolve it. An
    # unreadable target is not a removed one.
    assert _edge_states(note.id) == {target.id: "active"}


def test_dropping_the_link_does_not_retire_a_cross_space_mention_either(fresh_db):
    target = _far_space()
    note = service.create_node(
        type="note", title="S", content="See [[B thing]].", principal=owner()
    )
    editor = agent("editor", grants={"meta": "read", "main": "edit"})

    service.update_node(note.id, content="Nothing here.", principal=editor)

    # Retiring the edge is a state change on b as much as on main.
    assert _edge_states(note.id) == {target.id: "active"}


def test_a_writer_granted_both_spaces_still_retires_the_mention(fresh_db):
    target = _far_space()
    note = service.create_node(
        type="note", title="S", content="See [[B thing]].", principal=owner()
    )
    editor = agent("editor", grants={"meta": "read", "main": "edit", "b": "edit"})

    service.update_node(note.id, content="Nothing here.", principal=editor)

    assert _edge_states(note.id) == {target.id: "archived"}


def test_in_space_mentions_still_come_and_go_under_a_single_grant(fresh_db):
    _far_space()
    target = service.create_node(type="concept", title="A thing", principal=owner())
    editor = agent("editor", grants={"meta": "read", "main": "edit"})
    note = service.create_node(type="note", title="S", content="See [[A thing]].", principal=editor)
    assert _edge_states(note.id) == {target.id: "active"}

    service.update_node(note.id, content="Nothing here.", principal=editor)

    assert _edge_states(note.id) == {target.id: "archived"}


# ── S3: an ungranted space and a nonexistent one answer identically ───────────


def test_writing_to_an_ungranted_space_looks_exactly_like_a_missing_space(fresh_db):
    _far_space()
    writer = agent("writer", grants={"meta": "read", "main": "edit"})

    with pytest.raises(service.TypeNotFound) as existing:
        service.create_node(type="note", title="x", space="b", principal=writer)
    with pytest.raises(service.TypeNotFound) as missing:
        service.create_node(type="note", title="x", space="nope", principal=writer)

    # Same wording, same class — only the ref the caller typed differs.
    assert str(existing.value) == "unknown space: b"
    assert str(missing.value) == "unknown space: nope"


def test_a_readable_but_unwritable_space_refuses_with_a_grant_error(fresh_db):
    _far_space()
    reader = agent("reader", grants={"meta": "read", "main": "edit", "b": "read"})

    with pytest.raises(GrantNotPermitted):
        service.create_node(type="note", title="x", space="b", principal=reader)


# ── S1: diff is not a version-id enumeration oracle ───────────────────────────


def test_diff_refuses_versions_on_an_unreadable_node(fresh_db):
    target = _far_space()
    service.update_node(target.id, content="changed", principal=owner())
    first, second = service.history(target.id, principal=owner())
    reader = agent("reader", grants={"meta": "read", "main": "read"})

    with pytest.raises(service.VersionNotFound):
        service.diff_versions(first.id, second.id, principal=reader)


# ── S4: batch-by-filter never names an out-of-scope proposal ──────────────────


def test_accept_matching_ignores_proposals_outside_the_read_scope(fresh_db):
    _far_space()
    far = agent("far", grants={"meta": "read", "b": "suggest"})
    far_proposal = service.create_node(type="note", title="far", space="b", principal=far)
    near = agent("near", grants={"meta": "read", "main": "edit"})
    near_proposal = service.create_node(type="note", title="near", principal=near)
    service.transition(near_proposal.id, "archive", principal=near)

    result = service.accept_matching(principal=near)

    assert far_proposal.id not in [failure.id for failure in result.failed]
    assert far_proposal.id not in result.transitioned
    assert service.get_node(far_proposal.id, principal=owner()).state == "proposed"


# ── N1: the type catalog is not an existence oracle either ────────────────────


def test_a_type_in_an_unreadable_space_does_not_resolve(fresh_db):
    seed_space("b")
    service.create_node(
        type="type",
        title="secret-kind",
        space="b",
        props={"type_kind": "node"},
        principal=owner(),
    )
    blind = agent("blind", grants={"main": "edit"})

    with pytest.raises(service.TypeNotFound):
        service.list_nodes(type="secret-kind", principal=blind)
    with pytest.raises(ValueError, match="unknown node type"):
        search.search("anything", type="secret-kind", principal=blind)
