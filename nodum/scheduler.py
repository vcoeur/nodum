"""The nightly consolidation scheduler (design decision J1).

The phase's exit criterion is a graph that maintains itself — "a cycle runs, on
demand and on a schedule" — and pushing the schedule onto ``cron`` would leave
that claim depending on a file this repo does not ship. So the schedule lives
where the server already is: one asyncio task in ``nodum serve``'s lifespan, no
new dependency, no second process.

Every property below is a design decision rather than an accident:

* **It cannot overlap itself.** The loop is sequential — the next wait is
  computed only after the run it follows has *returned* — so there is no timer
  that can fire into a cycle still in progress. A cycle is a writer against a
  single-writer database, and two of them at once would be a lock fight at 3am
  that nobody is awake to read.
* **It runs once a night, on the two nights a year that are not 24 hours
  long.** ``NODUM_CONSOLIDATE_AT`` is a *wall clock* time and a wall clock does
  not advance uniformly, so :func:`seconds_until` does its arithmetic in aware
  local time — see its docstring for the two transitions and what the naive
  version did on each.
* **A crash does not take the server down, and does not stop the schedule.**
  The runner already closes a failing cycle ``failed`` and leaves it in the
  journal; anything that escapes it is logged here and the loop waits for the
  next occurrence. A background task that dies silently is worse than one that
  fails loudly and tries again tomorrow.
* **A night somebody else was already consolidating is a *skip*, not a
  failure.** Consolidation cycles are serialised by a ``cycles`` row — migration
  ``0014``'s partial unique index — so a second opener is **refused**
  (:class:`nodum.consolidate.CycleInProgress`) rather than queued, whichever
  process it is in. A human running a cycle across the schedule's fire time
  therefore makes the timer bounce off it, and that is not rare and is getting
  less so: ``POST /api/cycles`` now runs off the event loop specifically so a
  human-triggered cycle may take minutes. ``CycleInProgress`` is a
  ``ValueError``, so it used to land on the generic handler above and be reported
  as ``scheduled consolidation cycle failed`` at ERROR with a full traceback — a
  fault report for a night when the graph was being consolidated exactly as
  intended, by somebody who asked for it. It is caught ahead of that handler and
  logged at WARNING, without a traceback and **carrying the runner's own
  sentence verbatim**, which is load-bearing rather than tidy: that sentence
  names the cycle in the way and the ``nodum cycle-abandon <id>`` that clears it.

  **Where a skipped night is visible is the server log, and deliberately
  nowhere else.** The obvious alternative is a journal row saying the night was
  skipped, and it is wrong three times over: the ``cycles`` table records cycles
  that *ran* (what was consolidated, by whom, how it ended), a row for a
  non-event would have no events under it — which is precisely the shape a
  ``dry_run`` entry has, the one this system leans on as the machine-checkable
  proof that a rehearsal changed nothing — and writing it would mean opening a
  cycle against the very index that just refused one. The journal is not silent
  about this either, which is the point: what a skipped night leaves behind is a
  cycle sitting there ``running``, so "why has nothing run since Tuesday" is
  answered by ``nodum cycle-list`` and the answer is actionable. The *cause* is
  in the journal because it is a cycle; the skip is a note about the schedule,
  so it goes where the schedule's other notes go — beside the "off because
  unparseable" warning and the ``serve`` banner's announcement that a schedule
  is on at all.

  This matters more since the guard moved into the database. A run a ``SIGKILL``
  ended never closes itself and now blocks **every** later night rather than
  only the ones sharing its process, so the nightly WARNING is the recurring
  signal that something needs abandoning — which is exactly why it must carry
  the remedy and must not be an ERROR that reads like the schedule itself is
  broken.
* **It is off unless configured, and "off" is an idle loop rather than no
  object.** :data:`ENV_CONSOLIDATE_AT` is unset by default, and a background
  process that writes to the human's graph without being asked is not something
  to enable by surprise — so nothing runs. But the *task* exists either way,
  because the alternative is what turning the schedule on from a browser used
  to require: stop this object, build another, start it. Two concurrent writes
  did that twice, leaving two live schedulers over one database with the loser
  unreachable from the app that would have to stop it. One object that reads
  the schedule and sometimes has nothing to do has neither failure.
* **The sleep is chunked, at :data:`SLICE_SECONDS`.** The loop re-reads the
  schedule on every slice, so a change — on, off, or to another hour, from this
  process or from a ``nodum config set`` in another one — applies at the next
  slice instead of at the next restart. ``NODUM_CONSOLIDATE_AT`` was read
  exactly once, at app construction, and by nothing afterwards; mutating the
  time in place would have taken effect after the current sleep expired, which
  is up to 24 hours away and therefore not a mechanism. The cost is one wakeup
  a minute; the last slice before a run is the exact remainder, so the cycle
  still fires on its own minute and the DST arithmetic below is untouched.
* **Shutdown does not wait for it, and never cancels a cycle.**
  :meth:`ConsolidationScheduler.stop` cancels the task and gives it
  :data:`SHUTDOWN_GRACE_SECONDS` before returning regardless. The common case is
  a task asleep, where cancelling the sleep is instant; the rare one is a cycle
  in flight, which runs on a worker thread that cannot be cancelled at all — it
  finishes its own transaction while the server goes down around it, exactly as
  an in-flight request does.

The cycle itself runs through :func:`asyncio.to_thread`. Request handlers on
this surface deliberately call the service inline, but this is the one call on
the server nobody is waiting for: running it in the event loop would stall every
request for the length of the cycle, and unlike a handler there is no client to
notice. The service opens its own short-lived connection per call, so the thread
brings no shared state with it.

The clock, the sleep, and the runner are all injectable — that is what lets the
tests drive a year of nights without sleeping through one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from nodum import consolidate, service, settings
from nodum.settings import parse_daily_time

#: The setting naming the local wall-clock time the nightly cycle runs at, as
#: ``HH:MM`` (24-hour). Unset or empty means **off**, which is the default: see
#: the module docstring for why that is the safe default rather than the timid
#: one. It is read through :mod:`nodum.settings`, so the environment wins and
#: ``settings.env`` is consulted after it — the name is kept as
#: ``ENV_CONSOLIDATE_AT`` because the environment is still the layer an
#: operator is told about first.
ENV_CONSOLIDATE_AT = settings.CONSOLIDATE_AT

#: The longest a loop iteration sleeps for. The schedule is re-read on every
#: slice, so this is how stale a schedule change can be before it applies — and
#: the price is one wakeup a minute, which is nothing against a task that would
#: otherwise sleep for a day and wake into a configuration nobody has told it
#: about. It is emphatically **not** how the schedule is kept: the last slice
#: before a run is the exact remainder, so a cycle still fires on its minute.
SLICE_SECONDS = 60.0

#: How long :meth:`ConsolidationScheduler.stop` waits for a cancelled task to
#: unwind before giving up on it and returning. A sleeping task unwinds in
#: microseconds; this budget exists for the one that is mid-cycle, and the
#: point of the budget is that it is *bounded* — shutdown proceeds either way.
SHUTDOWN_GRACE_SECONDS = 5.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Schedule:
    """The nightly schedule in force, and where it came from.

    ``at`` is ``None`` when the schedule is off — because nobody set it, or
    because what they set could not be parsed, and ``problem`` tells those two
    apart. ``source`` is the phrase a banner or a warning uses, so a message
    sends an operator to the layer that actually holds the value rather than to
    a variable they never set.
    """

    at: time | None
    source: str
    problem: str | None = None


def configured_time() -> time | None:
    """Read the schedule through the settings seam (environment, then ``settings.env``).

    Returns:
        The configured run time, or ``None`` when the scheduler is off.

    Raises:
        ValueError: If the value is set but unparseable. The domain function is
            strict; the adapters decide what a typo means, and both of them
            announce it and carry on without a schedule.
    """
    return parse_daily_time(settings.resolve(ENV_CONSOLIDATE_AT))


def schedule() -> Schedule:
    """The schedule in force, with a value it could not parse reported rather than raised.

    The tolerant reading of :func:`configured_time`, for the two callers that
    must not fail over an optional setting: the loop, which re-reads on every
    slice, and ``create_app``, which announces the problem and starts anyway.

    Returns:
        The :class:`Schedule` in force.
    """
    config = settings.snapshot()
    raw = config.value(ENV_CONSOLIDATE_AT)
    source = config.source(ENV_CONSOLIDATE_AT)
    try:
        return Schedule(at=parse_daily_time(raw), source=source)
    except ValueError as exc:
        return Schedule(at=None, source=source, problem=f"{exc} (from {source})")


def seconds_until(now: datetime, at: time) -> float:
    """Seconds from ``now`` to the next local occurrence of ``at``.

    The result is always strictly positive: firing at the instant the run
    finished would spin the loop, so an exact match rolls to tomorrow.

    **The arithmetic is done in aware local time, and that is what makes "one
    cycle a night" literally true.** ``at`` is a *wall clock* time, and a wall
    clock does not advance uniformly: on a DST fall-back the local day is 25
    hours long and on a spring-forward it is 23. Subtracting two naive
    datetimes measures neither — it measures the calendar — so the loop asked
    for 86400 seconds and got a wall clock an hour off at the far end. Driven
    over a real ``Europe/Paris`` timeline that ran the schedule **twice on the
    autumn fall-back** (waking an hour early, then again at the hour it was
    asked for) and **an hour late on the spring-forward**. Neither crashed and
    neither overlapped — the loop is sequential — but "one cycle a night" is the
    property, and it did not hold.

    :meth:`datetime.astimezone` on a naive value reads it as local wall time and
    attaches the offset in force *at that instant*, which is exactly the
    question here, so the difference is real elapsed seconds. Two wall-clock
    times are pathological by nature and answered rather than special-cased: a
    time that occurs **twice** (02:30 on a fall-back night) resolves to its
    first occurrence, so the cycle runs once; a time that occurs **not at all**
    (02:30 on a spring-forward night) resolves an hour later, so the cycle still
    runs once, on the right date.

    Args:
        now: The current local time, naive (as :func:`datetime.now` returns it)
            or aware.
        at: The local wall-clock time to run at.

    Returns:
        Real seconds until the next occurrence — never zero or negative.
    """
    local_now = now.astimezone()
    target = datetime.combine(local_now.date(), at).astimezone()
    if target <= local_now:
        target = datetime.combine(local_now.date() + timedelta(days=1), at).astimezone()
    return (target - local_now).total_seconds()


class ConsolidationScheduler:
    """A single asyncio task that runs one consolidation cycle a day.

    The lifecycle is :meth:`start` then :meth:`stop`, both driven by
    ``nodum serve``'s lifespan. Nothing else in the system creates one, and
    creating a second on the same database would defeat the no-overlap property
    this class exists to hold.
    """

    def __init__(
        self,
        *,
        at: time | None = None,
        db_path: str | Path | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], datetime] = datetime.now,
        run: Callable[..., Any] | None = None,
        slice_seconds: float = SLICE_SECONDS,
    ) -> None:
        """Build a scheduler; it starts nothing until :meth:`start` is called.

        Args:
            at: Local wall-clock time to run the cycle at. Given, it **pins**
                the schedule: the seam is not consulted and a settings write
                cannot move it. ``None`` — the default — means the schedule is
                whatever :func:`schedule` says it is, re-read on every slice.
            db_path: Explicit database path, passed straight to the runner.
            sleep: The wait between runs. Injected so a test can drive many
                nights without sleeping through one.
            clock: Local-time "now". Injected for the same reason.
            run: The runner, defaulting to :func:`nodum.consolidate.consolidate`.
                Called with keyword arguments only.
            slice_seconds: The longest one sleep may be; see :data:`SLICE_SECONDS`.
        """
        self._pinned_at = at
        self._db_path = db_path
        self._sleep = sleep
        self._clock = clock
        self._run = consolidate.consolidate if run is None else run
        self._slice = slice_seconds
        self._task: asyncio.Task[None] | None = None
        #: The last schedule problem announced, so a value that stays wrong is
        #: one log line rather than one a minute.
        self._announced: str | None = None

    @property
    def at(self) -> time | None:
        """The schedule in force: the constructor's pin, else the settings seam.

        A property rather than a stored value, and that is the whole of how a
        schedule change applies without a restart. The loop reads it on every
        slice, so turning the nightly cycle on, off, or to another hour is a
        write to ``settings.env`` (or to the environment, before the process
        starts) and nothing else — no scheduler is stopped, rebuilt or started,
        which is what stops two of them ever existing at once.
        """
        if self._pinned_at is not None:
            return self._pinned_at
        return self._schedule().at

    def _schedule(self) -> Schedule:
        """Read the schedule, announcing a value it could not parse **once**."""
        current = schedule()
        if current.problem != self._announced:
            if current.problem is not None:
                logger.warning("%s — the nightly consolidation cycle is off", current.problem)
            self._announced = current.problem
        return current

    @property
    def running(self) -> bool:
        """Whether the loop task exists and has not finished."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Create the loop task on the running event loop.

        Raises:
            RuntimeError: If this scheduler is already started. Two loops on one
                scheduler would be two cycles racing, which is the one thing
                this class promises cannot happen.
        """
        if self._task is not None:
            raise RuntimeError("this consolidation scheduler is already started")
        self._task = asyncio.create_task(self._loop(), name="nodum-consolidation")

    async def stop(self) -> None:
        """Cancel the loop and return within :data:`SHUTDOWN_GRACE_SECONDS`.

        ``asyncio.wait`` is used rather than awaiting the task directly: it
        neither re-raises the task's ``CancelledError`` into the shutdown path
        nor blocks past the timeout, so a cycle that is mid-write delays nothing
        — it is left to finish its own transaction while the server goes down
        around it, exactly as an in-flight request is.

        **The grace period is not a grace period for the cycle.** The cancel
        reaches the loop's ``await``, not the worker thread the cycle runs on:
        ``asyncio.to_thread`` cannot cancel a running thread, so a cycle
        mid-write is not interrupted and not waited for — it finishes into a
        database the server has stopped answering for. What the budget bounds is
        how long *shutdown* waits before proceeding without it, and the earlier
        wording here promised the cycle a window to unwind in that it never had.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        finished, _ = await asyncio.wait({task}, timeout=SHUTDOWN_GRACE_SECONDS)
        if not finished:
            logger.warning(
                "consolidation cycle still running after %ss; shutting down without it",
                SHUTDOWN_GRACE_SECONDS,
            )

    async def _loop(self) -> None:
        """Sleep in slices, re-reading the schedule each time, and run one cycle a night.

        The ordering is the no-overlap guarantee: the next wait is computed from
        the clock *after* the run returned, so a cycle that overran its window
        pushes the following night's start rather than being joined by it.

        **The sleep is chunked because the schedule can change under it.** One
        ``await sleep(86400)`` is a task that cannot notice anything for a day,
        which is why turning the nightly cycle on or off used to mean stopping
        this object and building another — two schedulers racing over one
        database, with the loser orphaned and unreachable from the app that
        would have to stop it. Slicing dissolves that: nothing is ever rebuilt,
        an unset schedule is an idle loop rather than a missing object, and a
        change applies at the next slice. An in-flight cycle is never
        interrupted by one, because the schedule is only consulted between
        runs.

        **The target instant is computed once and then held**, not re-derived
        after every wake. Re-deriving loses a night outright: a slice that
        overruns — a stalled event loop, a suspended host, a long
        ``to_thread`` hop — wakes past the scheduled minute, ``seconds_until``
        answers about *tomorrow*, and the cycle silently does not run. Measured
        with virtual time: a 90 s stall at 02:58 against an 03:00 schedule
        skipped the night entirely, where the single long sleep it replaced ran
        it late. Late is the right answer for a nightly sweep; skipped is not,
        and it is announced when it happens.

        The instant is **aware**, because that is what makes it a real moment
        rather than a wall-clock reading: :func:`seconds_until` measures real
        elapsed seconds, and adding those to a naive local datetime would land
        an hour out on the two nights a year the local day is not 24 hours
        long. Every wait is still a slice or the exact remainder, whichever is
        shorter.
        """
        target: datetime | None = None
        planned_for: time | None = None
        while True:
            at = self.at
            if at is None:
                # Idle: no schedule is in force. Still a loop rather than no
                # object, so one written from another process starts a cycle
                # tonight instead of at the next restart.
                target, planned_for = None, None
                await self._sleep(self._slice)
                continue
            now = self._clock()
            if target is None or planned_for != at:
                # First pass, or the schedule moved under us: the run this was
                # counting down to is not the one that is due now.
                target = now.astimezone() + timedelta(seconds=seconds_until(now, at))
                planned_for = at
            overdue = (now.astimezone() - target).total_seconds()
            if overdue < 0:
                await self._sleep(min(-overdue, self._slice))
                continue
            if overdue >= self._slice:
                # Something held the loop across the scheduled minute. The cycle
                # still runs; a human reading the log should know it ran late.
                logger.warning(
                    "scheduled consolidation cycle is starting %.0fs late (the loop was "
                    "held past %s)",
                    overdue,
                    at.strftime("%H:%M"),
                )
            target, planned_for = None, None
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except consolidate.CycleInProgress as busy:
                # A skipped night, not a failed one. See the module docstring for
                # why this is a log line and not a journal row.
                logger.warning("scheduled consolidation cycle skipped: %s", busy)
            except Exception:
                # The runner already records a failing cycle in the journal;
                # anything that escapes it is a bug, and a bug in the gardener
                # must not take down the server the human is using.
                logger.exception("scheduled consolidation cycle failed")

    async def _run_once(self) -> None:
        """Run one cycle off the event loop; the journal records the clock as asking."""
        await asyncio.to_thread(self._run, triggered_by=service.SCHEDULER_ACTOR, path=self._db_path)
