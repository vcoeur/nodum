"""Consolidation cycles: the ambient stamp, the lifecycle, and the journal read.

Phase 5's foundation (design §8.4). A cycle groups a set of graph writes under
one ``cycle_id`` so that a human can take the whole of it back in one action;
what is tested here is everything *around* that column — the context variable
:func:`nodum.service._emit` reads, the four lifecycle functions, and who may
call them.
"""

from __future__ import annotations

import pytest
from helpers import agent, owner

from nodum import auth, db, service
from nodum.service import InvalidTransition, RecordNotFound, TypeNotFound
from nodum.store import GrantNotPermitted


def _events():
    return list(reversed(service.list_events(limit=1000, principal=owner())))  # chronological


def _open(**kwargs):
    kwargs.setdefault("trigger", "manual")
    kwargs.setdefault("principal", owner())
    return service.open_cycle(**kwargs)


# ── The ambient stamp ─────────────────────────────────────────────────────────


def test_an_ordinary_write_inside_a_cycle_is_stamped_without_naming_it(fresh_db):
    """The whole reason the stamp is ambient: no call site mentions a cycle.

    `create_node` and `create_edge` have no `cycle_id` parameter and never
    will — a parameter threaded through every public signature is the thing a
    consolidation path forgets on one branch, and a missed event is one rollback
    would leave behind.
    """
    cycle = _open()
    with service.in_cycle(cycle.id):
        source = service.create_node(type="claim", title="A", principal=owner())
        target = service.create_node(type="claim", title="B", principal=owner())
        service.create_edge(source.id, target.id, "supports", principal=owner())

    assert [event.op for event in _events()] == [
        "node.create",
        "node.create",
        "edge.create",
    ]
    assert {event.cycle_id for event in _events()} == {cycle.id}


def test_writes_outside_the_block_carry_no_cycle_id(fresh_db):
    cycle = _open()
    before = service.create_node(type="note", title="before", principal=owner())
    with service.in_cycle(cycle.id):
        inside = service.create_node(type="note", title="inside", principal=owner())
    after = service.create_node(type="note", title="after", principal=owner())

    stamped = {event.payload["after"]["id"]: event.cycle_id for event in _events()}
    assert stamped[before.id] is None
    assert stamped[inside.id] == cycle.id
    assert stamped[after.id] is None


def test_the_cycle_is_reset_when_the_block_raises(fresh_db):
    """A leaked cycle id would make every later human write un-undoable.

    `undo` refuses a cycle-stamped event by design, so an id that outlived its
    `with` block would quietly convert ordinary edits into writes whose only
    route back is the rollback of a cycle they were never part of.
    """
    cycle = _open()
    with pytest.raises(RuntimeError, match="the runner fell over"):
        with service.in_cycle(cycle.id):
            service.create_node(type="note", title="inside", principal=owner())
            raise RuntimeError("the runner fell over")

    after = service.create_node(type="note", title="after", principal=owner())
    stamped = {event.payload["after"]["id"]: event.cycle_id for event in _events()}
    assert stamped[after.id] is None
    # And the write that did land before the failure is still stamped: the
    # reset is about what comes next, not about undoing the block.
    assert cycle.id in set(stamped.values())


def test_nested_cycles_restore_the_outer_one(fresh_db):
    """The reset is token-based, so nesting is well defined rather than lossy."""
    outer, inner = _open(), _open()
    with service.in_cycle(outer.id):
        first = service.create_node(type="note", title="outer", principal=owner())
        with service.in_cycle(inner.id):
            nested = service.create_node(type="note", title="inner", principal=owner())
        last = service.create_node(type="note", title="outer again", principal=owner())

    stamped = {event.payload["after"]["id"]: event.cycle_id for event in _events()}
    assert stamped[first.id] == outer.id
    assert stamped[nested.id] == inner.id
    assert stamped[last.id] == outer.id


def test_an_explicit_cycle_id_wins_over_the_ambient_one(fresh_db):
    """`_emit` resolves the argument first, then the context variable."""
    conn = db.connect()
    try:
        with service.in_cycle("ambient"):
            ambient_seq = service._emit(conn, "human:owner", "node.create", {"before": None})
            explicit_seq = service._emit(
                conn, "human:owner", "node.create", {"before": None}, cycle_id="explicit"
            )
        conn.commit()
        stamps = dict(
            conn.execute(
                "SELECT seq, cycle_id FROM events WHERE seq IN (?, ?)",
                (ambient_seq, explicit_seq),
            ).fetchall()
        )
    finally:
        conn.close()
    assert stamps[ambient_seq] == "ambient"
    assert stamps[explicit_seq] == "explicit"


