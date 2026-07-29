"""Consolidation cycles: the ambient stamp, the lifecycle, and the journal read.

Phase 5's foundation (design §8.4). A cycle groups a set of graph writes under
one ``cycle_id`` so that a human can take the whole of it back in one action;
what is tested here is everything *around* that column — the context variable
:func:`nodum.service._emit` reads, the four lifecycle functions, and who may
call them.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest
from helpers import agent, owner

from nodum import agent as agent_runtime  # `agent` is the helper that seeds one
from nodum import auth, db, service
from nodum.migrations import GARDENER_AGENT_ID
from nodum.service import InvalidTransition, RecordNotFound, TypeNotFound
from nodum.store import GrantNotPermitted


def _events():
    return list(reversed(service.list_events(limit=1000, principal=owner())))  # chronological


def _open(**kwargs):
    kwargs.setdefault("trigger", "manual")
    kwargs.setdefault("principal", owner())
    return service.open_cycle(**kwargs)


def _closed(**kwargs):
    """Open a cycle and close it, for a test that needs several journal entries.

    Only one *consolidation* cycle may be running against a file at a time
    (0014's partial unique index), so a test wanting three of them is a test
    about three finished runs.
    """
    cycle = _open(**kwargs)
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    return cycle


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
    """The reset is token-based, so nesting is well defined rather than lossy.

    A consolidation sweep with a curative operation inside it is the shape that
    actually nests, and it is also the pair the file admits at once: two running
    consolidation cycles are refused.
    """
    outer, inner = _open(trigger="scheduled"), _open(trigger="curative")
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
    assert _closed(scope="research").scope == space.id
    assert _closed(scope=space.id).scope == space.id


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


def test_a_second_consolidation_cycle_is_refused_wherever_it_is_asked_for(fresh_db):
    """One `running` consolidation row in the file, whichever process opened it.

    The refusal happens on the **insert**, so it does not matter that the first
    cycle was opened by another process, another thread, or an hour ago: two
    runs cannot both believe they won a read.
    """
    running = _open(trigger="scheduled")

    for trigger in ("manual", "scheduled"):
        with pytest.raises(service.CycleInProgress, match="already running"):
            _open(trigger=trigger)

    service.close_cycle(running.id, status="completed", report={}, principal=owner())
    assert _open(trigger="manual").status == "running"


def test_the_refusal_names_the_cycle_in_the_way_and_the_door_out_of_it(fresh_db):
    """A stuck `running` row blocks every later run, so the way out has to be in it.

    `cycle-abandon` is that door — a cycle killed by a `SIGKILL` or a power cut
    never closes itself, and nothing else moves the row. A refusal that says
    "try again when it has finished" about a run that will never finish is
    advice nobody can carry out, which is the shape this project has already
    fixed once on `rollback`.
    """
    stuck = _open(trigger="scheduled")

    with pytest.raises(service.CycleInProgress) as refused:
        _open(trigger="manual")

    message = str(refused.value)
    assert stuck.id in message
    assert f"nodum cycle-abandon {stuck.id}" in message


def test_an_integrity_error_that_is_not_the_guard_is_not_reported_as_a_busy_graph(
    fresh_db, monkeypatch
):
    """`CycleInProgress` is a claim about the file, so it needs a running cycle behind it.

    The `except sqlite3.IntegrityError` around the INSERT is there for exactly
    one constraint — `0014`'s partial unique index. Every other constraint on
    `cycles` is a bug, and translating one into "a consolidation cycle is
    already running" would tell a caller to wait for a run that does not exist,
    and put `nodum cycle-abandon <id>` in front of an id there is none of.

    Forced here through the primary key, with the id generator pinned so two
    opens collide. Nothing in the file is `running` when it happens.
    """
    first = _open(trigger="curative")
    service.close_cycle(first.id, status="completed", report={}, principal=owner())

    class _SameIdEveryTime:
        @staticmethod
        def uuid4():
            return uuid.UUID(hex=first.id)

    monkeypatch.setattr(service, "uuid", _SameIdEveryTime)
    # Matched, so the fixture is known to have provoked the constraint it meant
    # to rather than any `IntegrityError` that happens to reach the handler.
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE constraint failed: cycles\.id"):
        _open(trigger="curative")

    # No cycle was left behind, and none is running to have blocked anything.
    assert [cycle.status for cycle in service.list_cycles(principal=owner())] == ["completed"]


def test_a_curative_or_rollback_cycle_is_not_blocked_by_a_running_consolidation(fresh_db):
    """Blocking these would take the curative tier offline for the nightly sweep.

    A curative operation is one human-driven act and a rollback is the human's
    undo; both are short, and both open a cycle of their own. Only the two
    triggers a consolidation run opens are serialised against each other.
    """
    _open(trigger="scheduled")

    node = service.create_node(type="claim", title="Alpha", principal=owner())
    retype = service.retype([node.id], "concept", principal=owner())

    assert service.get_cycle(retype.cycle_id, principal=owner()).trigger == "curative"
    rollback = service.rollback_cycle(retype.cycle_id, principal=owner())
    assert service.get_cycle(rollback.rollback_cycle_id, principal=owner()).trigger == "rollback"


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
    service.close_cycle(scoped.id, status="completed", report={}, principal=owner())
    # And an unscoped cycle, because it holds `edit` somewhere.
    assert service.open_cycle(trigger="scheduled", principal=gardener).scope is None


def test_the_gardener_can_review_a_proposed_version_today(fresh_db):
    """The authority is live in the shipped release; only the caller is missing.

    `_transition_row` gates a version through `Store.require_review`, which
    passes for a human *or* an agent holding `edit` on the node's space — and
    `0014` seeds the gardener with `edit` on `main`. So "nothing reaches the
    version-review hole today" is true of the shipped *callers* and false of the
    shipped *authority*: writing the call is all it takes, which is why the
    reversal of a review had to be closed before anything is written that makes
    it. `suggest` is here as the control — without it this test would pass on a
    check that let everyone through.
    """
    gardener = auth.internal_principal()
    assert not gardener.is_human
    assert gardener.grants["main"] == "edit"

    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})

    def staged(title):
        node = service.create_node(type="claim", title=title, content="first", principal=owner())
        service.update_node(node.id, content="a second thought", principal=proposer)
        version = next(
            entry
            for entry in service.history(node.id, principal=owner())
            if entry.state == "proposed"
        )
        return node, version

    accepted_node, to_accept = staged("Alpha")
    _, to_reject = staged("Beta")
    with pytest.raises(GrantNotPermitted):
        service.transition(str(to_accept.id), "accept", principal=proposer)

    cycle = _open()
    with service.in_cycle(cycle.id):
        assert (
            service.transition(str(to_accept.id), "accept", principal=gardener).state == "applied"
        )
        assert (
            service.transition(str(to_reject.id), "reject", principal=gardener).state == "archived"
        )
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())

    assert service.get_node(accepted_node.id, principal=owner()).content == "a second thought"
    reviews = [event for event in _events() if event.cycle_id == cycle.id]
    assert {event.actor for event in reviews} == {f"agent:{GARDENER_AGENT_ID}"}
    assert {event.op for event in reviews} == {"node.update", "version.reject"}


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

    The trigger is `curative` because that is the one an agent legitimately
    opens for itself (`_curative_cycle`); `manual` means a *human* asked, and is
    refused below.
    """
    writer = agent("writer", grants={"meta": "read", "main": "edit"})
    assert service.open_cycle(trigger="curative", principal=writer).scope is None


def test_only_a_human_may_open_a_manual_cycle(fresh_db):
    """`triggered_by` answers "who asked", and it was a convention, not a rule.

    The schema's own comment says the column holds `'human:<id>'` or
    `'scheduler'`, but `open_cycle` wrote `principal.actor_string` unchecked —
    and `consolidate.consolidate` takes `triggered_by` as a **string** and
    re-mints a principal from it, so no `principal=` binding existed anywhere
    downstream for a guard to check. A caller reaching that function could put
    `agent:builtin-gardener` in the one column that answers "I did not ask for
    this". The curative and scheduled triggers are untouched: one records the
    operation's own principal by design, the other records the clock.
    """
    gardener = auth.internal_principal()
    writer = agent("writer", grants={"meta": "read", "main": "edit"})

    for principal in (gardener, writer):
        with pytest.raises(GrantNotPermitted, match="may not open a 'manual'"):
            service.open_cycle(trigger="manual", principal=principal)
    assert service.list_cycles(principal=owner()) == []

    # Neither trigger an agent may legitimately use is affected, and neither
    # can name an agent as the asker.
    assert _open(trigger="scheduled", principal=gardener).triggered_by == service.SCHEDULER_ACTOR
    assert _open(trigger="curative", principal=writer).triggered_by == "agent:writer"


def test_the_gardener_cannot_forge_who_asked_through_the_runners_string(fresh_db):
    """The demonstrated route, closed where the value is written.

    `consolidate(triggered_by=…)` resolves the string to a principal and hands
    it to `open_cycle`, so the check has to live at the write and not at that
    call site — every future caller passes through this one.
    """
    forged = auth.principal_from_actor(f"agent:{GARDENER_AGENT_ID}")
    with pytest.raises(GrantNotPermitted, match="may not open a 'manual'"):
        service.open_cycle(trigger="manual", principal=forged)
    assert [entry.triggered_by for entry in service.list_cycles(principal=owner())] == []


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
    with pytest.raises(RecordNotFound, match="consolidation cycle not found"):
        service.close_cycle("nope", status="completed", report={}, principal=owner())


def test_a_missing_cycle_is_named_without_naming_the_table_it_lives_in(fresh_db):
    """`no cycles row with id` is schema vocabulary reaching a human at a prompt.

    Every other lookup in the service says `<thing> not found: <id>`, and the
    person who typed `nodum cycle-get <id>` has no reason to know the table is
    called `cycles`.
    """
    for call in (
        lambda: service.get_cycle("nope", principal=owner()),
        lambda: service.rollback_cycle("nope", principal=owner()),
        lambda: service.abandon_cycle("nope", principal=owner()),
        lambda: service.close_cycle("nope", status="failed", report={}, principal=owner()),
    ):
        with pytest.raises(RecordNotFound) as missing:
            call()
        assert str(missing.value) == "consolidation cycle not found: nope"
        assert "cycles row" not in str(missing.value)


# ── Abandoning an interrupted cycle ───────────────────────────────────────────


def test_an_interrupted_cycle_can_be_abandoned_and_only_then_rolled_back(fresh_db):
    """The door out of a `running` row, which had none on any surface.

    A cycle that never closed is not cosmetic: rollback refuses a `running`
    cycle because its event set is not closed, and `undo` refuses every event it
    stamped — so a run killed by `SIGKILL`, a power cut, or the scheduler
    cancelling a mid-cycle task on shutdown left its writes irreversible
    *everywhere*, behind advice ("close it first") that nothing could carry out.
    """
    cycle = _open()
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())
    with pytest.raises(InvalidTransition, match="still running"):
        service.rollback_cycle(cycle.id, principal=owner())

    abandoned = service.abandon_cycle(cycle.id, principal=owner())

    assert abandoned.status == "failed"
    assert abandoned.finished_at is not None
    # The journal says what happened and who declared it dead.
    assert abandoned.report["abandoned"] is True
    assert abandoned.report["abandoned_by"] == "human:owner"
    assert "interrupted" in abandoned.report["detail"]
    # And the writes it made are reachable again — which is the whole point.
    service.rollback_cycle(cycle.id, principal=owner())
    assert service.get_cycle(cycle.id, principal=owner()).status == "rolled_back"
    from nodum.service import NodeNotFound

    with pytest.raises(NodeNotFound):
        service.get_node(node.id, principal=owner())


