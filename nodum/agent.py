"""The internal agent's runtime — the one door to the model (Phase 5b).

Design P3: ``/ask`` *could* import :mod:`nodum.llm` and call it. It will not.
§8.4 says humans reach the internal agent through the API, and the reason is
attribution: a route that called the provider itself would be a second place
where a model call happens, with its own accounting, its own budget and its own
way of being absent. **Every provider call in the system goes through
:meth:`AgentRun.chat`**, which takes a principal, a job and a budget, and
returns the text together with the provenance a write must record.

**This module is a peer client too.** It opens no connection, imports no
service private, and mints no principal — it receives one. It produces the two
records the accounting needs and hands them back; the *writing* is the job's,
through the ordinary public :mod:`nodum.service` functions, so a model-caused
write is gated by the same grant, stamped by the same cycle and reversed by the
same rollback as any other.

## What gets recorded (A1–A3)

**The model goes on the event; the cost goes on the cycle; nothing
decision-bearing goes on the node.** :class:`GeneratedBy` — ``{provider,
model_id, prompt_version}`` — is the provenance a model-caused write carries,
and it belongs in the append-only log because that log is already this system's
answer to *who is answerable for this write*. ``actor`` stays
``agent:builtin-gardener``: the gardener made the write, the model is *how*.
Collapsing the two would make ``agent:llama3.2:1b`` an actor with no account,
no grants and nothing to revoke.

This is deliberately **not** ``chunks.model_id``'s mechanism (A3). Embeddings
are derived data: a model change is answered by ``projector rebuild vec``,
which replays the log and re-embeds everything. Generated text is not
regenerable — replaying the log would call the model again and get something
else — so its provenance has to live *in* the log rather than beside it. Do not
later "unify" the two into a ``model_id`` column on ``nodes``; that would put
irreproducible provenance into mutable state.

**How it reaches the log, given the service as it stands.** ``service._emit``'s
payload for a graph write is the row's ``before``/``after``, and no public write
function takes an extra payload key — so the route that exists today is
:meth:`GeneratedBy.as_props`, merged into the node's or edge's ``props``. That
lands the object *inside* the event payload (the payload **is** the row), never
rewritten because nothing rewrites an event, and removed by a rollback along
with the row. A1's objection to "the node" was to props as *mutable state no
event covers*; a prop written by the same call that emits the event is covered
by it. If a later wave wants the object off the row entirely, the service needs
a ``generated_by=`` keyword that ``_emit`` splices in beside ``before``/
``after`` — recorded in this phase's notes rather than reached around here.

``prompt_version`` (A2) is a short hash of the prompt *template*, because two
cycles a month apart can name the same ``model_id`` and produce different
proposals when the prompt changed, and a journal reporting acceptance rates by
model that cannot tell those apart is reporting a mixture. Compute it once at
import time from the template constant with :func:`prompt_version`.

## Budgets (B1–B4)

**Per-call ⊂ per-job ⊂ per-cycle**, because the three answer different
questions: per-cycle alone lets one job eat the night, per-job alone lets ten
jobs cost ten times what anyone agreed to, and per-call alone is what
``max_tokens`` already is. The cycle budget is the only one a human configures
(:data:`ENV_CYCLE_BUDGET`, **default 0 = the LLM jobs do not run**); job shares
are derived from it by a declared split (:meth:`Budget.split`).

**The unit is tokens, with wall clock as a second, independent ceiling** (B2).
Tokens because that is what ``usage`` reports and what a remote bill is
denominated in — calls are the wrong unit, since one call can cost 4 096 prompt
tokens or 90. But tokens alone do not bound a night: 2 395 prompt tokens cost
47 seconds on the local model, so a 40 000-token budget is a quarter of an hour
before anything notices. The two ceilings are independent because they bound
different resources — money and a night — and either one stops the work.

**At a ceiling: refuse, and itemise what was skipped** (B3). Never truncate the
prompt to fit: that is precisely the failure the server already commits
silently at its context window, and doing it deliberately makes a worse answer
indistinguishable from a good one. :meth:`AgentRun.chat` estimates the call's
worst case *before* sending, refuses with :class:`BudgetExhausted` when it does
not fit, and — when the caller named the item — records a
:class:`SkippedItem` naming what was not examined.

**A budget exhaustion is not a degraded path, and the report says so in a
different voice.** When ``fastembed`` is absent the runner writes a job *note*:
a stable statement about a configuration, true for every cycle until somebody
installs a model. Exhaustion is a transient statement about one run — the same
cycle tomorrow with a fresh budget does more work. So exhaustion is **never a
note**: it is :attr:`LLMReport.exhausted` plus an itemised
:attr:`LLMReport.skipped`, and a provider's absence is
:attr:`LLMReport.available` plus :attr:`LLMReport.unavailable_reason`. A human
who reads "degraded" and shrugs is right about the first and wrong about the
second, and ``tests/test_agent.py`` pins that the two shapes cannot be
confused.

**A dry run costs the full budget** (B4), and that is the point: a dry run that
skipped the model calls would rehearse nothing about the expensive half. This
module does not know about dry runs — the runner still calls
:meth:`AgentRun.chat` and still meters — which is what makes ``consolidate
--dry-run`` an answer to "what would tonight cost".

## The kill switch (K1–K3)

Three levels, and only the third is new. **Off** is
:data:`ENV_CYCLE_BUDGET` unset or 0. **Stop the gardener entirely** is ``nodum
agent disable builtin-gardener``, which already exists. **Stop this run now**
is a ``cycles.stop_requested`` row, checked here at the last of K3's three
points — immediately before every provider call — and by the runner at the
other two, between jobs and between items. Worst-case latency is one provider
call, bounded above by the per-call timeout; cancelling mid-call would buy
seconds and cost a torn transaction.

It is deliberately **not** ``abandon_cycle``. That verb is a *repair* — a human
declaring somebody else's dead process dead — and the kill switch is an
instruction to a live run which is expected to obey it and close its own cycle
honestly. A journal that could not tell "the operator stopped this" from "this
process died" would fail the human reading a ``failed`` cycle at 09:00. The
flag is a **row rather than a process signal** for the reason 5a's cycle
serialisation is a row: ``nodum cycle-stop`` typed at a terminal must stop a
cycle running inside ``nodum serve``, and those are two interpreters.

**The column is not this wave's to add.** Migration ``0015`` adds
``cycles.stop_requested`` and the service read that exposes it; until it lands,
:func:`cycle_stop_check` resolves to a check that always answers "keep going"
and :func:`stop_switch_available` is false, which the report carries as
:attr:`LLMReport.stop_switch` so a journal entry never implies a switch that
was not wired. See that function for exactly what the migration must add.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from nodum import llm, service
from nodum.llm import (
    Completion,
    ContextOverflow,
    LLMError,
    Message,
    OutputTruncated,
    PromptTooLong,
    ProviderTimeout,
    ProviderUnavailable,
)
from nodum.principal import Principal

__all__ = [
    "Budget",
    "BudgetExhausted",
    "Completion",
    "ContextOverflow",
    "CycleStopped",
    "GeneratedBy",
    "Generation",
    "JobCost",
    "LLMError",
    "LLMReport",
    "Message",
    "OutputTruncated",
    "PromptTooLong",
    "ProviderTimeout",
    "ProviderUnavailable",
    "SkippedItem",
    "AgentRun",
    "cycle_stop_check",
    "for_cycle",
    "for_request",
    "prompt_version",
    "stop_switch_available",
]

#: The key :class:`LLMReport` is filed under inside ``cycles.report`` (A1).
#: ``report`` is JSON, exactly as the metrics object is, which is why 5a made
#: both objects rather than columns — this needs no migration.
REPORT_KEY = "llm"

#: The ``props`` key :class:`GeneratedBy` is merged under. Named here so the
#: badge, the filter and the write all spell it once.
GENERATED_BY_PROP = "generated_by"

#: The per-**cycle** token budget, and the only budget a human configures (B1).
#: **Unset or 0 means the LLM jobs do not run** — the same posture as
#: ``NODUM_CONSOLIDATE_AT`` being unset, and K2's level 1.
ENV_CYCLE_BUDGET = "NODUM_LLM_CYCLE_BUDGET"

#: The per-cycle wall-clock ceiling in seconds (B2). Independent of the token
#: budget because they bound different resources.
ENV_CYCLE_SECONDS = "NODUM_LLM_CYCLE_SECONDS"

#: The token budget one human-initiated request may spend. Unlike the cycle
#: budget this defaults to *on*: "off by default" exists to stop an unattended
#: background process spending the human's night, and a human pressing a button
#: is not that. One request is still bounded, and bounded small.
ENV_REQUEST_BUDGET = "NODUM_LLM_REQUEST_BUDGET"

#: The wall-clock ceiling for one human-initiated request.
ENV_REQUEST_SECONDS = "NODUM_LLM_REQUEST_SECONDS"

#: The per-call wall-clock ceiling handed to the provider (B2).
ENV_CALL_TIMEOUT = "NODUM_LLM_CALL_TIMEOUT"

#: The per-call output ceiling. A call that comes back at it is a *failed*
#: call (B3) — the body is cut mid-token, measured — so this is sized for the
#: longest legitimate answer, not for the average one.
ENV_MAX_OUTPUT_TOKENS = "NODUM_LLM_MAX_OUTPUT_TOKENS"

DEFAULT_CYCLE_BUDGET = 0
DEFAULT_CYCLE_SECONDS = 1800.0
DEFAULT_REQUEST_BUDGET = 8000
DEFAULT_REQUEST_SECONDS = 180.0
DEFAULT_CALL_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT_TOKENS = 512

#: The public :mod:`nodum.service` read that answers "has this cycle been told
#: to stop?". It does not exist yet — migration ``0015`` and its wave own it —
#: and :func:`cycle_stop_check` gates on its presence rather than importing a
#: name that would be an ``AttributeError`` on every install today.
SERVICE_STOP_READ = "stop_requested"

#: What :attr:`LLMReport.stop_switch` says when the column and its read exist.
STOP_SWITCH_ARMED = "armed"

#: What it says when they do not. A journal entry must never imply a switch
#: that was not wired, and "no stop was requested" and "no stop *could* be
#: requested" are different facts.
STOP_SWITCH_PENDING = "pending: cycles.stop_requested is not in this database's schema yet"


class BudgetExhausted(RuntimeError):
    """A call was refused because a ceiling was reached — nothing was spent.

    Raised **before** the provider is touched, so the refusal costs nothing and
    the numbers in the message are the numbers as they stood. ``kind``
    distinguishes the three ways to be refused, because they call for different
    reactions and a journal must not print them in one voice:

    ``off``
        The budget was never turned on (:data:`ENV_CYCLE_BUDGET` is 0). Nothing
        was skipped through spending, so this does **not** set
        :attr:`LLMReport.exhausted`.
    ``tokens``
        The token ceiling is used up. Tomorrow's cycle does more work.
    ``seconds``
        The wall-clock ceiling is used up. Same.
    """

    def __init__(self, message: str, *, kind: str, scope: str, remaining: int, needed: int) -> None:
        super().__init__(message)
        self.kind = kind
        #: Which budget refused — a job's name, or the run's.
        self.scope = scope
        #: What was left, in tokens (``0`` for a wall-clock refusal).
        self.remaining = remaining
        #: What the next call needed, in tokens.
        self.needed = needed


class CycleStopped(RuntimeError):
    """A human asked this run to stop, and it is stopping (K1–K3).

    Distinct from every failure: the run did what it was told. The cycle still
    closes ``failed`` — the run did not do what it was *asked* to do, and a
    journal entry reading ``completed`` for a truncated night lies quietly —
    and every write already made stays, stamped with the cycle id, reversible
    by the same ``rollback_cycle`` that reverses any other. Stopping and
    undoing are two decisions; a kill switch that also reverted would make
    "stop, look at what it did, then decide" impossible, which is the reason a
    human hits one.
    """


class StopCheck(Protocol):
    """Answers "has this run been told to stop?" — one row read, no caching."""

    def __call__(self) -> bool:
        """``True`` when a stop has been requested."""
        ...


class GeneratedBy(BaseModel):
    """The provenance a model-caused write records (A1).

    Deliberately three fields and no cost: cost is a property of *the run*, not
    of any one write, and lives on the cycle report. "Last night's cycle spent
    40 000 tokens and got three proposals" is a sentence about a cycle; "this
    sentence was written by this model under this prompt" is a sentence about a
    write.
    """

    provider: str
    model_id: str
    prompt_version: str

    def as_props(self) -> dict[str, Any]:
        """This object as the ``props`` fragment a write merges in.

        The route to the event payload that the service supports today: a
        graph event's payload **is** the row, so a prop written by the call
        that emits the event is inside that payload, append-only with it, and
        deleted with the row by a rollback.
        """
        return {GENERATED_BY_PROP: self.model_dump(mode="json")}


class Generation(BaseModel):
    """One completed model call: the text, its provenance, and what it cost.

    **Every generation this type describes is whole.** A call that filled the
    context or hit its output ceiling never becomes one —
    :meth:`AgentRun.chat` charges it to the budget and raises
    :class:`~nodum.llm.ContextOverflow` or :class:`~nodum.llm.OutputTruncated`
    instead, because a truncated body is no result rather than a partial one.

    **Whole is not true.** Under a JSON schema this model answered a question
    its context could not answer with a schema-valid object claiming a
    citation it had not read. Validating ``text`` against what was actually
    retrieved is the caller's job, always, and nothing here does it.
    """

    text: str
    generated_by: GeneratedBy
    prompt_tokens: int
    output_tokens: int
    latency_ms: int


class SkippedItem(BaseModel):
    """One item a ceiling or a stop kept the run from examining (B3).

    The defence against a budget that silently produces a worse cycle is a
    loud, itemised list of what it did not get to.
    """

    job: str | None
    id: str
    reason: str


class JobCost(BaseModel):
    """What one job's share of the budget bought."""

    job: str
    budget_tokens: int
    calls: int
    failed_calls: int
    prompt_tokens: int
    output_tokens: int
    exhausted: bool


