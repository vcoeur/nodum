"""Cross-space isolation: the cases a single-space suite cannot see.

Every other test file grants its agents both seeded spaces, so the scope
filters never actually filter (Q13 adversarial review, suite blind spot).
Here an agent always lacks a grant on some space that exists — which is what
turns the scoped store's filters, the mention lifecycle's authority checks
and the "unreadable does not exist" rule into something a test can fail.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from helpers import agent, owner, seed_space

from nodum import auth, db, search, service
from nodum.store import GrantNotPermitted, Store


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


def test_the_listings_space_clause_is_anded_onto_the_scope_and_never_replaces_it(fresh_db):
    """The same invariant on the other builder — `service.list_nodes`.

    The sibling above pinned `search`, and `service` was left to behavioural
    coverage alone. It was not enough: making the space filter *replace* the
    grant scope (`if scope:` → `if scope and space_id is None:`) left the
    entire suite green, because resolution refuses to hand an agent the id of a
    space outside its read set and no runtime call can reach the widening. This
    is the assertion that would have failed.
    """
    _three_spaces()
    scout = _scout()
    conn = db.connect()
    try:
        clauses, params = service._node_list_filters(
            Store(conn, scout),
            state=None,
            type_id=None,
            parent_id=None,
            space_id="b",
            include_meta=False,
        )
    finally:
        conn.close()

    scope = [clause for clause in clauses if "IN (" in clause]
    assert scope, f"the principal's scope clause must survive the filter: {clauses}"
    assert "space_id = ?" in clauses
    assert "b" in params and set(scout.read_spaces) <= set(params)


def test_every_search_hit_names_the_space_it_lives_in(fresh_db):
    """An unnarrowed result list spans spaces, so each hit has to say which.

    The human UI scans this list; without the field a ``main`` hit and a
    ``research`` hit are indistinguishable until one is opened. Both hit
    shapes are covered — a fused (ranked) hit and a graph-expansion one, which
    are built in two different places.
    """
    nodes = _three_spaces()
    service.create_edge(nodes["main"].id, nodes["c"].id, "relates_to", principal=owner())

    result = search.search("territory", principal=owner())

    assert {hit.node_id: hit.space_id for hit in result.hits} == {
        node.id: node.space_id for node in nodes.values()
    }
    # And the three are genuinely different spaces, so the mapping is not vacuous.
    assert len({node.space_id for node in nodes.values()}) == 3
    # The expansion hit is constructed separately and must say it too.
    expanded = search.search("main thing", expand=True, principal=owner())
    neighbour = next(hit for hit in expanded.hits if hit.node_id == nodes["c"].id)
    assert neighbour.signals.keys() == {"graph"}
    assert neighbour.space_id == nodes["c"].space_id


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


# ── M3: a space node is visible iff the principal holds a grant on it ─────────


def test_an_agent_without_a_grant_on_a_space_cannot_tell_it_exists(fresh_db):
    """The fifth existence oracle: space nodes leaked through the meta read.

    Space nodes live in the meta space, and every agent is seeded `meta: read`
    for the type vocabulary — so a filter on `space_id` alone handed every
    space in the file to any agent, while `resolve_space_id` still refused the
    same space. A space node now resolves through its own id: an agent holding
    no grant on `research` must answer on every node read exactly as if the
    space did not exist, and its probe of the space's *name* stays a not-found.
    """
    # Created through the service, so the space node is event-logged and the
    # search index actually holds its title — the filter is what is tested.
    service.create_space("research", principal=owner())
    default = agent("default", grants={"meta": "read"})
    research_node_id = service.resolve_space_id("research", principal=owner())

    # Search: the space node's title is indexed, but the row is outside the
    # read set, so the hit is simply not there.
    assert search.search("research", principal=default).hits == []

    # Listing: the only space node in the default agent's world is meta's own
    # (it holds `meta: read`); `research` and `main` are not listed.
    space_nodes = {
        node.title
        for node in service.list_nodes(principal=default, limit=500)
        if node.type == "space"
    }
    assert space_nodes == {"meta"}

    # Children: the space node is not found, never denied — not-found
    # semantics, like every other read.
    with pytest.raises(service.NodeNotFound, match="node not found"):
        service.list_children(research_node_id, principal=default)

    # And the name does not resolve either — the same answer as a space that
    # does not exist at all.
    with pytest.raises(service.TypeNotFound, match="unknown space: research"):
        service.resolve_space_id("research", principal=default)

    # The human control: the space genuinely exists, and a human still sees it.
    assert research_node_id in {
        node.id for node in service.list_nodes(space="meta", principal=owner())
    }


def test_an_agent_granted_on_a_space_sees_its_space_node(fresh_db):
    """The positive control: the grant is the proof of acquaintance.

    An agent holding `research: read` can see the research space node through
    the same reads — it needs to: the space is part of its world, and the node
    is how it appears in listings and search results.
    """
    # Created through the service, so the space node is event-logged and the
    # search index actually holds its title — the filter is what is tested.
    service.create_space("research", principal=owner())
    research_node_id = service.resolve_space_id("research", principal=owner())
    research_note = service.create_node(
        type="note", title="private note", space="research", principal=owner()
    )
    # The grant is on the space's node id (create_space generates one).
    insider = agent("insider", grants={"meta": "read", research_node_id: "read"})

    listed = {node.id for node in service.list_nodes(principal=insider, limit=500)}
    assert research_node_id in listed
    assert research_note.id in listed
    assert "main" not in {node.id for node in service.list_nodes(principal=insider, limit=500)}

    (space_hit,) = search.search("research", principal=insider).hits
    assert space_hit.node_id == research_node_id
    assert space_hit.type == "space"

    # The meta read is intact: the type vocabulary still lists, so types still
    # resolve, and the note-typed node in the granted space still lists.
    type_nodes = {
        node.id
        for node in service.list_nodes(principal=insider, limit=500)
        if node.space_id == "meta" and node.type == "type"
    }
    assert "note" in type_nodes
    assert service.list_nodes(space="research", principal=insider, limit=500) == [research_note]


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

    The route back exists but is not the state machine's: `TRANSITIONS` has no
    `active ← archived` entry, and `undo` restores the `before` row with a raw
    UPDATE regardless. That is exactly why the guard sits in `_transition_row`
    rather than resting on irreversibility — and it is `undo` of a *human's*
    mistake, human-only, not something any surface offers as an un-archive.
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


# ── Archiving a space is how a human cuts an agent off it ────────────────────


def test_archiving_a_space_makes_every_grant_on_it_inert(fresh_db):
    """The promise `archive_space`, the CLI and the archive dialog all make.

    It used to be true only of calls that *spelled the space's name*: those
    stopped resolving, so a write naming the space was refused. Everything
    reachable by node id kept working — the agent still read the space's nodes
    and `update_node` still returned `state='active'`, i.e. full live write
    authority — while `list_spaces` stopped showing the space or its grants. A
    human archives a space precisely to stop an agent, so the gap ran the exact
    opposite of the promise, silently.
    """
    _three_spaces()
    bot = agent("bot", grants={"meta": "read", "b": "edit"})
    node = service.create_node(type="concept", title="B thing", space="b", principal=bot)
    # The premise: before the archive this is real, live authority.
    assert service.get_node(node.id, principal=bot).title == "B thing"
    assert service.update_node(node.id, title="edited", principal=bot).state == "active"

    service.archive_space("b", principal=owner())
    bot = auth.agent_principal("bot")

    assert "b" not in bot.grants, "an archived space confers nothing"
    assert "b" not in (bot.read_spaces or ())
    # Reachable-by-id is the half that used to survive: it is default-deny now.
    with pytest.raises(service.NodeNotFound):
        service.get_node(node.id, principal=bot)
    with pytest.raises(service.NodeNotFound):
        service.update_node(node.id, title="edited again", principal=bot)
    assert node.id not in {row.id for row in service.list_nodes(principal=bot, limit=500)}
    # The human still sees everything: nothing was moved or deleted.
    assert service.get_node(node.id, principal=owner()).title == "edited"


def test_an_archived_spaces_grant_survives_so_it_can_be_seen_and_revoked(fresh_db):
    """Inert, not destroyed — and revocable, which is the part that was missing.

    `grant`/`revoke` resolved through `_resolve_space`, which matches active
    spaces only, so archiving a space left its grants with **no supported route
    to removal at all**: `revoke` answered `unknown space` by id and by name
    alike, and raw SQL or undoing the archive were the only ways out. An
    authority that cannot be taken away is a bug whichever way inertness goes.
    """
    _three_spaces()
    agent("bot", grants={"meta": "read", "b": "edit"})
    service.archive_space("b", principal=owner())

    # The row survives, so the human can still see what is delegated.
    held = {(g.space_id, g.level) for g in service.list_grants("bot", principal=owner())}
    assert ("b", "edit") in held
    # Granting more is refused, and says why rather than "unknown space".
    with pytest.raises(ValueError, match="cannot grant on the archived space 'b'"):
        service.grant("bot", "b", "read", principal=owner())

    service.revoke("bot", "b", principal=owner())

    assert "b" not in {g.space_id for g in service.list_grants("bot", principal=owner())}
    # And by id as well as by name — both spellings reach an archived space.
    agent("bot2", grants={"b": "edit"})
    service.revoke("bot2", "b", principal=owner())
    assert service.list_grants("bot2", principal=owner()) == []


def test_undoing_an_archive_brings_the_delegation_back_with_the_space(fresh_db):
    """Why the grant rows are kept rather than deleted on archive.

    `undo` of a `node.archive` is the route back, and it must restore the state
    that was there — including who could write the space. Deleting the grants
    would make archiving a one-way door that silently ate delegation.
    """
    _three_spaces()
    bot = agent("bot", grants={"meta": "read", "b": "edit"})
    node = service.create_node(type="concept", title="B thing", space="b", principal=bot)
    service.archive_space("b", principal=owner())
    archive_seq = next(
        event.seq
        for event in service.list_events(limit=50, principal=owner())
        if event.op == "node.archive"
    )

    service.undo(archive_seq, principal=owner())
    bot = auth.agent_principal("bot")

    assert bot.grants["b"] == "edit"
    assert service.get_node(node.id, principal=bot).title == "B thing"


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
    # `conventions` (migration 0016) is a real space, listed like any other.
    assert [space.id for space in service.list_spaces(principal=owner())] == [
        "meta",
        "main",
        "conventions",
        "c",
    ]


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
    The proposer is granted on every space: the name check spans the whole
    file and the refusal names the holder, so only a principal that can list
    every space may run it (M3).
    """
    _three_spaces()
    proposer = agent(
        "proposer",
        grants={
            "meta": "suggest",
            "main": "read",
            "b": "read",
            "c": "read",
            "conventions": "read",
        },
    )
    proposal = service.update_node(
        service.resolve_space_id("b", principal=owner()), title="reading", principal=proposer
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
    """The refusal names a space; the only principals that meet it see them all.

    `_require_space_name_free` is not scope-filtered, so in principle it could
    confirm a space exists to someone who cannot read it (Q13 review S3). It
    used to be safe by inheritance: every space node lives in meta, and a meta
    reader could list all of them. A space node now resolves through its own
    id (M3), so a meta reader with a partial grant set sees only the spaces it
    holds grants on — and the check enforces the premise itself: naming a
    space takes a grant on every space in the file. The refusal is on the
    grant, so a taken name, an archived one, and a free one all read
    identically, and the refusal names no space at all.

    The premise has to hold for **archived** spaces too now that a refusal can
    name one: `list_nodes` filters by state only when asked to, so the same
    listing that shows the live spaces shows the retired ones.
    """
    _three_spaces()
    service.archive_space("c", principal=owner())
    gardener = agent("gardener", grants={"meta": "edit", "main": "read"})

    # The premise, asserted rather than assumed: the gardener lists exactly the
    # space nodes it holds grants on — `meta` and `main`, live or retired — and
    # none of the others.
    listed = {node.title for node in service.list_nodes(space="meta", principal=gardener)}
    assert {"meta", "main"} <= listed, "the granted spaces' nodes still list"
    assert not {"b", "c", "conventions"} & listed, "a space without a grant does not list"

    # So the name check refuses outright rather than answer: the taken name,
    # the archived one, and a free one are word-for-word the same refusal, and
    # it names no space at all — none of the probes can tell the gardener
    # anything it could not list for itself.
    with pytest.raises(GrantNotPermitted) as taken:
        service.create_space("b", principal=gardener)
    with pytest.raises(GrantNotPermitted) as archived:
        service.create_space("c", principal=gardener)
    with pytest.raises(GrantNotPermitted) as free:
        service.create_space("definitely-free", principal=gardener)
    assert str(taken.value) == str(archived.value) == str(free.value)
    for name in ("b", "c", "definitely-free"):
        # Whole-word match: "c" is a substring of "space", and the refusal
        # necessarily says *space* — the probe names must not appear as words.
        assert re.search(rf"\b{name}\b", str(taken.value)) is None


def test_a_space_cannot_be_created_outside_meta(fresh_db):
    """The precondition that turned the name refusal into an existence oracle.

    `POST /api/nodes {"type": "space", "space": "main"}` answered 200, and
    `space` sits in the editor's type picker, so a human could put a space
    inside ordinary territory by accident. `GET /api/spaces` then listed it and
    `_resolve_space` resolved it as real territory, while the grants governing
    it were the *host* space's — and the rename path's grant check is on the
    host space, which is what let a principal holding nothing but `main` reach
    the unscoped name check. A space belongs in meta, which is what every
    adapter is already written against.
    """
    _three_spaces()

    with pytest.raises(ValueError, match="a space must live in the 'meta' space, not 'main'"):
        service.create_node(type="space", title="scratch", space="main", principal=owner())
    with pytest.raises(ValueError, match="a space must live in the 'meta' space, not 'b'"):
        service.create_node(type="space", title="scratch", space="b", principal=owner())

    # Nothing was created, and aiming at meta still works — this is a rule about
    # where a space lands, not a ban on the generic path.
    # (`conventions` from migration 0016 lists between the seeded and the made.)
    listed = [space.id for space in service.list_spaces(principal=owner())]
    assert listed == ["meta", "main", "conventions", "b", "c"]
    landed = service.create_node(type="space", title="scratch", space="meta", principal=owner())
    assert landed.space_id == "meta"
    assert service.resolve_space_id("scratch", principal=owner()) == landed.id


def test_naming_a_space_tells_a_principal_that_cannot_read_meta_nothing(fresh_db):
    """The oracle itself, on the `update_node` path the create-path premise missed.

    `_require_space_name_free` searches every space in the file and names the
    holder — including an archived one, which no listing shows. That is safe
    only for a principal that can already list every space, which the check
    now enforces itself. `create_node` gets there by construction (resolving
    the `space` type needs READ on meta), but a rename is gated on `suggest`
    on the space the node *lives in*, so a `space`-typed node sitting in
    `main` let an agent holding nothing but `main` read a confirm/deny — plus
    the id — for a space it cannot list. The rename of such a row is now
    refused as *not found* before the name check can run: a space node
    resolves through its own id (M3), and no principal holds a grant on the
    decoy.

    Both halves are asserted, because a refusal is only not an oracle if the
    taken name and a free one are **indistinguishable**: it was the free name
    being accepted that made the refusal a clean signal.
    """
    _three_spaces()
    secret = service.create_space("classified", principal=owner())
    service.archive_space(secret.id, principal=owner())
    # A legacy row: the service refuses to create this now, but a database
    # written before that guard — or by raw SQL — can still hold one.
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO nodes (id, space_id, type_id, title, props, state, created_by)"
            " VALUES ('decoy', 'main', 'space', 'scratch', '{}', 'active', 'human:owner')"
        )
        conn.commit()
    finally:
        conn.close()
    outsider = agent("outsider", grants={"main": "suggest"})

    # The premise: this principal genuinely cannot see the space it is probing.
    with pytest.raises(GrantNotPermitted):
        service.list_spaces(principal=outsider)
    with pytest.raises(service.NodeNotFound):
        service.get_node(secret.id, principal=outsider)

    # The decoy is a space node, so it resolves through its own id — which the
    # outsider holds no grant on. The rename is refused as not found, for a
    # taken name and a free one alike, and the refusal names nothing.
    with pytest.raises(service.NodeNotFound) as taken:
        service.update_node("decoy", title="classified", principal=outsider)
    with pytest.raises(service.NodeNotFound) as free:
        service.update_node("decoy", title="definitely-free", principal=outsider)

    # Word for word the same refusal, and neither names a space or an id.
    assert str(taken.value) == str(free.value)
    assert secret.id not in str(taken.value)
    assert "classified" not in str(taken.value)
    # And the useful refusal survives for the principal it was written for — a
    # human, the one principal that can already list every space.
    with pytest.raises(service.SpaceNameTaken) as refused:
        service.update_node("decoy", title="classified", principal=owner())
    assert "archived space already answers to 'classified'" in str(refused.value)
    assert secret.id in str(refused.value)