def test_the_running_refusal_names_the_command_that_closes_it(fresh_db):
    """ "Close it first" is the advice; `cycle-abandon` is the only thing that does.

    A crashed run does not close itself — that is what made it crashed — so
    `rollback` refusing with "close it first, a crashed run is closed 'failed'"
    described a state nothing on any surface could reach. This system prints the
    exact command everywhere else; here it printed a wish.
    """
    cycle = _open()
    with service.in_cycle(cycle.id):
        service.create_node(type="claim", title="Half-written", principal=owner())

    with pytest.raises(InvalidTransition) as refused:
        service.rollback_cycle(cycle.id, principal=owner())

    assert f"nodum cycle-abandon {cycle.id}" in str(refused.value)


def test_an_abandoned_cycle_does_not_read_as_a_failed_curative_operation(fresh_db):
    """The abandon succeeded; the *run* failed, and the report has to say which.

    The report was written in the shape a one-op curative cycle uses — an `op`
    naming the operation and an `error` explaining it — so a nightly sweep
    abandoned after a power cut was rendered "One curative operation:
    abandon_cycle. It failed." Three things wrong in one line: the cycle was a
    consolidation and not a curative op, `abandon_cycle` is not an operation the
    cycle ran, and the abandon did not fail. The cycle's own `trigger` says what
    it was; the report says only what is known about how it ended.

    `abandoned` is the discriminator a reader branches on, and it is the **only**
    one: there is no `op` here, because an abandon is not an operation the cycle
    ran and naming one is that same misreading in field form. It carried
    `op: "abandon_cycle"` for exactly one round — the journal view's reader
    returned nothing for a report without that key, so a value on the server was
    being kept alive by a client, which now keys on `abandoned`.
    """
    cycle = _open(trigger="scheduled")
    with service.in_cycle(cycle.id):
        service.create_node(type="claim", title="Half-written", principal=owner())

    report = service.abandon_cycle(cycle.id, principal=owner()).report

    assert report["abandoned"] is True, "an abandon must be tellable without matching a name"
    assert "op" not in report, "an abandon is not an operation the cycle ran"
    assert "error" not in report, "the abandon succeeded — it is the run that failed"
    assert "failed" not in str(report), "nothing in the report may say the abandon failed"
    assert report["abandoned_by"] == "human:owner"
    # And the entry still says what the cycle was, which the report never claims.
    assert service.get_cycle(cycle.id, principal=owner()).trigger == "scheduled"


