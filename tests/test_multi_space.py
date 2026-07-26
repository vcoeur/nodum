"""Cross-space isolation: the cases a single-space suite cannot see.

Every other test file grants its agents both seeded spaces, so the scope
filters never actually filter (Q13 adversarial review, suite blind spot).
Here an agent always lacks a grant on some space that exists — which is what
turns the scoped store's filters, the mention lifecycle's authority checks
and the "unreadable does not exist" rule into something a test can fail.
"""

from __future__ import annotations

import sqlite3

import pytest
from helpers import agent, owner, seed_space

from nodum import db, search, service
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


# ── The read-side space filter composes with the grant scope, never replaces it ─


def _three_spaces():
    """``main`` plus ``b`` and ``c``, each holding one owner-written node.

    Returns ``{space_id: node}``. Every node shares the word ``territory`` so
    one query reaches all three and the *filter* is what distinguishes them —
    a query that only ever matched one node could not tell a working filter
    from a broken one.
    """
    nodes = {}
    for space in ("main", "b", "c"):
        if space != "main":
            seed_space(space)
        nodes[space] = service.create_node(
            type="note",
            title=f"{space} thing",
            content=f"territory notes for {space}",
            space=space,
            principal=owner(),
        )
    return nodes


def _scout():
    """An agent reading a **strict subset** of the spaces that exist: no ``c``."""
    return agent("scout", grants={"meta": "read", "main": "read", "b": "read"})


def test_the_listing_scope_is_not_vacuous_before_any_filter_is_applied(fresh_db):
    """The premise every test below rests on: the scout genuinely cannot see ``c``.

    Q13's root defect was fixtures that held every grant, which made the scope
    clauses filter nothing while every test still passed. This asserts the
    fixture itself is not that: the owner sees three spaces, the scout two.
    """
    nodes = _three_spaces()
    scout = _scout()

    seen = {node.id for node in service.list_nodes(principal=scout, limit=500)}
    assert nodes["main"].id in seen
    assert nodes["b"].id in seen
    assert nodes["c"].id not in seen
    # And the owner does see it, so `c` is a real space holding a real node.
    assert nodes["c"].id in {node.id for node in service.list_nodes(principal=owner(), limit=500)}


def test_the_space_filter_narrows_a_listing_within_the_read_set(fresh_db):
    nodes = _three_spaces()
    scout = _scout()

    listed = service.list_nodes(space="b", principal=scout, limit=500)

    assert [node.id for node in listed] == [nodes["b"].id]
    # The owner, unfiltered by grants, gets the same narrowing from the filter
    # alone — this is a convenience, not a boundary.
    owned = service.list_nodes(space="b", principal=owner(), limit=500)
    assert [node.id for node in owned] == [nodes["b"].id]


def test_listing_a_space_the_principal_cannot_read_is_the_unknown_space_answer(fresh_db):
    """Not an error that leaks: the ungranted space reads exactly like a missing one."""
    _three_spaces()
    scout = _scout()

    with pytest.raises(service.TypeNotFound) as existing:
        service.list_nodes(space="c", principal=scout)
    with pytest.raises(service.TypeNotFound) as missing:
        service.list_nodes(space="nope", principal=scout)

    assert str(existing.value) == "unknown space: c"
    assert str(missing.value) == "unknown space: nope"
    # The owner proves `c` is there to be found — so the scout's answer is the
    # scope talking, not an empty database.
    assert len(service.list_nodes(space="c", principal=owner(), limit=500)) == 1


def test_an_agent_with_no_grants_can_name_no_space_at_all(fresh_db):
    _three_spaces()
    blind = agent("blind", grants={})

    assert service.list_nodes(principal=blind, limit=500) == []
    for space in ("main", "b", "c"):
        with pytest.raises(service.TypeNotFound):
            service.list_nodes(space=space, principal=blind)


def test_the_search_filter_narrows_within_the_read_set_too(fresh_db):
    nodes = _three_spaces()
    scout = _scout()

    unfiltered = search.search("territory", principal=scout)
    assert {hit.node_id for hit in unfiltered.hits} == {nodes["main"].id, nodes["b"].id}

    narrowed = search.search("territory", space="b", principal=scout)
    assert [hit.node_id for hit in narrowed.hits] == [nodes["b"].id]

    # Three spaces hold the term; the owner sees all three, so the scout's two
    # are a scope effect and the one is a filter effect.
    assert len({hit.node_id for hit in search.search("territory", principal=owner()).hits}) == 3
    assert [
        hit.node_id for hit in search.search("territory", space="c", principal=owner()).hits
    ] == [nodes["c"].id]


