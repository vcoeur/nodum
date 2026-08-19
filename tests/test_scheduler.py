"""The nightly consolidation scheduler (design decision J1).

**Nothing here sleeps for real.** The scheduler takes its clock, its sleep and
its runner as arguments precisely so a test can drive a week of nights inside
one event-loop iteration: :class:`_VirtualTime` is a clock and a sleep that
agree with each other — sleeping *is* how time passes — so the delays the loop
asks for become assertions instead of a wait, and a test that pinned real time
would only be measuring the machine it ran on.

Five properties carry the file, and they are the five a background writer has to
have: it runs on the schedule, it never overlaps itself, a crash inside a cycle
neither kills the loop nor the server, shutting the server down does not wait
for a cycle that is still writing — and a night it *skipped* because a human was
already consolidating reads as the skip it is rather than as a fault, which is
the one of the five that is about what a human is told rather than about what
runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as time_module
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from helpers import owner
from typer.testing import CliRunner

from nodum import cli, consolidate, http_api, scheduler, service, settings
from nodum.migrations import GARDENER_AGENT_ID

#: The name the loop task carries, so the lifespan tests can find it in
#: ``asyncio.all_tasks()`` without reaching into the app.
TASK_NAME = "nodum-consolidation"

#: How long a test waits on a flag another thread sets before calling it a
#: failure. Generous: it is a deadlock detector, never a timing assertion.
FLAG_TIMEOUT = 5.0

#: Stands in for the runner's own refusal text. The loop must carry the reason
#: through to the log line, because "skipped" without it says nothing a human can
#: act on.
BUSY_MESSAGE = "a consolidation cycle is already running"


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

    def __init__(
        self,
        *,
        signal_after: int = 1,
        fail_on: tuple[int, ...] = (),
        busy_on: tuple[int, ...] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        #: The virtual moment of each call. Since the loop sleeps in slices, the
        #: delays it asked for are no longer a readable record of the schedule
        #: it kept — *when it ran* is, and it is the property anyway.
        self.moments: list[datetime] = []
        self._clock = clock
        self.concurrent = 0
        self.max_concurrent = 0
        self.reached = threading.Event()
        self.hold = threading.Event()
        self._lock = threading.Lock()
        self._signal_after = signal_after
        self._fail_on = set(fail_on)
        self._busy_on = set(busy_on)

    def __call__(self, **kwargs: object) -> None:
        with self._lock:
            self.calls.append(kwargs)
            if self._clock is not None:
                self.moments.append(self._clock())
            index = len(self.calls)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if index >= self._signal_after:
                self.reached.set()
                self.hold.wait(FLAG_TIMEOUT)
            if index in self._busy_on:
                # What the real runner raises when somebody else holds the
                # cycle: a refusal, not a fault. Raised from the same place a
                # crash is, so the loop has to tell the two apart itself.
                raise consolidate.CycleInProgress(BUSY_MESSAGE)
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


def _scheduler(virtual, runner, at: time | None = time(3, 0)):
    """A scheduler wired to virtual time and a fake runner.

    ``at`` given **pins** the schedule, which is what every test below wants:
    the schedule under test is the one the test named, not whatever the ambient
    configuration says. ``None`` leaves it reading the settings seam, which is
    what the liveness tests drive.
    """
    return scheduler.ConsolidationScheduler(
        at=at, sleep=virtual.sleep, clock=virtual.clock, run=runner
    )


def _nights(runner: _FakeRunner) -> list[str]:
    """The virtual moments a runner was called at, as ``YYYY-MM-DD HH:MM``."""
    return [moment.strftime("%Y-%m-%d %H:%M") for moment in runner.moments]


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
    already prints, and the app starts with the schedule off.
    """
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "half past three")

    with caplog.at_level(logging.WARNING, logger="nodum.http_api"):
        app = http_api.create_app()

    assert scheduler.ENV_CONSOLIDATE_AT in caplog.text
    assert "off" in caplog.text
    # The task exists — it always does now — and has nothing to do.
    assert TASK_NAME in asyncio.run(_lifespan_task_names(app))
    assert scheduler.schedule().at is None


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
    # And which of the two layers holds it: "unset it to turn the schedule off"
    # is advice a reader cannot act on without being told where it is set.
    assert "from the environment" in result.stderr