def test_abandoning_a_cycle_that_already_said_how_it_ended_is_refused(fresh_db):
    """Not a general "close this" verb: re-closing would overwrite the record."""
    cycle = _open()
    service.close_cycle(cycle.id, status="completed", report={"jobs": 4}, principal=owner())

    with pytest.raises(InvalidTransition, match="already completed, not running"):
        service.abandon_cycle(cycle.id, principal=owner())

    assert service.get_cycle(cycle.id, principal=owner()).report == {"jobs": 4}


def test_abandon_is_human_only(fresh_db):
    """It makes a whole cycle's writes reversible, which `rollback` does not delegate."""
    cycle = _open()
    for principal in (agent("bot", grants={"main": "edit"}), auth.internal_principal()):
        with pytest.raises(GrantNotPermitted, match="abandon a consolidation cycle"):
            service.abandon_cycle(cycle.id, principal=principal)
    assert service.get_cycle(cycle.id, principal=owner()).status == "running"


# ── The kill switch (design K1–K3) ────────────────────────────────────────────


def _running(gardener):
    """One `running` consolidation cycle, opened by the principal that runs it."""
    return _open(trigger="scheduled", principal=gardener)


def test_a_stop_is_recorded_on_the_row_and_the_run_reads_it_back(fresh_db):
    """The switch is a row rather than a process signal, for `0014`'s reason.

    `nodum cycle-stop` typed at a terminal has to stop a cycle running inside
    `nodum serve`, and those are two interpreters — the same argument that made
    the one-running-consolidation guard a database index rather than a
    module-level lock. So the write and the read are two service calls over one
    row, and neither shares memory with the other.
    """
    gardener = auth.internal_principal()
    cycle = _running(gardener)
    assert service.stop_requested(cycle.id, principal=gardener) is False

    stopped = service.request_stop(cycle.id, principal=owner())
    assert stopped.stop_requested is True
    assert stopped.stop_requested_by == "human:owner"
    assert stopped.stop_requested_at is not None
    assert service.stop_requested(cycle.id, principal=gardener) is True
    # And the journal read carries it, so both human surfaces can render it.
    assert service.get_cycle(cycle.id, principal=owner()).stop_requested is True