class LLMReport(BaseModel):
    """``cycles.report["llm"]`` — what the run cost and what it did not reach.

    Two absences are reported in two voices on purpose (B3). A provider that is
    not configured is :attr:`available` false with a :attr:`unavailable_reason`
    — a stable statement about this install, true every night until somebody
    changes it. A budget that ran out is :attr:`exhausted` true with an
    itemised :attr:`skipped` — a statement about *this* run, false again
    tomorrow. Neither is ever written as a job ``note``, which is the
    vocabulary 5a's degraded path already owns.
    """

    enabled: bool
    available: bool
    unavailable_reason: str | None
    provider: str | None
    model_id: str | None
    budget_tokens: int
    budget_seconds: float
    calls: int
    failed_calls: int
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    elapsed_seconds: float
    exhausted: bool
    stopped: bool
    stop_switch: str
    per_job: list[JobCost] = []
    skipped: list[SkippedItem] = []


@dataclass
class Budget:
    """A token ceiling, a wall-clock ceiling, and the ledger under both (B1/B2).

    Budgets nest: a job budget names a parent, charging it charges every
    ancestor, and what remains is the *minimum* down the chain — so a job
    cannot outspend its share and the shares together cannot outspend the
    cycle. The wall clock is one clock for the whole nesting: a child copies
    its parent's :attr:`started` and :attr:`seconds`, so "the night is over"
    means the same thing at every level, and no budget ever holds an infinite
    ceiling that JSON cannot carry.

    Attributes:
        name: What this budget is for — a job name, or the run's purpose.
        tokens: The token ceiling. ``0`` means off.
        seconds: The wall-clock ceiling, measured from :attr:`started`.
        parent: The budget this one is a share of, if any.
    """

    name: str
    tokens: int
    seconds: float
    parent: Budget | None = None
    spent_prompt_tokens: int = 0
    spent_output_tokens: int = 0
    calls: int = 0
    failed_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def spent_tokens(self) -> int:
        """What has been billed here, in the unit the ceiling is denominated in.

        Prompt and output are metered apart because they are priced apart
        everywhere a bill exists, and because they fail differently: a run that
        spent its budget on prompts is one sending too much context, and a run
        that spent it on output is one asking for too much text.
        """
        return self.spent_prompt_tokens + self.spent_output_tokens

    def split(self, shares: Mapping[str, float]) -> dict[str, Budget]:
        """Derive per-job budgets from a declared split of this one (B1).

        Args:
            shares: Job name → the fraction of this budget it may spend. Every
                fraction must be positive and they must not sum above 1: a
                split that over-committed would be per-job ceilings that add up
                to more than the cycle agreed to, which is the defect a
                per-cycle budget exists to prevent. Summing *below* 1 is fine
                and is how a run holds some back.

        Returns:
            One budget per name, in the order given.

        Raises:
            ValueError: If a share is not positive, or the shares over-commit.
        """
        for name, share in shares.items():
            if share <= 0:
                raise ValueError(f"share for {name!r} must be positive, got {share}")
        total = sum(shares.values())
        # A float split of thirds sums to 0.9999999999999999 or 1.0000000000000002
        # depending on the order; the tolerance is for that and nothing wider.
        if total > 1.0 + 1e-9:
            raise ValueError(
                f"job shares sum to {total:g}, which over-commits the {self.name} budget"
            )
        return {
            name: Budget(
                name=name,
                tokens=int(self.tokens * share),
                seconds=self.seconds,
                parent=self,
                started=self.started,
            )
            for name, share in shares.items()
        }

    @property
    def chain(self) -> list[Budget]:
        """This budget and every budget it is a share of, innermost first."""
        budgets: list[Budget] = []
        current: Budget | None = self
        while current is not None:
            budgets.append(current)
            current = current.parent
        return budgets

    @property
    def elapsed_seconds(self) -> float:
        """Wall clock since the run started."""
        return time.monotonic() - self.started

    @property
    def remaining_tokens(self) -> int:
        """Tokens left, bounded by every ancestor's remainder as well."""
        return min(budget.tokens - budget.spent_tokens for budget in self.chain)

    @property
    def remaining_seconds(self) -> float:
        """Wall clock left, bounded by every ancestor's ceiling as well."""
        return min(budget.seconds - budget.elapsed_seconds for budget in self.chain)

    @property
    def exhausted(self) -> bool:
        """Has either ceiling been reached?"""
        return self.remaining_tokens <= 0 or self.remaining_seconds <= 0

    def charge(self, completion: Completion) -> None:
        """Bill a call — including a failed one — to this budget and its ancestors.

        A call that came back ``finish_reason: "length"`` or with its context
        filled is charged exactly like a useful one, because the tokens were
        really spent. A meter that only counted successes would make a night of
        truncated calls look free.
        """
        for budget in self.chain:
            budget.spent_prompt_tokens += completion.prompt_tokens
            budget.spent_output_tokens += completion.output_tokens
            budget.calls += 1

    def note_failure(self) -> None:
        """Record a call that reached the wire and produced no usage to bill."""
        for budget in self.chain:
            budget.failed_calls += 1