def test_a_schedule_stored_in_the_settings_file_is_announced_as_such(fresh_db, monkeypatch):
    """The same banner, naming the other layer."""
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    settings.set_value(settings.CONSOLIDATE_AT, "04:15")

    result = CliRunner().invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "nightly consolidation cycle: 04:15 local time" in result.stderr
    assert f"from {settings.SETTINGS_FILENAME}" in result.stderr


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
    runner = _FakeRunner(signal_after=3, clock=virtual.clock)

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
    # Read off *when it ran* rather than off the delays it asked for: the loop
    # sleeps in slices now, so the delays are a record of the slicing and the
    # moments are the record of the schedule.
    assert _nights(runner) == ["2026-07-28 03:00", "2026-07-29 03:00", "2026-07-30 03:00"]
    assert max(virtual.delays) <= scheduler.SLICE_SECONDS
    assert runner.max_concurrent == 1
    assert len(runner.calls) == 3
    # Nobody asked — the clock did, and the journal has to say so.
    assert {call["triggered_by"] for call in runner.calls} == {service.SCHEDULER_ACTOR}


def test_a_crashing_cycle_does_not_stop_the_loop():
    """A bug in the gardener must not take down the server the human is using."""
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    runner = _FakeRunner(signal_after=3, fail_on=(1, 2), clock=virtual.clock)

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
    assert _nights(runner) == ["2026-07-28 03:00", "2026-07-29 03:00", "2026-07-30 03:00"]


def test_a_night_a_human_was_already_consolidating_is_logged_as_a_skip(caplog):
    """A busy night is a skip, and a skip is not a failure.

    ``CycleInProgress`` is a ``ValueError``, so it landed on the generic
    ``except Exception`` below it and the night was reported as
    ``scheduled consolidation cycle failed`` at ERROR with a full traceback. The
    collision is not rare and is getting less rare: ``POST /api/cycles`` moved
    off the event loop precisely so a human-triggered cycle can take minutes, and
    a human running one across 03:00 is exactly the case the runner's refusal
    exists for. Nothing was broken — a cycle *was* running, the runner declined
    to start a second, and the graph is being consolidated as we speak — so an
    ERROR-level traceback sends a human looking for a fault that is not there.

    Three things are asserted, and the third is the one that matters: the level
    is not ERROR, the record carries **no** exception info (a traceback is what
    says "read me, something is wrong"), and the runner's own reason survives
    into the line, because "skipped" alone is not actionable.
    """
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    runner = _FakeRunner(signal_after=3, busy_on=(1, 2), clock=virtual.clock)

    async def drive() -> None:
        nightly = _scheduler(virtual, runner)
        nightly.start()
        try:
            await _await_flag(runner.reached)
            assert nightly.running, "a skipped night must not end the schedule"
            await nightly.stop()
        finally:
            runner.release()

    with caplog.at_level(logging.INFO, logger=scheduler.logger.name):
        asyncio.run(drive())

    records = [record for record in caplog.records if record.name == scheduler.logger.name]
    assert len(records) == 2, [record.getMessage() for record in records]
    for record in records:
        assert record.levelno < logging.ERROR, record.levelname
        assert record.exc_info is None, "a skip is not a fault, so it carries no traceback"
        assert "skip" in record.getMessage().lower()
        assert BUSY_MESSAGE in record.getMessage()
    # And the schedule kept its shape: the third night ran a day after the second.
    assert len(runner.calls) == 3
    assert _nights(runner) == ["2026-07-28 03:00", "2026-07-29 03:00", "2026-07-30 03:00"]


def test_a_crash_is_still_a_failure_with_its_traceback(caplog):
    """The other half of the split: a bug in the gardener still reads as one.

    Narrowing the skip out of the generic handler must not narrow the handler
    itself — a cycle that raised something nobody predicted is still an ERROR
    with the traceback attached, because that is a thing to go and read.
    """
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    runner = _FakeRunner(signal_after=2, fail_on=(1,))

    async def drive() -> None:
        nightly = _scheduler(virtual, runner)
        nightly.start()
        try:
            await _await_flag(runner.reached)
            await nightly.stop()
        finally:
            runner.release()

    with caplog.at_level(logging.INFO, logger=scheduler.logger.name):
        asyncio.run(drive())

    (record,) = [r for r in caplog.records if r.name == scheduler.logger.name]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert "failed" in record.getMessage()


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


