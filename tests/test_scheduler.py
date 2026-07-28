"""The nightly consolidation scheduler (design decision J1).

**Nothing here sleeps for real.** The scheduler takes its clock, its sleep and
its runner as arguments precisely so a test can drive a week of nights inside
one event-loop iteration: :class:`_VirtualTime` is a clock and a sleep that
agree with each other — sleeping *is* how time passes — so the delays the loop
asks for become assertions instead of a wait, and a test that pinned real time
would only be measuring the machine it ran on.

Four properties carry the file, and they are the four a background writer has to
have: it runs on the schedule, it never overlaps itself, a crash inside a cycle
neither kills the loop nor the server, and shutting the server down does not
wait for a cycle that is still writing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as time_module
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from helpers import owner
from typer.testing import CliRunner

from nodum import cli, consolidate, http_api, scheduler, service
from nodum.migrations import GARDENER_AGENT_ID

#: The name the loop task carries, so the lifespan tests can find it in
#: ``asyncio.all_tasks()`` without reaching into the app.
TASK_NAME = "nodum-consolidation"

#: How long a test waits on a flag another thread sets before calling it a
#: failure. Generous: it is a deadlock detector, never a timing assertion.
FLAG_TIMEOUT = 5.0


class _VirtualTime:
    """A clock and a sleep that agree: sleeping is how time passes here.

    The loop's ``await sleep(delay)`` advances the clock by exactly ``delay``
    and returns, so the sequence of delays it asked for is a complete record of
    the schedule it kept — and the whole week takes microseconds.
    """

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.delays: list[float] = []

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += timedelta(seconds=delay)
        # Yield to the loop, so a pending cancellation is delivered here rather
        # than only at the next real await.
        await asyncio.sleep(0)


class _FakeRunner:
    """A stand-in for the consolidation runner, recording how it was driven.

    It runs in a worker thread (:func:`asyncio.to_thread`), so the counters are
    guarded: the *point* of ``max_concurrent`` is to catch two cycles running at
    once, and a counter that raced would report the bug as a flake.

    After ``signal_after`` calls it sets :attr:`reached` and then blocks on
    :attr:`hold` — which is what lets a test assert "exactly this many nights
    ran" and, in the shutdown test, holds a cycle open while the server stops.
    """

    def __init__(self, *, signal_after: int = 1, fail_on: tuple[int, ...] = ()) -> None:
        self.calls: list[dict] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.reached = threading.Event()
        self.hold = threading.Event()
        self._lock = threading.Lock()
        self._signal_after = signal_after
        self._fail_on = set(fail_on)

    def __call__(self, **kwargs: object) -> None:
        with self._lock:
            self.calls.append(kwargs)
            index = len(self.calls)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if index >= self._signal_after:
                self.reached.set()
                self.hold.wait(FLAG_TIMEOUT)
            if index in self._fail_on:
                raise RuntimeError(f"cycle {index} exploded")
        finally:
            with self._lock:
                self.concurrent -= 1

    def release(self) -> None:
        """Let a held cycle finish, so no worker thread outlives the test."""
        self.hold.set()


async def _await_flag(flag: threading.Event, timeout: float = FLAG_TIMEOUT) -> None:
    """Wait on another thread's flag without blocking the event loop."""
    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, flag.wait, timeout), "the scheduler never got there"


def _scheduler(virtual: _VirtualTime, runner: _FakeRunner, at: time = time(3, 0)):
    """A scheduler wired to virtual time and a fake runner."""
    return scheduler.ConsolidationScheduler(
        at=at, sleep=virtual.sleep, clock=virtual.clock, run=runner
    )


# ── The next occurrence ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "at", "expected_hours"),
    [
        # Before the hour: later today.
        (datetime(2026, 7, 27, 0, 30), time(3, 0), 2.5),
        # After it: tomorrow.
        (datetime(2026, 7, 27, 12, 0), time(3, 0), 15.0),
        # Exactly on it: tomorrow, not now — firing at the instant the last run
        # finished would spin the loop instead of scheduling it.
        (datetime(2026, 7, 27, 3, 0), time(3, 0), 24.0),
    ],
)
def test_the_next_occurrence_is_always_strictly_in_the_future(now, at, expected_hours):
    assert scheduler.seconds_until(now, at) == expected_hours * 3600