def prompt_version(template: str) -> str:
    """A short, stable hash of a prompt template (A2).

    Compute it once, at import time, from the template constant — so it changes
    when and only when the template does. Two cycles a month apart can name the
    same ``model_id`` and produce different proposals because the prompt
    changed; a journal that cannot tell those apart is reporting a mixture.
    """
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]


def stop_switch_available() -> bool:
    """Is the kill switch's row read present in this build? (K3)

    False until migration ``0015``'s wave lands. Reported rather than assumed,
    because "no stop was requested" and "no stop could be requested" are
    different facts and a journal entry must not print the second as the first.
    """
    return callable(getattr(service, SERVICE_STOP_READ, None))


def cycle_stop_check(
    cycle_id: str, *, principal: Principal, path: str | Path | None = None
) -> StopCheck:
    """The stop check for one cycle: one row read, no caching, no signal (K3).

    A row rather than a process signal for the reason 5a's cycle serialisation
    is a row — ``nodum cycle-stop`` typed at a terminal must stop a cycle
    running inside ``nodum serve``, and those are two interpreters. No caching,
    because a check that answered from a value read at the top of the run would
    be a kill switch that cannot be hit after the run starts, which is the only
    time anyone hits one.

    **What migration ``0015`` and its wave must add for this to do anything:**

    1. ``ALTER TABLE cycles ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0``
       — plus, if the journal is to say *who* asked and *when*,
       ``stop_requested_by TEXT`` and ``stop_requested_at TEXT``.
    2. ``service.stop_requested(cycle_id, *, principal, path=None) -> bool`` —
       readable by the principal **running** the cycle, not human-only like
       ``get_cycle`` and ``list_cycles``. Those are human-only because a
       journal entry says what the gardener did across every space in the file;
       this read is one boolean about the caller's own run and discloses no
       territory, and a runner that could not ask whether it had been told to
       stop could not obey.
    3. ``service.request_stop(cycle_id, *, principal, path=None)`` — human-only,
       refusing a cycle that is not ``running``, and **not** a reuse of
       ``abandon_cycle``: stopping cleanly and crashing are different facts the
       journal has to keep apart (K1).
    4. ``CycleOut.stop_requested`` so both human surfaces can render it.

    Until then this returns a check that always answers "keep going", and
    :func:`stop_switch_available` says so. Gated on the function's presence
    rather than importing a name that would be an ``AttributeError`` on every
    install today.

    Args:
        cycle_id: The cycle to watch.
        principal: Who is asking — the principal the run acts as.
        path: Explicit database path.

    Returns:
        A callable answering ``True`` when a stop has been requested.
    """

    def check() -> bool:
        reader = getattr(service, SERVICE_STOP_READ, None)
        if reader is None:
            return False
        return bool(reader(cycle_id, principal=principal, path=path))

    return check