def test_an_unconfigured_server_starts_the_task_and_it_writes_nothing(monkeypatch):
    """Off by default is still off — but "off" is an idle loop, not a missing object.

    The task exists whether or not a schedule is configured, and that is the
    whole of what makes a schedule live: turning the nightly cycle on is a
    write that the running loop reads at its next slice, with nothing stopped,
    rebuilt or started. The version that built the object only when configured
    had no way to turn one on, and the version that stopped and rebuilt it left
    two schedulers over one database the first time two writes landed together.
    """
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)

    assert TASK_NAME in asyncio.run(_lifespan_task_names(http_api.create_app()))
    assert scheduler.schedule().at is None, "nothing is scheduled, so nothing runs"


def test_the_environment_variable_is_all_it_takes_to_turn_it_on(monkeypatch):
    """No flag on ``nodum serve``: the app reads the variable through the seam."""
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "03:00")

    assert TASK_NAME in asyncio.run(_lifespan_task_names(http_api.create_app()))
    assert scheduler.schedule() == scheduler.Schedule(at=time(3, 0), source="the environment")


def test_a_constructor_argument_pins_the_schedule_against_the_seam(monkeypatch):
    """The ladder is constructor argument > environment > settings.env > off.

    The pin is what the tests in this file rely on, and what an embedder gets
    when they build an app with a schedule of their own: a settings write must
    not move a schedule somebody passed in code.
    """
    monkeypatch.setenv(scheduler.ENV_CONSOLIDATE_AT, "23:00")
    pinned = scheduler.ConsolidationScheduler(at=time(3, 0))
    reading = scheduler.ConsolidationScheduler()

    assert pinned.at == time(3, 0)
    assert reading.at == time(23, 0)

    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT)
    settings.set_value(settings.CONSOLIDATE_AT, "04:15")

    assert pinned.at == time(3, 0)
    assert reading.at == time(4, 15)


# ── Liveness: the schedule changes while the loop is running ──────────────────
#
# The D8 class this file owns. `NODUM_CONSOLIDATE_AT` was read once, at app
# construction, and by nothing afterwards — so a schedule written from a browser
# or from a second process applied at the next restart and not before. These two
# drive the change through the store, against a loop that is already running and
# is never stopped, rebuilt or started.


def _writing_clock(virtual: _VirtualTime, actions: dict[int, object]):
    """A sleep that performs ``actions[n]`` after the *n*-th slice.

    Driving the write from inside the loop's own sleep is what makes these
    deterministic: the change lands between two slices, at a slice number the
    test chose, rather than whenever another thread happened to get scheduled.
    """

    async def sleep(delay: float) -> None:
        await virtual.sleep(delay)
        action = actions.pop(len(virtual.delays), None)
        if action is not None:
            action()  # pyright: ignore[reportCallIssue] - test callables

    return sleep


def test_a_schedule_written_while_the_server_runs_starts_the_nightly_cycle(monkeypatch):
    """Unset → set, against a loop started with **no** schedule at all.

    Nothing is restarted and no second scheduler exists: the loop re-reads the
    seam on its next slice and starts counting down to the time that was just
    stored.
    """
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    runner = _FakeRunner(signal_after=1, clock=virtual.clock)
    written: list[int] = []

    def turn_it_on() -> None:
        written.append(len(virtual.delays))
        settings.set_value(settings.CONSOLIDATE_AT, "03:00")

    nightly = scheduler.ConsolidationScheduler(
        sleep=_writing_clock(virtual, {3: turn_it_on}), clock=virtual.clock, run=runner
    )

    async def drive() -> None:
        nightly.start()
        try:
            await _await_flag(runner.reached)
            await nightly.stop()
        finally:
            runner.release()

    asyncio.run(drive())

    assert written == [3], "the schedule was written after the third idle slice"
    assert _nights(runner) == ["2026-07-28 03:00"]
    assert virtual.delays[:3] == [60.0, 60.0, 60.0], "an unset schedule idles a slice at a time"