def test_searching_a_space_the_principal_cannot_read_is_the_unknown_space_answer(fresh_db):
    _three_spaces()
    scout = _scout()

    with pytest.raises(ValueError) as existing:
        search.search("territory", space="c", principal=scout)
    with pytest.raises(ValueError) as missing:
        search.search("territory", space="nope", principal=scout)

    assert str(existing.value) == "unknown space: c"
    assert str(missing.value) == "unknown space: nope"


def test_the_space_clause_is_anded_onto_the_scope_and_never_replaces_it(fresh_db):
    """The invariant, checked where a refactor would break it: both clauses stand.

    Resolution already refuses to hand an agent the id of a space it cannot
    read, so a runtime call cannot reach this state — which is exactly why the
    SQL builder is worth pinning directly. If a later edit made the space
    filter an *alternative* to the scope clause rather than an addition, every
    behavioural test above would still pass and this one would not.
    """
    _three_spaces()
    scout = _scout()

    clauses, params = search._node_filters(None, None, None, None, None, False, "c", scout)

    scope = [clause for clause in clauses if "IN (" in clause]
    assert scope, f"the principal's scope clause must survive the filter: {clauses}"
    assert "n.space_id = ?" in clauses
    assert "c" in params and set(scout.read_spaces) <= set(params)


def test_a_filtered_search_does_not_reach_out_of_the_space_through_expansion(fresh_db):
    """One-hop expansion respects the filter: a narrowed search stays narrowed."""
    nodes = _three_spaces()
    service.create_edge(
        nodes["main"].id,
        nodes["b"].id,
        "relates_to",
        principal=owner(),
    )

    expanded = search.search("territory", space="main", expand=True, principal=owner())

    assert [hit.node_id for hit in expanded.hits] == [nodes["main"].id]
    # Unfiltered, the same edge does produce the neighbour.
    everything = search.search("main thing", expand=True, principal=owner())
    assert nodes["b"].id in {hit.node_id for hit in everything.hits}


# ── include_meta: off by default, and naming meta is the opt-in said precisely ─


def test_meta_is_excluded_by_default_and_included_on_request(fresh_db):
    _three_spaces()

    default = service.list_nodes(principal=owner(), limit=500)
    assert [node for node in default if node.space_id == "meta"] == []

    with_meta = service.list_nodes(include_meta=True, principal=owner(), limit=500)
    assert [node for node in with_meta if node.space_id == "meta"] != []


def test_naming_the_meta_space_is_itself_the_opt_in(fresh_db):
    """Otherwise the filter would be a trap: `meta` is in the space list itself."""
    listed = service.list_nodes(space="meta", principal=owner(), limit=500)

    assert listed, "filtering to meta must not be silently emptied by the default exclusion"
    assert {node.space_id for node in listed} == {"meta"}


def test_search_excludes_meta_by_default_and_finds_it_when_narrowed_to_it(fresh_db):
    service.create_node(
        type="type",
        title="territory-kind",
        content="territory vocabulary",
        space="meta",
        props={"type_kind": "node"},
        principal=owner(),
    )
    service.create_node(type="note", title="ordinary", content="territory notes", principal=owner())

    default = search.search("territory", principal=owner())
    assert [hit.title for hit in default.hits] == ["ordinary"]

    assert "territory-kind" in {
        hit.title for hit in search.search("territory", include_meta=True, principal=owner()).hits
    }
    assert [
        hit.title for hit in search.search("territory", space="meta", principal=owner()).hits
    ] == ["territory-kind"]


# ── The structural spaces are not archivable, on any surface ──────────────────


@pytest.mark.parametrize("structural", ["main", "meta"])
def test_a_structural_space_cannot_be_archived(fresh_db, structural):
    """Archiving `main` is destructive in the quietest way there is.

    `list_spaces` returns active spaces only, so it vanishes from every picker,
    while `resolve_space_id(None)` keeps returning `main` without ever reading
    the row's state — writes go on landing in a space the human can no longer
    see or name. Archiving `meta` retires the space that holds every space.
    Neither is reversible: there is no `active ← archived` transition anywhere.
    """
    _three_spaces()

    with pytest.raises(service.InvalidTransition, match=f"cannot archive the '{structural}' space"):
        service.archive_space(structural, principal=owner())

    # Still active, still resolving, still taking writes.
    assert structural in {space.id for space in service.list_spaces(principal=owner())}
    assert service.resolve_space_id(structural, principal=owner()) == structural
    assert service.resolve_space_id(None, principal=owner()) == "main"


def test_an_ordinary_space_is_still_archivable(fresh_db):
    """The guard is two ids, not a ban on archiving."""
    _three_spaces()

    archived = service.archive_space("b", principal=owner())

    assert archived.state == "archived"
    assert "b" not in {space.id for space in service.list_spaces(principal=owner())}