def test_a_stop_neither_closes_the_cycle_nor_touches_what_it_wrote(fresh_db):
    """K3: it does not roll back, and it does not close the row from outside.

    Stopping and undoing are two decisions — a kill switch that also reverted
    would make "stop, look at what it did, then decide" impossible, which is the
    reason a human hits one. And the *run* closes its own entry: a switch that
    closed it would leave a live runner writing into a finished journal entry.
    The stop is also not a graph write, so it emits no event and appears in no
    cycle's diff.
    """
    gardener = auth.internal_principal()
    cycle = _running(gardener)
    with service.in_cycle(cycle.id):
        node = service.create_node(type="note", title="written before the stop", principal=owner())
    before = [(event.op, event.seq) for event in _events()]

    service.request_stop(cycle.id, principal=owner())

    still = service.get_cycle(cycle.id, principal=owner())
    assert still.status == "running"
    assert still.finished_at is None
    assert still.report is None
    assert service.get_node(node.id, principal=owner()).title == "written before the stop"
    assert [(event.op, event.seq) for event in _events()] == before

    # The run notices, closes its own entry, and the stop outlives it — a
    # journal entry has to go on saying who stopped this night.
    closed = service.close_cycle(
        cycle.id, status="failed", report={"stopped": True}, principal=owner()
    )
    assert closed.status == "failed"
    assert closed.stop_requested_by == "human:owner"