def _positive_int(name: str, default: int) -> int:
    """Read a non-negative whole number from the environment, or the default.

    An unparseable value falls back and does **not** raise: the scheduler's
    precedent is that a server refusing to boot over a stray character in an
    optional setting is worse than one that says what it skipped — and here the
    fallback for the cycle budget is 0, which is *off*, so a typo cannot
    accidentally authorise spending.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _positive_float(name: str, default: float) -> float:
    """Read a positive number of seconds from the environment, or the default."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class AgentRun:
    """One bounded stretch of model use: a cycle, or one human request.

    Holds the principal the work is attributed to, the budget it may spend, the
    stop check it obeys, and the accounting it hands back. Construct one
    through :func:`for_cycle` or :func:`for_request` rather than directly —
    those are the two callers there are, and each wires the defaults its posture
    calls for.
    """

    def __init__(
        self,
        *,
        principal: Principal,
        purpose: str,
        budget: Budget,
        stop: StopCheck | None = None,
        stop_switch: str = STOP_SWITCH_PENDING,
        max_output_tokens: int | None = None,
        call_timeout: float | None = None,
    ) -> None:
        self.principal = principal
        self.purpose = purpose
        self.budget = budget
        self.stop = stop
        self.stop_switch = stop_switch
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else _positive_int(ENV_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS)
        )
        self.call_timeout = (
            call_timeout
            if call_timeout is not None
            else _positive_float(ENV_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT)
        )
        self.provider = llm.get_provider()
        self.unavailable_reason = llm.unavailable_reason()
        self.stopped = False
        self._jobs: dict[str, Budget] = {}
        self._skipped: list[SkippedItem] = []
        # Which budgets refused a call on a *spending* ceiling. Not the same
        # question as `Budget.exhausted`: a budget with 10 tokens left that
        # cannot afford any call has stopped the work while its counter is
        # still above zero, and it is the stopping that a journal reader needs
        # told. A run switched off (`kind="off"`) never joins this set — nothing
        # was skipped through spending, and saying otherwise would send a human
        # looking for a night that cost too much.
        self._ceilings_hit: set[str] = set()

    # ── Posture ──────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """Is a provider configured at all? (A configuration, not this run.)"""
        return self.provider is not None

    @property
    def enabled(self) -> bool:
        """May this run spend anything — a provider *and* a non-zero budget?

        The two are separate on purpose: a missing provider is an install that
        cannot do smart features, and a zero budget is an install that has
        chosen not to. K2's level 1 is the second, and the runner reads this
        before deciding whether to run the LLM jobs at all — a job that ran and
        refused every call would fill the report with skips for a night nobody
        asked to spend.
        """
        return self.available and self.budget.tokens > 0

    @property
    def model_id(self) -> str | None:
        return self.provider.model_id if self.provider is not None else None

    @property
    def provider_id(self) -> str | None:
        return self.provider.provider_id if self.provider is not None else None

    def generated_by(self, prompt_version_hash: str) -> GeneratedBy:
        """The provenance object for a write this run's text caused (A1)."""
        if self.provider is None:
            raise ProviderUnavailable(self.unavailable_reason or "no LLM provider configured")
        return GeneratedBy(
            provider=self.provider.provider_id,
            model_id=self.provider.model_id,
            prompt_version=prompt_version_hash,
        )

    # ── Budgets ──────────────────────────────────────────────────────────────

    def split(self, shares: Mapping[str, float]) -> dict[str, Budget]:
        """Declare the per-job split of this run's budget (B1).

        The returned budgets are remembered, so :meth:`report` lists every job
        the run declared — including one that never made a call, which is how a
        reader tells "this job had no work" from "this job never got a turn".
        """
        jobs = self.budget.split(shares)
        self._jobs.update(jobs)
        return jobs

    def job(self, name: str, *, share: float) -> Budget:
        """One job's share of this run's budget — :meth:`split` for a single job."""
        return self.split({name: share})[name]

    # ── The kill switch ──────────────────────────────────────────────────────

    def check_stop(self, *, job: Budget | None = None, item_id: str | None = None) -> None:
        """Raise :class:`CycleStopped` if a human asked this run to stop (K3).

        Call it between jobs, between items inside a job's write loop, and — as
        :meth:`chat` already does — immediately before every provider call.
        Naming ``item_id`` records the skip, so the journal says what the stop
        cost as well as that it happened.

        Raises:
            CycleStopped: If a stop has been requested.
        """
        if self.stop is None or not self.stop():
            return
        self.stopped = True
        reason = "stopped: a human asked this run to stop"
        if item_id is not None:
            self.record_skip(job=job, item_id=item_id, reason=reason)
        raise CycleStopped(reason)

    # ── The one door ─────────────────────────────────────────────────────────

    def chat(
        self,
        messages: Sequence[Message],
        *,
        prompt_version: str,
        schema: dict[str, Any] | None = None,
        job: Budget | None = None,
        item_id: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float | None = None,
    ) -> Generation:
        """Make one provider call, metered, bounded and attributable (P3).

        The order is the whole design. A stop is checked first, because a run
        told to stop must not spend. The call's **worst case** — the prompt's
        over-counted estimate plus the whole output reservation — is measured
        against the budget next, and refused before anything is sent, so the
        budget is never overrun by a call whose cost was only discovered
        afterwards. The provider then refuses an over-long prompt on its own
        ceiling. Only after all three does a request go out; whatever comes
        back is charged, *including* a failure, and a body that filled the
        context or hit its output ceiling is discarded rather than returned.

        Args:
            messages: The prompt.
            prompt_version: :func:`prompt_version` of the template that
                rendered it (A2) — recorded on the write, not on the cost.
            schema: A JSON schema for structured output. It fixes the envelope
                and nothing else: never read a schema-valid object as a true
                one.
            job: The job budget to bill, or ``None`` to bill the run's directly.
            item_id: What this call was about. Naming it is what turns a
                refusal into an itemised :class:`SkippedItem` rather than a
                number that dropped.
            max_output_tokens: Per-call output ceiling; defaults to the run's.
            timeout: Per-call wall-clock ceiling; defaults to the run's.

        Returns:
            A whole :class:`Generation`.

        Raises:
            CycleStopped: If a human asked this run to stop.
            BudgetExhausted: If a ceiling was reached. Nothing was spent.
            ProviderUnavailable: If no provider is configured, or the one
                configured could not be reached.
            PromptTooLong: If the prompt does not fit the model's window.
                Nothing was spent — this is the refusal that exists so an
                over-long prompt is never silently answered from a prefix.
            ContextOverflow: If the server filled its context anyway. Charged.
            OutputTruncated: If the output ceiling bit. Charged.
        """
        ledger = job if job is not None else self.budget
        output_ceiling = (
            max_output_tokens if max_output_tokens is not None else self.max_output_tokens
        )
        call_timeout = timeout if timeout is not None else self.call_timeout

        self.check_stop(job=job, item_id=item_id)
        provider = self.provider
        if provider is None:
            raise ProviderUnavailable(self.unavailable_reason or "no LLM provider configured")

        needed = provider.estimate_prompt_tokens(messages) + output_ceiling
        self._require_budget(ledger, needed, job=job, item_id=item_id)

        try:
            completion = provider.chat(
                messages,
                schema=schema,
                max_output_tokens=output_ceiling,
                timeout=call_timeout,
            )
        except PromptTooLong:
            # Refused before the wire: no tokens spent, nothing to charge.
            raise
        except ProviderUnavailable:
            ledger.note_failure()
            raise

        ledger.charge(completion)
        if completion.context_filled:
            raise ContextOverflow(
                f"the provider filled its {completion.context_tokens}-token context "
                f"({completion.prompt_tokens} prompt tokens), so part of the prompt was "
                f"never read and the answer is from a prefix. The call is charged because "
                f"it was really spent. Lower {llm.ENV_CONTEXT_TOKENS} to the model's real "
                f"window, or send less context",
                completion,
            )
        if completion.output_truncated:
            raise OutputTruncated(
                f"the answer hit its {output_ceiling}-token ceiling and is cut mid-token, "
                f"so it is no result rather than a short one. The call is charged because "
                f"it was really spent",
                completion,
            )
        return Generation(
            text=completion.text,
            generated_by=self.generated_by(prompt_version),
            prompt_tokens=completion.prompt_tokens,
            output_tokens=completion.output_tokens,
            latency_ms=completion.latency_ms,
        )

    # ── Accounting ───────────────────────────────────────────────────────────

    def record_skip(self, *, job: Budget | None, item_id: str, reason: str) -> None:
        """Record one item this run did not examine, and why (B3)."""
        self._skipped.append(
            SkippedItem(job=job.name if job is not None else None, id=item_id, reason=reason)
        )

    def report(self) -> LLMReport:
        """The ``llm`` object for ``cycles.report`` (A1), or a request's ``used``.

        :attr:`LLMReport.exhausted` says **a spending ceiling stopped work**,
        not that a counter reached exactly zero. Those come apart in the
        ordinary case: a budget with 10 tokens left cannot afford any call, so
        the run is over while ``remaining_tokens`` is still positive, and a flag
        computed from the counter would report a truncated night as a complete
        one. A run with the budget switched off reports ``enabled: false`` and
        ``exhausted: false``, because nothing was skipped through spending and a
        journal saying otherwise would send a human looking for a night that
        cost too much.
        """
        return LLMReport(
            enabled=self.enabled,
            available=self.available,
            unavailable_reason=self.unavailable_reason,
            provider=self.provider_id,
            model_id=self.model_id,
            budget_tokens=self.budget.tokens,
            budget_seconds=self.budget.seconds,
            calls=self.budget.calls,
            failed_calls=self.budget.failed_calls,
            prompt_tokens=self.budget.spent_prompt_tokens,
            output_tokens=self.budget.spent_output_tokens,
            total_tokens=self.budget.spent_tokens,
            elapsed_seconds=round(self.budget.elapsed_seconds, 3),
            exhausted=bool(self._ceilings_hit),
            stopped=self.stopped,
            stop_switch=self.stop_switch,
            per_job=[
                JobCost(
                    job=budget.name,
                    budget_tokens=budget.tokens,
                    calls=budget.calls,
                    failed_calls=budget.failed_calls,
                    prompt_tokens=budget.spent_prompt_tokens,
                    output_tokens=budget.spent_output_tokens,
                    exhausted=budget.name in self._ceilings_hit,
                )
                for budget in self._jobs.values()
            ],
            skipped=list(self._skipped),
        )

    def _require_budget(
        self, ledger: Budget, needed: int, *, job: Budget | None, item_id: str | None
    ) -> None:
        """Refuse the call if its worst case does not fit — and itemise (B3).

        Never truncates the prompt to fit. That is exactly the failure the
        server already commits silently at its context window, and committing
        it deliberately would make a worse answer indistinguishable from a good
        one.
        """
        if ledger.tokens <= 0:
            self._refuse(
                ledger,
                job=job,
                item_id=item_id,
                kind="off",
                remaining=0,
                needed=needed,
                message=(
                    f"the LLM budget for {ledger.name!r} is 0, so no model call is made "
                    f"(set {ENV_CYCLE_BUDGET} to a token budget to turn the LLM jobs on)"
                ),
            )
        remaining_seconds = ledger.remaining_seconds
        if remaining_seconds <= 0:
            self._refuse(
                ledger,
                job=job,
                item_id=item_id,
                kind="seconds",
                remaining=0,
                needed=needed,
                message=(
                    f"the wall-clock ceiling for {ledger.name!r} is used up "
                    f"({ledger.seconds:g}s), so this call is refused rather than made. "
                    f"Tokens alone do not bound a night: a 2 395-token prompt costs 47 "
                    f"seconds on the local model"
                ),
            )
        remaining = ledger.remaining_tokens
        if needed > remaining:
            self._refuse(
                ledger,
                job=job,
                item_id=item_id,
                kind="tokens",
                remaining=remaining,
                needed=needed,
                message=(
                    f"the token budget for {ledger.name!r} has {remaining} left and this "
                    f"call needs up to {needed}, so it is refused rather than made with a "
                    f"shortened prompt — a truncated prompt produces an answer nobody can "
                    f"tell from a good one"
                ),
            )

    def _refuse(
        self,
        ledger: Budget,
        *,
        job: Budget | None,
        item_id: str | None,
        kind: str,
        remaining: int,
        needed: int,
        message: str,
    ) -> None:
        """Record the ceiling, itemise the skip, and raise.

        ``off`` is deliberately not recorded: a run nobody funded skipped
        nothing through spending, and :attr:`LLMReport.exhausted` must not say
        it did.
        """
        if kind != "off":
            self._ceilings_hit.add(ledger.name)
        if item_id is not None:
            self.record_skip(job=job, item_id=item_id, reason=message)
        raise BudgetExhausted(
            message, kind=kind, scope=ledger.name, remaining=remaining, needed=needed
        )