@pytest.mark.parametrize("structural", ["main", "meta"])
def test_renaming_a_structural_space_is_still_allowed(fresh_db, structural):
    """Rename touches the title; it is the **id** everything structural depends on."""
    renamed = service.rename_space(structural, f"{structural}-renamed", principal=owner())

    assert renamed.id == structural
    assert renamed.title == f"{structural}-renamed"
    assert service.resolve_space_id(None, principal=owner()) == "main"
    # Resolvable by the new name and, since ids are unchanged, by the old string
    # too — which for these two happens to be the id.
    assert service.resolve_space_id(f"{structural}-renamed", principal=owner()) == structural
    assert service.resolve_space_id(structural, principal=owner()) == structural


def test_a_principal_that_cannot_read_main_meets_unknown_space_not_the_refusal(fresh_db):
    """Resolution first, refusal second — the guard must not become an oracle.

    The scout reads `main` but not `c`; an agent granted neither reads neither,
    and for it `main` has to answer exactly as a space that is not there does.
    """
    _three_spaces()
    outsider = agent("outsider", grants={"b": "read"})

    with pytest.raises(service.TypeNotFound, match="unknown space: main"):
        service.archive_space("main", principal=outsider)
    with pytest.raises(service.TypeNotFound, match="unknown space: nope"):
        service.archive_space("nope", principal=outsider)
    # And the fixture is not vacuous: `b` *does* resolve for this principal, and
    # is then refused on its own merits — a space node lives in meta, which the
    # outsider cannot read, so the transition answers "no such record".
    with pytest.raises(service.RecordNotFound):
        service.archive_space("b", principal=outsider)


# ── One space per name, in any state, enforced where every surface inherits it ─


def test_a_second_space_cannot_take_a_live_space_name(fresh_db):
    """`_resolve_space` matches `id = ? OR title = ?`; two matches is ambiguity."""
    _three_spaces()

    with pytest.raises(service.SpaceNameTaken, match="a space already answers to 'b'"):
        service.create_space("b", principal=owner())
    with pytest.raises(service.SpaceNameTaken, match="a space already answers to 'main'"):
        service.create_space("main", principal=owner())


def test_a_rename_cannot_collide_with_another_space_either(fresh_db):
    _three_spaces()

    with pytest.raises(service.SpaceNameTaken, match="a space already answers to 'c'"):
        service.rename_space("b", "c", principal=owner())
    # Renaming a space to a name it already holds is a no-op, not a self-clash.
    assert service.rename_space("b", "b", principal=owner()).title == "b"


def test_the_generic_node_path_cannot_route_around_the_name_rule(fresh_db):
    """`create_space`/`rename_space` are conveniences; `node create` is the bypass."""
    _three_spaces()

    with pytest.raises(service.SpaceNameTaken, match="a space already answers to 'b'"):
        service.create_node(type="space", title="b", space="meta", principal=owner())
    space_b = service.resolve_space_id("b", principal=owner())
    with pytest.raises(service.SpaceNameTaken, match="a space already answers to 'c'"):
        service.update_node(space_b, title="c", principal=owner())


def test_the_schema_holds_the_rule_under_the_service(fresh_db):
    """Migration 0013's unique index, checked where no Python guard can run."""
    _three_spaces()
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
                " VALUES ('sneaky', 'meta', 'space', 'b', 'human:owner')"
            )
    finally:
        conn.close()


def test_archiving_a_space_keeps_its_name_reserved(fresh_db):
    """The exact reproduction that disproved the old rule, first half.

    Archived titles used to be freed, on the argument that an archived space
    stops resolving and that the state machine has no un-archive. `undo` is the
    un-archive: it writes the `before` row back with a raw UPDATE, past
    `TRANSITIONS`. So the freed name could be re-taken and the restore would
    then die on the unique index. A space keeps its name instead — the cost,
    taken knowingly, is exactly this refusal.
    """
    _three_spaces()
    service.archive_space("b", principal=owner())

    with pytest.raises(service.SpaceNameTaken) as refused:
        service.create_space("b", principal=owner())

    # The message has to say *archived*: nothing lists archived spaces, so a
    # bare "that name is taken" would name something the human cannot see.
    assert "archived space already answers to 'b'" in str(refused.value)
    assert "id b" in str(refused.value)
    # The generic path is refused for the same reason, in the same words.
    with pytest.raises(service.SpaceNameTaken, match="archived space already answers to 'b'"):
        service.create_node(type="space", title="b", space="meta", principal=owner())
    # Nothing was created, and the archived row is still archived.
    assert service.get_node("b", principal=owner()).state == "archived"
    assert [space.id for space in service.list_spaces(principal=owner())] == ["meta", "main", "c"]