# ── Opening a cycle ───────────────────────────────────────────────────────────


def test_open_cycle_records_who_asked_and_starts_running(fresh_db):
    cycle = _open()
    assert cycle.status == "running"
    assert cycle.trigger == "manual"
    # Who *asked*, which is deliberately not the actor on the events inside.
    assert cycle.triggered_by == "human:owner"
    assert cycle.scope is None
    assert cycle.dry_run is False
    assert cycle.report is None
    assert cycle.finished_at is None
    assert cycle.rolled_back_by is None
    assert cycle.started_at


def test_a_scheduled_cycle_says_the_clock_asked(fresh_db):
    """Nobody asked for the nightly run, so no account may be named as having."""
    cycle = _open(trigger="scheduled")
    assert cycle.triggered_by == service.SCHEDULER_ACTOR


def test_a_dry_run_is_recorded_as_one(fresh_db):
    assert _open(dry_run=True).dry_run is True


def test_an_unknown_trigger_is_refused(fresh_db):
    with pytest.raises(ValueError, match="trigger must be one of"):
        _open(trigger="whenever")


def test_scope_resolves_by_name_or_id_like_every_other_space_reference(fresh_db):
    space = service.create_space("research", principal=owner())
    assert _open(scope="research").scope == space.id
    assert _open(scope=space.id).scope == space.id


def test_an_unknown_scope_and_an_unreadable_one_answer_identically(fresh_db):
    """A cycle is not an existence oracle either (Q13 review S3)."""
    service.create_space("research", principal=owner())
    writer = agent("bot", grants={"meta": "read", "main": "edit"})

    with pytest.raises(TypeNotFound) as absent:
        service.open_cycle(trigger="manual", scope="nowhere", principal=writer)
    with pytest.raises(TypeNotFound) as ungranted:
        service.open_cycle(trigger="manual", scope="research", principal=writer)
    assert str(absent.value).replace("nowhere", "X") == str(ungranted.value).replace(
        "research", "X"
    )


# ── Who may open and close one ────────────────────────────────────────────────


def test_a_cycle_is_curative_tier_so_suggest_is_not_enough(fresh_db):
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    with pytest.raises(GrantNotPermitted, match="open a consolidation cycle"):
        service.open_cycle(trigger="manual", scope="main", principal=proposer)


def test_the_gardener_may_open_a_cycle_on_the_spaces_it_holds_edit_on(fresh_db):
    """0014's grants are what make the seeded identity able to do anything."""
    gardener = auth.internal_principal()
    scoped = service.open_cycle(trigger="scheduled", scope="main", principal=gardener)
    assert scoped.scope == "main"
    # And an unscoped cycle, because it holds `edit` somewhere.
    assert service.open_cycle(trigger="scheduled", principal=gardener).scope is None


def test_an_agent_with_edit_nowhere_cannot_open_an_unscoped_cycle(fresh_db):
    """An unscoped cycle covers the whole file, which no grant confers."""
    reader = agent("reader", grants={"meta": "read"})
    with pytest.raises(GrantNotPermitted, match="open a consolidation cycle"):
        service.open_cycle(trigger="manual", principal=reader)


def test_a_read_only_grant_does_not_veto_an_unscoped_cycle_the_agent_may_run(fresh_db):
    """What the unscoped check asks is "does this principal hold `edit` anywhere?".

    The spaces it hands `require_review` are therefore the `edit` ones and not
    every granted space: an agent with `edit` on `main` and `read` on `meta` —
    the ordinary shape, since resolving a type needs `read` on meta — would
    otherwise be refused by its own read grant.
    """
    writer = agent("writer", grants={"meta": "read", "main": "edit"})
    assert service.open_cycle(trigger="manual", principal=writer).scope is None


def test_closing_is_gated_the_same_way_opening_is(fresh_db):
    cycle = _open(scope="main")
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    with pytest.raises(GrantNotPermitted, match="close a consolidation cycle"):
        service.close_cycle(cycle.id, status="completed", report={}, principal=proposer)