def test_a_stop_is_not_an_abandon(fresh_db):
    """K1: a repair and an instruction are different facts the journal keeps apart.

    `abandon_cycle` is a human declaring somebody else's dead process dead, from
    outside; a stop is an instruction a live run obeys and answers for itself.
    Both leave a `failed` cycle, and the human reading one at 09:00 needs to know
    which — so each is told by a record the other does not have, rather than by
    parsing a sentence.
    """
    gardener = auth.internal_principal()
    first = _running(gardener)
    service.request_stop(first.id, principal=owner())
    stopped = service.close_cycle(first.id, status="failed", report={}, principal=owner())

    second = _running(gardener)
    abandoned = service.abandon_cycle(second.id, principal=owner())

    assert stopped.stop_requested is True
    assert "abandoned" not in stopped.report
    assert abandoned.stop_requested is False
    assert abandoned.report["abandoned"] is True


def test_request_stop_is_human_only(fresh_db):
    """An instruction aimed at a process the caller cannot see.

    An agent able to stop the gardener's night could stop the review queue from
    ever being filled, and the gardener could stop itself — which is not a kill
    switch anybody is holding. `writer` holds `edit` on `main`, so this refuses
    the level that opens and closes cycles rather than merely refusing a
    principal with no grant.
    """
    gardener = auth.internal_principal()
    writer = agent("writer", grants={"meta": "read", "main": "edit"})
    cycle = _running(gardener)
    for principal in (gardener, writer):
        with pytest.raises(GrantNotPermitted, match="stop a consolidation cycle"):
            service.request_stop(cycle.id, principal=principal)
    assert service.stop_requested(cycle.id, principal=gardener) is False


def test_a_cycle_that_has_said_how_it_ended_cannot_be_told_to_stop(fresh_db):
    """Nothing is left to obey it, and the stamp would name a run that never saw it.

    `abandon_cycle` refuses the same state for the mirror reason: a cycle that
    has said how it ended is not one an interrupted run left behind.
    """
    cycle = _closed()
    with pytest.raises(InvalidTransition, match="not running"):
        service.request_stop(cycle.id, principal=owner())
    assert service.get_cycle(cycle.id, principal=owner()).stop_requested is False


def test_stopping_an_unknown_cycle_is_a_not_found(fresh_db):
    with pytest.raises(RecordNotFound, match="consolidation cycle not found"):
        service.request_stop("nope", principal=owner())
    with pytest.raises(RecordNotFound, match="consolidation cycle not found"):
        service.stop_requested("nope", principal=owner())


def test_asking_twice_keeps_the_first_asker(fresh_db):
    """A switch that raised because the run was already stopping would make a
    human hitting it twice doubt whether it worked at all — the one moment that
    must not be ambiguous. So the second call is a no-op, and *who stopped the
    night* stays the first answer to it rather than the last.
    """
    gardener = auth.internal_principal()
    cycle = _running(gardener)
    first = service.request_stop(cycle.id, principal=owner())

    colleague = service.create_human("colleague", principal=owner())
    second_human = auth.principal_from_actor(f"human:{colleague.id}")
    again = service.request_stop(cycle.id, principal=second_human)

    assert second_human.actor_string != "human:owner", "the second asker is the same human"
    assert again.stop_requested_by == "human:owner"
    assert again.stop_requested_at == first.stop_requested_at


def test_the_stop_read_is_scoped_by_the_authority_to_close_the_cycle(fresh_db):
    """Deliberately not human-only, and deliberately not unscoped either.

    `get_cycle` and `list_cycles` are human-only because a journal entry says
    what the gardener did across every space in the file, and an agent reading
    one learns the shape of territory it holds no grant on. This returns one
    boolean about a run and discloses nothing of the sort — and a runner that
    cannot ask whether it was told to stop cannot obey. What bounds it instead
    is the identical check `open_cycle` and `close_cycle` ask, because obeying a
    stop *is* closing the cycle: a principal that could not close this one has
    no use for the answer.
    """
    gardener = auth.internal_principal()
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    cycle = _running(gardener)

    # The runner may ask, though the journal itself stays closed to it.
    assert service.stop_requested(cycle.id, principal=gardener) is False
    with pytest.raises(GrantNotPermitted, match="consolidation journal"):
        service.get_cycle(cycle.id, principal=gardener)

    # And the bound is real, and is the *same* bound: the principal refused
    # here is refused by `close_cycle` too, which is the claim "scoped by the
    # authority to close the cycle" makes — checked rather than asserted.
    with pytest.raises(GrantNotPermitted, match="told to stop"):
        service.stop_requested(cycle.id, principal=proposer)
    with pytest.raises(GrantNotPermitted, match="close a consolidation cycle"):
        service.close_cycle(cycle.id, status="failed", report={}, principal=proposer)