def test_undoing_an_archive_restores_the_space_it_retired(fresh_db):
    """The exact reproduction, second half: the restore that used to fail.

    `undo` of a `node.archive` is the only route back from an archive, and with
    the name reserved it can no longer land on a title something else took —
    which is the whole point of reserving it.
    """
    _three_spaces()
    service.archive_space("b", principal=owner())
    archive_seq = next(
        event.seq
        for event in service.list_events(limit=50, principal=owner())
        if event.op == "node.archive"
    )

    undone = service.undo(archive_seq, principal=owner())

    assert undone.undone_op == "node.archive"
    assert service.get_node("b", principal=owner()).state == "active"
    # And it resolves again, by the name it never lost.
    assert service.resolve_space_id("b", principal=owner()) == "b"
    assert service.list_nodes(space="b", principal=owner()) != []


def test_undoing_a_rename_onto_a_taken_name_is_refused_not_an_integrity_error(fresh_db):
    """The collision reserving titles does *not* remove, mapped rather than raw.

    Create `scratch`, rename it to `moved`, create a new space called
    `scratch`, undo the rename: the recorded row carries the title `scratch`,
    which is now somebody else's. `undo` writes rows back past every guard, so
    this is checked there — otherwise it surfaces as `sqlite3.IntegrityError`,
    which `/api/undo` serves as a 500 for a conflict the caller caused and
    could fix. (The space is a created one rather than a seeded one because a
    seeded space's id *is* its name, and an id is taken for good either way.)
    """
    original = service.create_space("scratch", principal=owner())
    service.rename_space(original.id, "moved", principal=owner())
    replacement = service.create_space("scratch", principal=owner())
    rename_seq = max(
        event.seq
        for event in service.list_events(limit=50, principal=owner())
        if event.op == "node.update"
    )

    with pytest.raises(service.UndoNotPossible) as refused:
        service.undo(rename_seq, principal=owner())

    assert "cannot undo event" in str(refused.value)
    assert f"a space already answers to 'scratch' (id {replacement.id})" in str(refused.value)
    # Nothing moved: the undo failed before the UPDATE, not halfway through it.
    assert service.get_node(original.id, principal=owner()).title == "moved"
    assert service.resolve_space_id("scratch", principal=owner()) == replacement.id


def test_accepting_a_proposed_rename_onto_a_taken_name_is_refused_too(fresh_db):
    """The other write that lands a title late — checked, for the same reason.

    An agent proposes a rename while the name is free; a human takes the name
    before the review. The accept is the UPDATE that meets the unique index, so
    the reviewer gets the write path's sentence rather than an IntegrityError.
    """
    _three_spaces()
    gardener = agent("gardener", grants={"meta": "suggest", "main": "read"})
    proposal = service.update_node(
        service.resolve_space_id("b", principal=owner()), title="reading", principal=gardener
    )
    service.create_space("reading", principal=owner())

    with pytest.raises(service.SpaceNameTaken, match="a space already answers to 'reading'"):
        service.transition(str(proposal.id), "accept", principal=owner())

    assert service.get_node("b", principal=owner()).title == "b"


def test_two_names_differing_only_in_case_are_two_names(fresh_db):
    """The constraint is exactly as tight as the lookup, and no tighter.

    `title = ?` compares under SQLite's BINARY collation, so `Research` and
    `research` genuinely resolve to different rows. Refusing the pair would be
    refusing a name that works.
    """
    _three_spaces()

    upper = service.create_space("B", principal=owner())

    assert service.resolve_space_id("B", principal=owner()) == upper.id
    assert service.resolve_space_id("b", principal=owner()) == "b"


def test_the_name_check_tells_a_meta_writer_nothing_it_cannot_already_list(fresh_db):
    """The refusal names a space; the only principals that meet it already see it.

    `_require_space_name_free` is not scope-filtered, so in principle it could
    confirm a space exists to someone who cannot read it (Q13 review S3). It
    cannot in practice: creating a space means writing meta, every space node
    lives in meta, and reading meta lists all of them. The premise is asserted
    here rather than assumed, because a future grant shape could break it.

    It has to hold for **archived** spaces too now that a refusal can name one:
    `list_nodes` filters by state only when asked to, so the same meta read
    that shows the live spaces shows the retired ones.
    """
    _three_spaces()
    service.archive_space("c", principal=owner())
    gardener = agent("gardener", grants={"meta": "edit", "main": "read"})

    listed = {node.title for node in service.list_nodes(space="meta", principal=gardener)}
    assert {"main", "b", "c"} <= listed, "a meta reader already sees every space, archived too"

    # So neither refusal below reveals anything: both spaces were in the list it
    # just read, and the archived one is the case that could not be seen before.
    with pytest.raises(service.SpaceNameTaken, match="a space already answers to 'b'"):
        service.create_space("b", principal=gardener)
    with pytest.raises(service.SpaceNameTaken, match="archived space already answers to 'c'"):
        service.create_space("c", principal=gardener)