# ── The two nights a year that are not 24 hours long ──────────────────────────


@contextmanager
def _local_zone(name: str):
    """Run the block with the *process's* local zone set to ``name``.

    The scheduler reads a local wall clock, because that is what
    ``datetime.now()`` gives it, so a DST property cannot be driven by handing
    it aware datetimes — it has to be driven by a process whose local zone
    actually has DST. The ambient zone is restored on the way out, so this
    depends on the zone it names and never on the one CI happens to run in.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time_module.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time_module.tzset()


class _ZonedVirtualTime:
    """Virtual time over a **real** instant, reported as local wall time.

    :class:`_VirtualTime` is a naive clock, which is exactly why it cannot see
    this bug: it advances the wall clock by the delay asked for, so a wall clock
    that skips or repeats an hour is unrepresentable in it. Here the state is an
    aware UTC instant — what a real ``asyncio.sleep`` advances — and
    :meth:`clock` renders it the way ``datetime.now()`` would on a machine in
    this zone: naive local wall time.
    """

    def __init__(self, instant: datetime, zone: ZoneInfo) -> None:
        self.instant = instant
        self.zone = zone
        self.delays: list[float] = []

    def clock(self) -> datetime:
        return self.instant.astimezone(self.zone).replace(tzinfo=None)

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.instant += timedelta(seconds=delay)
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("start_utc", "nights"),
    [
        # The autumn fall-back: 2026-10-25 03:00 CEST becomes 02:00 CET, so the
        # local day is 25 hours. Naive arithmetic asked for 86400 seconds and
        # woke an hour early, ran, then ran *again* at the hour it was asked
        # for — two cycles on one night.
        (
            datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
            [date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)],
        ),
        # The spring-forward: 2026-03-29 02:00 CET becomes 03:00 CEST, a
        # 23-hour day. Naive arithmetic ran the cycle an hour late.
        (
            datetime(2026, 3, 27, 12, 0, tzinfo=UTC),
            [date(2026, 3, 28), date(2026, 3, 29), date(2026, 3, 30)],
        ),
    ],
    ids=["autumn-fall-back", "spring-forward"],
)
def test_one_cycle_a_night_holds_across_a_dst_transition(start_utc, nights):
    """The property is "one cycle a night", and a wall clock is not a duration.

    Driven over a real ``Europe/Paris`` timeline: each run's local wall clock
    must be the configured 03:00, and the dates must be consecutive with no
    repeat. The naive version failed both halves — twice on one date in autumn,
    at 04:00 in spring.
    """
    with _local_zone("Europe/Paris"):
        virtual = _ZonedVirtualTime(start_utc, ZoneInfo("Europe/Paris"))
        runs: list[datetime] = []
        reached, hold = threading.Event(), threading.Event()

        def record(**kwargs: object) -> None:
            runs.append(virtual.clock())
            if len(runs) >= len(nights):
                # Held open, as `_FakeRunner` does: the loop's sleeps are
                # virtual, so without this it would race ahead through further
                # nights while the test was still stopping it.
                reached.set()
                hold.wait(FLAG_TIMEOUT)

        nightly = _scheduler(virtual, record, at=time(3, 0))

        async def drive() -> None:
            nightly.start()
            try:
                await _await_flag(reached)
                await nightly.stop()
            finally:
                hold.set()

        asyncio.run(drive())

    assert [moment.date() for moment in runs] == nights
    assert {moment.time() for moment in runs} == {time(3, 0)}


# ── Configuration ─────────────────────────────────────────────────────────────


def test_the_schedule_is_off_unless_the_environment_names_a_time(monkeypatch):
    """Off by default: a process that writes to the graph unasked is not a default."""
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)
    assert scheduler.configured_time() is None

    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "03:30")
    assert scheduler.configured_time() == time(3, 30)

    # Blank is "not configured" too — an exported-but-empty variable is how a
    # shell profile says nothing at all.
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "  ")
    assert scheduler.configured_time() is None


def test_an_unparseable_time_is_refused_by_name(monkeypatch):
    """The domain function is strict; the adapter decides what to do about it."""
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "3pm")
    with pytest.raises(ValueError, match=scheduler.ENV_CONSOLIDATE_AT):
        scheduler.configured_time()


def test_a_misconfigured_schedule_is_announced_and_the_server_still_starts(monkeypatch, caplog):
    """A stray character in an optional schedule must not stop the server.

    It must not be silent either — that is the failure nobody notices for a
    month — so it is a warning on the console beside the banners ``nodum serve``
    already prints, and the app is built with no schedule at all.
    """
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "half past three")

    with caplog.at_level(logging.WARNING, logger="nodum.http_api"):
        app = http_api.create_app()

    assert scheduler.ENV_CONSOLIDATE_AT in caplog.text
    assert "off" in caplog.text
    assert asyncio.run(_lifespan_task_names(app)) == set()


def test_a_configured_schedule_is_announced_by_the_serve_banner(fresh_db, monkeypatch):
    """The setting that *works* was the silent one, which is the wrong way round.

    An unparseable ``NODUM_CONSOLIDATE_AT`` got a warning; a valid one produced
    nothing at all on the console — so the one setting that starts a background
    writer on the human's graph could be on, at 03:00, with no line anywhere
    saying so. ``serve`` prints the database path and the auth posture already;
    this belongs beside them.
    """
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "03:30")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "nightly consolidation cycle: 03:30 local time" in result.stderr
    assert scheduler.ENV_CONSOLIDATE_AT in result.stderr


def test_an_unconfigured_schedule_says_nothing_at_all(fresh_db, monkeypatch):
    """Off by default is the default, and a banner for it would be noise."""
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "nightly consolidation cycle" not in result.stderr


def test_a_misconfigured_schedule_is_not_announced_twice(fresh_db, monkeypatch):
    """One voice per fact: ``create_app`` owns the "ignored, and here is why" line."""
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "half past three")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "nightly consolidation cycle:" not in result.stderr


# ── The loop ──────────────────────────────────────────────────────────────────


def test_a_cycle_runs_once_a_night_and_never_overlaps_itself():
    """The schedule and the no-overlap guarantee, read off the delays it asked for.

    The loop is sequential by construction — the next wait is computed from the
    clock *after* the run returned — so there is no timer that can fire into a
    cycle still in progress. Two cycles at once would be two writers on a
    single-writer database at 3am, with nobody awake to read the lock fight.
    """
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    runner = _FakeRunner(signal_after=3)

    async def drive() -> None:
        nightly = _scheduler(virtual, runner)
        nightly.start()
        try:
            await _await_flag(runner.reached)
            await nightly.stop()
        finally:
            runner.release()

    asyncio.run(drive())

    # Noon to 03:00 is fifteen hours; every night after that is a whole day.
    assert virtual.delays[0] == 15 * 3600
    assert virtual.delays[1:3] == [86400.0, 86400.0]
    assert runner.max_concurrent == 1
    assert len(runner.calls) == 3
    # Nobody asked — the clock did, and the journal has to say so.
    assert {call["triggered_by"] for call in runner.calls} == {service.SCHEDULER_ACTOR}


def test_a_crashing_cycle_does_not_stop_the_loop():
    """A bug in the gardener must not take down the server the human is using."""
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    runner = _FakeRunner(signal_after=3, fail_on=(1, 2))

    async def drive() -> None:
        nightly = _scheduler(virtual, runner)
        nightly.start()
        try:
            await _await_flag(runner.reached)
            assert nightly.running, "the loop died with the cycle it was running"
            await nightly.stop()
        finally:
            runner.release()

    asyncio.run(drive())

    assert len(runner.calls) == 3
    # Two nights blew up and the third still ran on schedule.
    assert virtual.delays[1:3] == [86400.0, 86400.0]


def test_shutdown_does_not_wait_for_a_cycle_that_is_still_writing():
    """The rare case the grace period exists for, timed rather than asserted about.

    A held cycle stands in for one mid-transaction. ``stop`` cancels the loop,
    gives it a bounded moment, and returns — the alternative is a server that
    takes minutes to stop because it happened to be tidying up.
    """
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    runner = _FakeRunner(signal_after=1)
    elapsed: list[float] = []

    async def drive() -> None:
        nightly = _scheduler(virtual, runner)
        nightly.start()
        try:
            await _await_flag(runner.reached)
            started = time_module.perf_counter()
            await nightly.stop()
            elapsed.append(time_module.perf_counter() - started)
            assert not nightly.running
        finally:
            runner.release()

    asyncio.run(drive())

    assert elapsed[0] < scheduler.SHUTDOWN_GRACE_SECONDS / 2, elapsed
    # And the cycle really was in flight rather than merely scheduled: the fake
    # signalled from inside the call and then blocked there until `release`.
    assert runner.reached.is_set()
    assert len(runner.calls) == 1


def test_a_scheduler_cannot_be_started_twice():
    """Two loops on one scheduler is two cycles racing — the one thing it promises not to do."""

    async def drive() -> None:
        nightly = _scheduler(_VirtualTime(datetime(2026, 7, 27, 12, 0)), _FakeRunner())
        nightly.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                nightly.start()
        finally:
            await nightly.stop()

    asyncio.run(drive())


# ── The server's lifespan owns it ─────────────────────────────────────────────


async def _lifespan_task_names(app) -> set[str]:
    """Run the app's lifespan and report the task names alive inside it.

    The driving task is excluded, so an empty set means "this app started
    nothing" rather than "this app started nothing but the test".
    """
    async with app.router.lifespan_context(app):
        current = asyncio.current_task()
        return {task.get_name() for task in asyncio.all_tasks() if task is not current}


def test_the_lifespan_starts_the_task_and_takes_it_back(monkeypatch):
    """``nodum serve``'s startup and shutdown are the scheduler's whole lifecycle."""
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)
    app = http_api.create_app(consolidate_at=time(3, 0))

    async def drive() -> set[str]:
        inside = await _lifespan_task_names(app)
        assert TASK_NAME in inside
        # Outside the context the task is finished, so it is no longer pending.
        return {task.get_name() for task in asyncio.all_tasks()}

    assert TASK_NAME not in asyncio.run(drive())


def test_an_unconfigured_server_creates_no_background_writer(monkeypatch):
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)

    assert asyncio.run(_lifespan_task_names(http_api.create_app())) == set()


def test_the_environment_variable_is_all_it_takes_to_turn_it_on(monkeypatch):
    """No flag on ``nodum serve``: the app reads the variable when it is built."""
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "03:00")

    assert TASK_NAME in asyncio.run(_lifespan_task_names(http_api.create_app()))


# ── End to end: the clock writes a real journal entry ─────────────────────────


def test_a_scheduled_run_lands_in_the_journal_as_the_clock(fresh_db):
    """The wiring, against the real runner and a real database.

    The only stand-in is a wrapper that signals when the first cycle is done and
    holds the second, so the test can assert without racing the loop; the cycle
    itself is the runner ``nodum serve`` would call, on the path it was given.
    """
    service.create_node(type="claim", title="Kafka Streams", principal=owner())
    service.create_node(type="claim", title="Kafka Streams", principal=owner())
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    done, hold = threading.Event(), threading.Event()

    def run_once_then_hold(**kwargs):
        if done.is_set():
            hold.wait(FLAG_TIMEOUT)
            return None
        outcome = consolidate.consolidate(**kwargs)
        done.set()
        return outcome

    async def drive() -> None:
        nightly = scheduler.ConsolidationScheduler(
            at=time(3, 0),
            db_path=str(fresh_db),
            sleep=virtual.sleep,
            clock=virtual.clock,
            run=run_once_then_hold,
        )
        nightly.start()
        try:
            await _await_flag(done)
            await nightly.stop()
        finally:
            hold.set()

    asyncio.run(drive())

    (entry,) = [cycle for cycle in service.list_cycles(principal=owner())]
    assert entry.trigger == "scheduled"
    assert entry.triggered_by == service.SCHEDULER_ACTOR
    assert entry.status == "completed"
    assert entry.report is not None
    # And the writes inside are the gardener's, not the clock's: `triggered_by`
    # is who asked, the event actor is who acted.
    events = service.list_events(owner(), limit=500, cycle_id=entry.id)
    assert events, "the cycle had duplicates to propose"
    assert {event.actor for event in events} == {f"agent:{GARDENER_AGENT_ID}"}

    # Nothing else is wired in: the default runner is the real one.
    assert scheduler.ConsolidationScheduler(at=time(3, 0))._run is consolidate.consolidate
