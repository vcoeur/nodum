"""The nightly consolidation scheduler (design decision J1).

The phase's exit criterion is a graph that maintains itself — "a cycle runs, on
demand and on a schedule" — and pushing the schedule onto ``cron`` would leave
that claim depending on a file this repo does not ship. So the schedule lives
where the server already is: one asyncio task in ``nodum serve``'s lifespan, no
new dependency, no second process.

Four properties, and each one is a design decision rather than an accident:

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
* **It is off unless configured.** :data:`ENV_CONSOLIDATE_AT` is unset by
  default and an unset value means no task is created at all. A background
  process that writes to the human's graph without being asked is not something
  to enable by surprise, and the on-demand halves — the CLI and
  ``POST /api/cycles`` — need no scheduler at all.
* **Shutdown does not wait for it.** :meth:`ConsolidationScheduler.stop`
  cancels the task and gives it :data:`SHUTDOWN_GRACE_SECONDS` to unwind, then
  returns regardless. The common case is a task asleep until tomorrow, where
  cancelling the sleep is instant; the rare one is a cycle in flight, and a
  shutdown that blocked on it would be a server that takes minutes to stop
  because it happened to be tidying up.

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
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from nodum import consolidate, service

#: Environment variable naming the local wall-clock time the nightly cycle runs
#: at, as ``HH:MM`` (24-hour). Unset or empty means **off**, which is the
#: default: see the module docstring for why that is the safe default rather
#: than the timid one.
ENV_CONSOLIDATE_AT = "NODUM_CONSOLIDATE_AT"

#: How long :meth:`ConsolidationScheduler.stop` waits for a cancelled task to
#: unwind before giving up on it and returning. A sleeping task unwinds in
#: microseconds; this budget exists for the one that is mid-cycle, and the
#: point of the budget is that it is *bounded* — shutdown proceeds either way.
SHUTDOWN_GRACE_SECONDS = 5.0

logger = logging.getLogger(__name__)


def parse_daily_time(value: str | None) -> time | None:
    """Parse ``HH:MM`` (or ``HH:MM:SS``) into a local wall-clock time.

    Args:
        value: The configured value; ``None`` or blank means "not configured".

    Returns:
        The time of day the cycle should run at, or ``None`` when the scheduler
        is off.

    Raises:
        ValueError: If the value is not a 24-hour ``HH:MM`` time. This refuses
            rather than falling back to "off", so the caller gets to decide what
            a typo means; ``nodum.http_api`` announces it and starts without a
            schedule, because a server that will not boot over a stray character
            in an optional setting is worse than one that says what it ignored.
    """
    if value is None or not value.strip():
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"{ENV_CONSOLIDATE_AT} must be a 24-hour HH:MM time, got {value!r}"
        ) from exc


def configured_time(environ: dict[str, str] | None = None) -> time | None:
    """Read :data:`ENV_CONSOLIDATE_AT` from the environment.

    Args:
        environ: The mapping to read; defaults to ``os.environ``.

    Returns:
        The configured run time, or ``None`` when the scheduler is off.

    Raises:
        ValueError: If the value is set but unparseable.
    """
    source = os.environ if environ is None else environ
    return parse_daily_time(source.get(ENV_CONSOLIDATE_AT))


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
        at: time,
        db_path: str | Path | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], datetime] = datetime.now,
        run: Callable[..., Any] | None = None,
    ) -> None:
        """Build a scheduler; it starts nothing until :meth:`start` is called.

        Args:
            at: Local wall-clock time to run the cycle at.
            db_path: Explicit database path, passed straight to the runner.
            sleep: The wait between runs. Injected so a test can drive many
                nights without sleeping through one.
            clock: Local-time "now". Injected for the same reason.
            run: The runner, defaulting to :func:`nodum.consolidate.consolidate`.
                Called with keyword arguments only.
        """
        self.at = at
        self._db_path = db_path
        self._sleep = sleep
        self._clock = clock
        self._run = consolidate.consolidate if run is None else run
        self._task: asyncio.Task[None] | None = None

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
        """Wait for the next occurrence, run one cycle, repeat — sequentially.

        The ordering is the no-overlap guarantee: the next wait is computed from
        the clock *after* the run returned, so a cycle that overran its window
        pushes the following night's start rather than being joined by it.
        """
        while True:
            await self._sleep(seconds_until(self._clock(), self.at))
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The runner already records a failing cycle in the journal;
                # anything that escapes it is a bug, and a bug in the gardener
                # must not take down the server the human is using.
                logger.exception("scheduled consolidation cycle failed")

    async def _run_once(self) -> None:
        """Run one cycle off the event loop; the journal records the clock as asking."""
        await asyncio.to_thread(self._run, triggered_by=service.SCHEDULER_ACTOR, path=self._db_path)