def test_a_schedule_removed_while_the_server_runs_stops_the_nightly_cycle(monkeypatch):
    """Set → unset, and then a whole day passes with nothing run.

    A day is the point: the cycle was due within the hour, and after the unset
    it does not run then or at any later occurrence — which is what "off" has to
    mean for a control a human just used.
    """
    monkeypatch.delenv(scheduler.ENV_CONSOLIDATE_AT, raising=False)
    settings.set_value(settings.CONSOLIDATE_AT, "03:00")
    virtual = _VirtualTime(datetime(2026, 7, 28, 2, 0))
    runner = _FakeRunner(signal_after=1, clock=virtual.clock)
    exhausted = threading.Event()
    #: Slices covering the whole of the next day, so a schedule that was merely
    #: *delayed* rather than removed would still be caught firing.
    budget = 26 * 60

    def turn_it_off() -> None:
        settings.unset_value(settings.CONSOLIDATE_AT)

    def give_up() -> None:
        exhausted.set()

    nightly = scheduler.ConsolidationScheduler(
        sleep=_writing_clock(virtual, {2: turn_it_off, budget: give_up}),
        clock=virtual.clock,
        run=runner,
    )

    async def drive() -> None:
        nightly.start()
        try:
            while not exhausted.is_set() and nightly.running:
                await asyncio.sleep(0)
        finally:
            await nightly.stop()
            runner.release()

    asyncio.run(drive())

    assert exhausted.is_set(), "the loop ended early; it should have idled the whole budget"
    assert runner.calls == [], "the cycle ran after its schedule was removed"
    assert virtual.now > datetime(2026, 7, 29, 3, 0), "the clock never passed the next occurrence"


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


def test_a_stranded_cycle_makes_every_night_a_skip_that_names_the_way_out(fresh_db, caplog):
    """The skip path end to end, against the real runner and the real guard.

    Since the serialisation moved into the database, an open ``running`` cycle
    blocks a run **from any process** — and one a ``SIGKILL`` left behind never
    closes itself, so it blocks the schedule every night from then on. That makes
    the log line the whole of what a human gets, which is why it has to carry the
    runner's own sentence rather than a summary of it: the refusal names the
    cycle in the way and the `nodum cycle-abandon <id>` that clears it, and a
    skip logged as "skipped" alone would be a nightly notice with no cure in it.

    The cause is not invisible either — the blocking cycle is sitting in the
    journal as ``running``, which is where a human looking for "why has nothing
    run since Tuesday" would find it. That is the division the skip decision
    rests on: the journal records cycles, the log records the schedule.
    """
    service.create_node(type="claim", title="Kafka Streams", principal=owner())
    service.create_node(type="claim", title="Kafka Streams", principal=owner())
    stranded = service.open_cycle(trigger="manual", principal=owner())
    virtual = _VirtualTime(datetime(2026, 7, 27, 12, 0))
    tried = threading.Event()

    def run_and_signal(**kwargs):
        try:
            return consolidate.consolidate(**kwargs)
        finally:
            tried.set()

    async def drive() -> None:
        nightly = scheduler.ConsolidationScheduler(
            at=time(3, 0),
            db_path=str(fresh_db),
            sleep=virtual.sleep,
            clock=virtual.clock,
            run=run_and_signal,
        )
        nightly.start()
        try:
            await _await_flag(tried)
            assert nightly.running, "a blocked night must not end the schedule"
        finally:
            await nightly.stop()

    with caplog.at_level(logging.INFO, logger=scheduler.logger.name):
        asyncio.run(drive())

    skips = [r for r in caplog.records if r.name == scheduler.logger.name]
    assert skips, "a night that could not run said nothing at all"
    for record in skips:
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert f"nodum cycle-abandon {stranded.id}" in record.getMessage()
    # The journal holds the cause, unchanged: no entry for the nights that were
    # skipped, and the cycle that is blocking them still open.
    assert [(c.id, c.status) for c in service.list_cycles(principal=owner())] == [
        (stranded.id, "running")
    ]