def test_a_space_archived_mid_cycle_does_not_make_its_cycle_uncloseable(fresh_db):
    """The check reads the recorded scope, not a fresh resolution of it.

    Resolving it again would refuse — `_resolve_space` matches active spaces
    only — and would leave a `running` row in the journal for good.
    """
    space = service.create_space("research", principal=owner())
    cycle = _open(scope=space.id)
    service.archive_space(space.id, principal=owner())
    closed = service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    assert closed.status == "completed"


# ── Closing a cycle ───────────────────────────────────────────────────────────


def test_close_cycle_records_the_outcome_and_the_report(fresh_db):
    cycle = _open()
    closed = service.close_cycle(
        cycle.id,
        status="completed",
        report={"merged": 2, "detail": {"skipped": ["a"]}},
        principal=owner(),
    )
    assert closed.status == "completed"
    assert closed.report == {"merged": 2, "detail": {"skipped": ["a"]}}
    assert closed.finished_at is not None
    assert service.get_cycle(cycle.id, principal=owner()) == closed


def test_a_failed_cycle_stays_in_the_journal(fresh_db):
    """A cycle that vanished on failure is a cycle nobody can ask about."""
    cycle = _open()
    closed = service.close_cycle(
        cycle.id, status="failed", report={"error": "boom"}, principal=owner()
    )
    assert closed.status == "failed"
    assert [entry.id for entry in service.list_cycles(principal=owner())] == [cycle.id]


def test_a_cycle_leaves_running_exactly_once(fresh_db):
    cycle = _open()
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    with pytest.raises(InvalidTransition, match="already completed"):
        service.close_cycle(cycle.id, status="failed", report={}, principal=owner())


def test_a_status_outside_the_closed_set_is_refused(fresh_db):
    cycle = _open()
    for status in ("running", "sideways"):
        with pytest.raises(ValueError, match="status must be one of"):
            service.close_cycle(cycle.id, status=status, report={}, principal=owner())


def test_closing_an_unknown_cycle_is_a_not_found(fresh_db):
    with pytest.raises(RecordNotFound, match="no cycles row"):
        service.close_cycle("nope", status="completed", report={}, principal=owner())


# ── Reading the journal ───────────────────────────────────────────────────────


def test_get_and_list_cycles_are_human_only(fresh_db):
    """The journal spans every space in the file, like `list_events`."""
    cycle = _open()
    # The gardener itself included: `edit` is authority to *run* a cycle, never
    # authority to read the governance record of every cycle in the file.
    for principal in (agent("bot", grants={"main": "edit"}), auth.internal_principal()):
        with pytest.raises(GrantNotPermitted, match="consolidation journal"):
            service.get_cycle(cycle.id, principal=principal)
        with pytest.raises(GrantNotPermitted, match="consolidation journal"):
            service.list_cycles(principal=principal)


def test_get_cycle_on_an_unknown_id_is_a_not_found(fresh_db):
    with pytest.raises(RecordNotFound, match="no cycles row"):
        service.get_cycle("nope", principal=owner())


def test_list_cycles_is_newest_first_and_capped(fresh_db):
    first, second, third = _open(), _open(), _open()
    assert [entry.id for entry in service.list_cycles(principal=owner())] == [
        third.id,
        second.id,
        first.id,
    ]
    assert [entry.id for entry in service.list_cycles(limit=2, principal=owner())] == [
        third.id,
        second.id,
    ]


def test_the_journals_diff_is_the_event_log_and_not_a_second_record(fresh_db):
    """What a cycle changed is read back out of the append-only log itself."""
    cycle = _open()
    outside = service.create_node(type="note", title="outside", principal=owner())
    with service.in_cycle(cycle.id):
        inside = service.create_node(type="note", title="inside", principal=owner())
    service.close_cycle(cycle.id, status="completed", report={"nodes": 1}, principal=owner())

    diff = service.list_events(owner(), cycle_id=cycle.id)
    assert [event.payload["after"]["id"] for event in diff] == [inside.id]
    assert outside.id not in {event.payload["after"]["id"] for event in diff}
    # The report says what the runner claims; it is not where the diff lives.
    assert service.get_cycle(cycle.id, principal=owner()).report == {"nodes": 1}