def for_cycle(
    *,
    cycle_id: str,
    principal: Principal,
    path: str | Path | None = None,
    budget: Budget | None = None,
) -> AgentRun:
    """The runtime for one consolidation cycle (P3, B1, K3).

    Reads the cycle budget from the environment — **0 by default, which means
    the LLM jobs do not run** — and wires the kill switch to this cycle's row.

    Args:
        cycle_id: The open cycle. Its ``stop_requested`` row is what the kill
            switch reads.
        principal: The principal the run acts as (the gardener).
        path: Explicit database path.
        budget: An explicit budget, overriding the environment. For tests and
            for a caller that has already decided what tonight may cost.
    """
    return AgentRun(
        principal=principal,
        purpose=f"cycle:{cycle_id}",
        budget=budget
        or Budget(
            name=f"cycle:{cycle_id}",
            tokens=_positive_int(ENV_CYCLE_BUDGET, DEFAULT_CYCLE_BUDGET),
            seconds=_positive_float(ENV_CYCLE_SECONDS, DEFAULT_CYCLE_SECONDS),
        ),
        stop=cycle_stop_check(cycle_id, principal=principal, path=path),
        stop_switch=STOP_SWITCH_ARMED if stop_switch_available() else STOP_SWITCH_PENDING,
    )


def for_request(*, purpose: str, principal: Principal, budget: Budget | None = None) -> AgentRun:
    """The runtime for one human-initiated request — ``/ask`` and its siblings.

    No cycle, so no kill switch: the request is bounded by its own token and
    wall-clock ceilings and by the human who can close the tab. The budget
    defaults to *on*, unlike a cycle's: "off by default" exists to stop an
    unattended background process spending the human's night, and a human
    pressing a button is not that.

    Args:
        purpose: What the request is — ``ask``, ``summarize``, ``search-rewrite``.
        principal: The session's human principal.
        budget: An explicit budget, overriding the environment.
    """
    return AgentRun(
        principal=principal,
        purpose=f"request:{purpose}",
        budget=budget
        or Budget(
            name=f"request:{purpose}",
            tokens=_positive_int(ENV_REQUEST_BUDGET, DEFAULT_REQUEST_BUDGET),
            seconds=_positive_float(ENV_REQUEST_SECONDS, DEFAULT_REQUEST_SECONDS),
        ),
    )