def test_the_spaces_the_stop_read_is_checked_against_are_the_cycles_own(fresh_db):
    """`_cycle_authority_spaces` again: a scoped cycle is checked against its scope.

    "Holds `edit` somewhere" is what an *unscoped* cycle asks, and reading it as
    the general rule would let an agent with `edit` on one space ask about a run
    over another. The agent here holds `edit` — just not where the cycle is.
    """
    research = service.create_space("research", principal=owner())
    elsewhere = agent("elsewhere", grants={"meta": "read", research.id: "edit"})

    over_main = _open(scope="main")
    with pytest.raises(GrantNotPermitted, match="told to stop"):
        service.stop_requested(over_main.id, principal=elsewhere)

    over_research = _open(trigger="curative", scope=research.id, principal=elsewhere)
    assert service.stop_requested(over_research.id, principal=elsewhere) is False


def test_a_space_archived_mid_run_does_not_make_its_cycle_unstoppable(fresh_db):
    """The recorded scope, not a fresh resolution of it — `close_cycle`'s rule.

    Resolving it again would refuse (`_resolve_space` matches active spaces
    only), and a run would then go on spending against a switch it could not
    read. The principal is an **agent** on purpose: a human passes
    `require_review` before the scope is looked at at all, so a human-only
    fixture could not reach the branch this is about.
    """
    space = service.create_space("research", principal=owner())
    writer = agent("writer", grants={"meta": "read", space.id: "edit"})
    cycle = _open(trigger="curative", scope=space.id, principal=writer)
    service.archive_space(space.id, principal=owner())

    assert service.stop_requested(cycle.id, principal=writer) is False
    service.request_stop(cycle.id, principal=owner())
    assert service.stop_requested(cycle.id, principal=writer) is True


def test_the_stop_switch_is_armed_and_the_runtime_reads_the_real_service(fresh_db):
    """The runtime driven against the shipped service rather than against a fake.

    `nodum.agent.cycle_stop_check` was written blind against this interface and
    resolved to "keep going" while `service.stop_requested` did not exist;
    `tests/test_agent.py` reached the armed branch by installing a fake over a
    name that was not there, because the tree it was written in could not express
    the real one. This is the real one: no fake, no monkeypatch, one row, and
    the check re-read after the switch is hit — a check that answered from a
    value taken at the top of the run would be a kill switch that cannot be hit
    after the run starts, which is the only time anyone hits one. What is left to
    a fake over there is the call shape, and it now goes over the real function.
    """
    gardener = auth.internal_principal()
    cycle = _running(gardener)

    check = agent_runtime.cycle_stop_check(cycle.id, principal=gardener)
    assert check() is False
    run = agent_runtime.for_cycle(cycle_id=cycle.id, principal=gardener)
    assert run.report().stop_switch == agent_runtime.STOP_SWITCH_ARMED
    run.check_stop()

    service.request_stop(cycle.id, principal=owner())
    assert check() is True, "the check answered from a value read before the stop"
    with pytest.raises(agent_runtime.CycleStopped):
        run.check_stop()
    assert run.report().stopped is True


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
    with pytest.raises(RecordNotFound, match="consolidation cycle not found"):
        service.get_cycle("nope", principal=owner())


def test_list_cycles_is_newest_first_and_capped(fresh_db):
    first, second, third = _closed(), _closed(), _closed()
    assert [entry.id for entry in service.list_cycles(principal=owner())] == [
        third.id,
        second.id,
        first.id,
    ]
    assert [entry.id for entry in service.list_cycles(limit=2, principal=owner())] == [
        third.id,
        second.id,
    ]


def test_a_limit_below_one_is_an_error_and_not_the_whole_journal(fresh_db):
    """SQLite reads a negative LIMIT as *unbounded*, which is the opposite answer.

    `cycle-list --limit -3` returned every row in the journal — a caller asking
    for less got everything, silently. `subgraph` states the rule this follows.
    """
    _closed(), _closed()
    for limit in (0, -3):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            service.list_cycles(limit=limit, principal=owner())
    assert len(service.list_cycles(limit=1, principal=owner())) == 1


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
