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

**The cost goes on the cycle; nothing decision-bearing goes on the node.**
:class:`GeneratedBy` — ``{provider, model_id, prompt_version}`` — is the
provenance of a model-caused write, and :class:`Generation` carries it beside
the text. It is no longer merged into the write's ``props``: for a while it
was (the payload of a graph event **is** the row, so a prop written by the
emitting call rode the append-only log, removed by a rollback along with the
row), but nothing ever read it there — the abstraction job's concept was the
only write to carry it, and M21 took the write out rather than keep a prop
nobody consumes. What a human or a job can still ask — *who is answerable for
this write* — the event's actor already answers: ``actor`` stays
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

**If a later wave wants the object on the row.** ``service._emit``'s payload
for a graph write is the row's ``before``/``after``, and no public write
function takes an extra payload key; the route would be a ``generated_by=``
keyword that ``_emit`` splices in beside ``before``/``after`` — recorded in
this phase's notes rather than reached around here.

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
is a stop recorded on the ``cycles`` row, checked here at the last of K3's three
points — immediately before every provider call — and by an LLM job at the other
two, between jobs and between items (:meth:`AgentRun.check_stop`). Worst-case
latency is one provider call, bounded above by the per-call timeout; cancelling
mid-call would buy seconds and cost a torn transaction.

It is deliberately **not** ``abandon_cycle``. That verb is a *repair* — a human
declaring somebody else's dead process dead — and the kill switch is an
instruction to a live run which is expected to obey it and close its own cycle
honestly. A journal that could not tell "the operator stopped this" from "this
process died" would fail the human reading a ``failed`` cycle at 09:00. The
flag is a **row rather than a process signal** for the reason 5a's cycle
serialisation is a row: ``nodum cycle-stop`` typed at a terminal must stop a
cycle running inside ``nodum serve``, and those are two interpreters.

**The row is migration ``0015``'s** — ``cycles.stop_requested_at`` and
``cycles.stop_requested_by`` — and :func:`cycle_stop_check` reads it through
:func:`nodum.service.stop_requested`. A human hits the switch with ``nodum
cycle-stop <id>`` or ``POST /api/cycles/{id}/stop``; both go through
:func:`nodum.service.request_stop`, which is human-only, refuses a cycle that is
not ``running``, and stamps the row without closing it. :attr:`LLMReport.
stop_switch` says which posture a run had, because a journal entry must never
imply a switch that was not there: a cycle has a row anybody can stamp
(:data:`STOP_SWITCH_ARMED`), and a human's request has none
(:data:`STOP_SWITCH_NONE`) — it is bounded by its own ceilings and by the human
who can close the tab.

**What obeys it, today.** Every provider call goes through :meth:`AgentRun.chat`,
which checks first, so any run that reaches a model is stoppable. The four
*deterministic* consolidation jobs in :mod:`nodum.consolidate` make no provider
call and no stop check, so a stop recorded against one of those runs is kept in
the journal and that run finishes on its own — the abstraction job (5b-ii's
first) is the exception, and it is the thing this paragraph now names: it
consults the switch through ``AgentRun.chat``, which is exactly the per-call
check described above. The between-jobs and between-items checks are the later
5b-ii jobs'.
 The human surfaces
say so rather than promising a stop that would not arrive.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from nodum import llm, service
from nodum.llm import (
    STRUCTURED_JSON_OBJECT,
    STRUCTURED_JSON_SCHEMA,
    THINKING_LEVELS,
    THINKING_NONE,
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
    # Re-exported from `nodum.llm` so a peer client like `nodum.answers` can name
    # a reasoning level or a structured-output mode without importing the
    # provider module — the same reason `Message` and `Completion` are here.
    "STRUCTURED_JSON_OBJECT",
    "STRUCTURED_JSON_SCHEMA",
    "THINKING_LEVELS",
    "THINKING_NONE",
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
]

#: The key :class:`LLMReport` is filed under inside ``cycles.report`` (A1).
#: ``report`` is JSON, exactly as the metrics object is, which is why 5a made
#: both objects rather than columns — this needs no migration.
REPORT_KEY = "llm"

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

#: The per-call output ceiling, and it is sized for a **reasoning** model
#: because that is what a remote provider now is.
#:
#: 512 was measured against ``deepseek-v4-flash`` as *below the floor at which
#: anything works at all*: a ceiling sweep on one synthesis prompt was perfectly
#: bimodal — 300, 400 and 500 gave ``finish_reason: "length"`` and an
#: unparseable body on every run, 650 and above parsed on every run — so the
#: shipped default turned every call on that provider into a B3 failure. There
#: is no parseable-but-degraded band, which is what makes a ``length`` finish
#: *no result* rather than a short one.
#:
#: 4 096 is not "650 plus margin", because the floor is not the number that
#: matters. What matters is that **thinking is spent out of this same ceiling
#: and its size cannot be predicted from anything the operator configures**:
#: measured worst cases are 2 177 reasoning tokens at ``low`` on a five-note
#: synthesis, 1 174 at ``high`` on a one-line query rewrite, and 1 277 total
#: output for the single word "ping". Repeated identical calls varied 26x. So
#: the ceiling is sized against the observed worst case (~2 200) with room for a
#: full answer beneath it, not against the median.
#:
#: It costs nothing where it is large: DeepSeek's own maximum output is 384 000
#: tokens. Against a small *shared* window — ollama's 4 096, where prompt and
#: answer come out of one KV cache — it would have eaten the whole thing, which
#: is why :data:`nodum.llm.OUTPUT_RESERVATION_FRACTION` caps what is actually
#: reserved and sent at a share of the window. On ollama this number therefore
#: lands at 2 048, which is the value ``docs/decisions.md`` records as the
#: measured cure for ``qwen3:8b`` answering with an empty body.
DEFAULT_MAX_OUTPUT_TOKENS = 4096

#: What :attr:`LLMReport.stop_switch` says for a run wired to a cycle's row:
#: this run reads ``0015``'s stamps and obeys them.
STOP_SWITCH_ARMED = "armed"

#: What it says for a run that has no cycle — :func:`for_request`'s posture.
#: A journal entry must never imply a switch that was not there, and "no stop
#: was requested" and "there was no stop to request" are different facts: a
#: human-initiated request has no ``cycles`` row for anybody to stamp, and is
#: bounded instead by its own ceilings and by the human who can close the tab.
#:
#: It replaces a third value, ``STOP_SWITCH_PENDING`` ("cycles.stop_requested is
#: not in this database's schema yet"), which was stale in three ways at once by
#: the time ``0015`` landed: the column is ``stop_requested_at``, the gate under
#: it keyed on the *service function* rather than on the column, and no build
#: carrying this module can reach the branch that produced it. Whether a
#: *database* can store a stop is a real question with two answers and it is
#: :func:`nodum.db._cycle_stop_problems`'s, asked at ``init_db`` where it can be
#: repaired — never a string in a report written after the write already failed.
STOP_SWITCH_NONE = "none: this run is not a cycle and has no stop row"


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

    **A default belongs on a field whose zero means "unknown", and nowhere
    else.** :attr:`reasoning_tokens` keeps one because ``0`` there really is
    ambiguous — the model did not think, or the wire did not say, and the two
    are indistinguishable. :attr:`latency_ms` has none: it is measured around
    every call this type is built from, ``Completion.latency_ms`` is itself
    required, and ``0`` on a report would be the claim that a call took no time.
    """

    text: str
    generated_by: GeneratedBy
    prompt_tokens: int
    output_tokens: int
    #: The share of :attr:`output_tokens` spent thinking rather than writing.
    #: A caller sizing its own ceiling needs this and not the total: what has to
    #: fit is the *content*, and the thinking is what pushes it out.
    reasoning_tokens: int = 0
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
    """What one job's share of the budget bought.

    ``calls`` is every call that reached the wire and ``failed_calls`` is the
    subset of them that **produced no usable result** — a timeout, an
    unreachable server, an output cut at its ceiling, a prompt the server
    truncated. The last two are charged as well as counted, because the tokens
    were really spent; ``calls - failed_calls`` is the work that came back
    usable.
    """

    job: str
    budget_tokens: int
    calls: int
    failed_calls: int
    prompt_tokens: int
    output_tokens: int
    #: The share of ``output_tokens`` this job spent thinking rather than
    #: writing. Not additional to it — see :attr:`Budget.spent_reasoning_tokens`.
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    #: Whether a spending ceiling stopped this job. **Required**, like
    #: :attr:`LLMReport.exhausted` and for the same reason: ``False`` is a claim
    #: about the run, not an absence, and the only construction site always
    #: knows the answer.
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

    :attr:`failed_calls` counts **calls that produced no usable result**, which
    includes the ones that were paid for: a body cut at its output ceiling and a
    prompt the server truncated are charged *and* counted here, because a night
    of three discarded answers reporting three successes is the report telling
    the opposite of what happened. :attr:`skipped` is the other half — items
    nothing was even attempted for, whether a ceiling, a stop or a prompt that
    could not fit refused them.
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
    #: Thinking tokens, a **share of** :attr:`output_tokens` and not an addition
    #: to :attr:`total_tokens`. A night that spent most of its output allowance
    #: reasoning is a night one bad sample away from a run of ``length``
    #: finishes, which on this interface are no result at all — and in
    #: ``output_tokens`` alone that night is indistinguishable from a productive
    #: one.
    reasoning_tokens: int = 0
    #: Prompt tokens served from the provider's prefix cache, and the ones that
    #: were not. Reported because they are priced ~50x apart, so a total in
    #: tokens is not a total in money without them.
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    #: The four fields below carry **no default, deliberately**, and the rule
    #: that separates them from the counters above is what a zero would *mean*.
    #: ``reasoning_tokens`` and the cache counters are wire-optional and ``0``
    #: there is honestly "the provider did not say". These four are answers this
    #: run always has, and their zero values are assertions: that the night took
    #: no time, that no ceiling stopped it, that nobody asked it to stop, and —
    #: worst — that there was no stop row for anybody to stamp.
    #:
    #: :data:`STOP_SWITCH_NONE` is the one that made this worth changing. It is
    #: an *affirmative claim* about a run's posture, and as a default it is the
    #: claim a **cycle** run must never make: a future construction site that
    #: forgot the field would file a report saying the kill switch was not there
    #: on precisely the runs where it was. Pydantic imposes no
    #: defaults-after-defaults ordering, so nothing but habit ever asked for
    #: these.
    elapsed_seconds: float
    exhausted: bool
    stopped: bool
    stop_switch: str
    #: Which ``response_format`` this run's provider uses, and the reasoning
    #: level it asks for. Both are properties of the *install* rather than of the
    #: run, and both change what the caller may believe about a body it gets
    #: back, so they ride with the cost rather than being discoverable only from
    #: a provider object :class:`AgentRun` deliberately does not hand out.
    structured_mode: str | None = None
    thinking: str | None = None
    thinking_applied: bool = True
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

    **The clock starts at the first call, not at construction** — see
    :meth:`start`.

    Attributes:
        name: What this budget is for — a job name, or the run's purpose.
        tokens: The token ceiling. ``0`` means off.
        seconds: The wall-clock ceiling, measured from :attr:`started`.
        parent: The budget this one is a share of, if any.
        started: When the wall clock started, or ``None`` while it has not.
    """

    name: str
    tokens: int
    seconds: float
    parent: Budget | None = None
    spent_prompt_tokens: int = 0
    spent_output_tokens: int = 0
    #: Thinking tokens, accumulated **beside** the spend rather than inside it.
    #: They are already counted in :attr:`spent_output_tokens` — a reasoning
    #: model bills them as ``completion_tokens`` — so a ledger that added them
    #: would report a night as costing up to twice the bill. Kept because the
    #: split is the difference between "the output ceiling is generous" and "the
    #: output ceiling is one excursion from returning nothing".
    spent_reasoning_tokens: int = 0
    #: Prompt tokens the provider's prefix cache served, priced ~50x below a
    #: miss. Without them a token total cannot be turned into a cost at all.
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    calls: int = 0
    failed_calls: int = 0
    started: float | None = None
    #: Shares this budget has already handed out, by job name. Kept because the
    #: over-commit rule is about *every* share of this budget and not about the
    #: ones that happened to arrive in the same call — see :meth:`split`.
    _shares: dict[str, float] = field(default_factory=dict)

    @property
    def spent_tokens(self) -> int:
        """What has been billed here, in the unit the ceiling is denominated in.

        Prompt and output are metered apart because they are priced apart
        everywhere a bill exists, and because they fail differently: a run that
        spent its budget on prompts is one sending too much context, and a run
        that spent it on output is one asking for too much text.
        """
        return self.spent_prompt_tokens + self.spent_output_tokens

    @property
    def declared_shares(self) -> Mapping[str, float]:
        """Every share handed out of this budget so far, by job name."""
        return dict(self._shares)

    def split(self, shares: Mapping[str, float]) -> dict[str, Budget]:
        """Derive per-job budgets from a declared split of this one (B1).

        **The rule is about every share of this budget, not about the ones in
        this call.** ``AgentRun.job`` is one ``split`` per job, so a guard
        reading only its own argument let three jobs at 0.6 each hold 600 tokens
        of a 1 000-token cycle — 180 % of the budget, reported as such in
        :attr:`LLMReport.per_job`. Spending stayed bounded (the remainder is the
        minimum down the chain); it was the report that lied, about the one
        number a human checks a night against. So the shares accumulate here.

        A **repeated name is refused** for a related reason: ``AgentRun._jobs``
        is keyed by name, so a second budget under one name displaced the first
        and took its calls and its tokens out of the report — a job that ran and
        cost money, reported as never having had a turn.

        Args:
            shares: Job name → the fraction of this budget it may spend. Every
                fraction must be positive, no name may already have a share, and
                the shares plus every share already handed out must not sum
                above 1. Summing *below* 1 is fine and is how a run holds some
                back.

        Returns:
            One budget per name, in the order given.

        Raises:
            ValueError: If a share is not positive, a name is already taken, or
                the shares over-commit this budget.
        """
        for name, share in shares.items():
            if share <= 0:
                raise ValueError(f"share for {name!r} must be positive, got {share}")
            if name in self._shares:
                raise ValueError(
                    f"the {self.name} budget already has a share for {name!r} "
                    f"({self._shares[name]:g}); a second one would replace it and take the "
                    f"first job's recorded spend out of the report"
                )
        total = sum(self._shares.values()) + sum(shares.values())
        # A float split of thirds sums to 0.9999999999999999 or 1.0000000000000002
        # depending on the order; the tolerance is for that and nothing wider.
        if total > 1.0 + 1e-9:
            raise ValueError(
                f"job shares sum to {total:g}, which over-commits the {self.name} budget"
            )
        jobs = {
            name: Budget(
                name=name,
                tokens=int(self.tokens * share),
                seconds=self.seconds,
                parent=self,
                started=self.started,
            )
            for name, share in shares.items()
        }
        self._shares.update(shares)
        return jobs

    @property
    def chain(self) -> list[Budget]:
        """This budget and every budget it is a share of, innermost first."""
        budgets: list[Budget] = []
        current: Budget | None = self
        while current is not None:
            budgets.append(current)
            current = current.parent
        return budgets

    def start(self) -> None:
        """Start the wall clock for this budget and every ancestor, once.

        The clock belongs to the *model use*, not to the object. ``for_cycle``
        is built when the cycle opens and the LLM jobs run last, so a clock
        started at construction charged the five deterministic jobs' minutes to
        the LLM's ceiling: a report saying the model spent time it never had,
        and — on a slow graph — a ceiling largely spent before the first prompt
        was built. It reaches the whole chain because a child copies its
        parent's :attr:`started` when it is split off, which for a job declared
        before the first call is ``None``; one clock for the nesting means one
        start for the nesting.

        Idempotent: a budget already running keeps the instant it started, so
        the second call of a run does not reset the night.
        """
        now = time.monotonic()
        for budget in self.chain:
            if budget.started is None:
                budget.started = now

    @property
    def elapsed_seconds(self) -> float:
        """Wall clock since the first call, or 0 while none has been made."""
        if self.started is None:
            return 0.0
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
            # Beside the spend, never added to it: already inside output_tokens.
            budget.spent_reasoning_tokens += completion.reasoning_tokens
            budget.cache_hit_tokens += completion.cache_hit_tokens
            budget.cache_miss_tokens += completion.cache_miss_tokens
            budget.calls += 1

    def note_failure(self) -> None:
        """Record a call that produced no usable result.

        Both kinds count: the call that never came back (a timeout, an
        unreachable server — nothing to bill) and the call that came back and
        was **thrown away** (a ``length`` finish, a prompt the server truncated
        — charged, because the tokens were really spent). Counting only the
        first made a night of three truncated answers report ``calls 3,
        failed_calls 0``, which is a night of three successes.
        """
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

    The read is :func:`nodum.service.stop_requested`, called on every check.
    That function is deliberately **not** human-only, unlike ``get_cycle`` and
    ``list_cycles``: it is one boolean about the caller's own run, disclosing no
    node, space or count, and a runner that cannot ask whether it was told to
    stop cannot obey. What bounds it instead is **the rule that would admit the
    caller to run this cycle's territory** — the grant that resolves a scoped
    cycle's scope, or ``edit`` somewhere for an unscoped one. Caller-relative
    rather than run-relative because ``cycles`` records who *asked* and never
    who is *running*: it therefore also admits an agent granted on the scope
    that has no part in the run. It was the authority to *close* the cycle until
    that rule was found to refuse this very call on a scoped night the gardener
    is entitled to run; the whole argument, both triggers and the width, is in
    :func:`nodum.service._may_watch_a_cycle`. A principal outside the run is
    answered with the not-found refusal an unknown id gets, word for word, so
    that **this** read is not an existence oracle over cycle ids —
    ``close_cycle`` still tells the two refusals apart, deliberately, for the
    reason :func:`nodum.service.stop_requested` gives.

    This raises nothing itself; :meth:`AgentRun.check_stop` is what turns a
    ``True`` into :class:`CycleStopped`, and :meth:`AgentRun.chat` calls it
    before every provider call.

    Args:
        cycle_id: The cycle to watch.
        principal: Who is asking — the principal the run acts as.
        path: Explicit database path.

    Returns:
        A callable answering ``True`` once a stop has been recorded.
    """

    def check() -> bool:
        return bool(service.stop_requested(cycle_id, principal=principal, path=path))

    return check


def _positive_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read a whole number of at least ``minimum`` from the environment.

    An unparseable value falls back and does **not** raise: the scheduler's
    precedent is that a server refusing to boot over a stray character in an
    optional setting is worse than one that says what it skipped — and here the
    fallback for the cycle budget is 0, which is *off*, so a typo cannot
    accidentally authorise spending.

    ``minimum`` is what separates a *decision* from a *misconfiguration*, and
    the two live behind the same reader. 0 on a budget means off, which is a
    decision and the shipped default. 0 on the output ceiling is nothing
    anybody chose: it reached the provider as ``ValueError: max_output_tokens
    must be at least 1``, which the HTTP surface renders as a **400** — the
    client-error voice — on every ``POST /api/ask``, about a request that was
    perfectly well formed.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _positive_float(name: str, default: float) -> float:
    """Read a positive, **finite** number of seconds from the environment.

    ``float()`` reads ``inf``, ``Infinity`` and ``1e999`` happily, and neither
    of the two serialisers this number reaches can carry the result:
    ``cycles.report`` and every HTTP envelope are ``json.dumps`` with
    ``allow_nan`` at its default, which writes a bare ``Infinity`` token that
    is not JSON. Measured: ``NODUM_LLM_REQUEST_SECONDS=inf`` gave ``POST
    /api/ask`` a 200 carrying ``"budget_seconds": Infinity``, and the browser's
    ``JSON.parse`` threw on it. ``nan`` is worse in a quieter way — every
    comparison against it is false, so ``remaining_seconds <= 0`` never fires
    and the wall-clock ceiling stops existing without saying so.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


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
        stop_switch: str = STOP_SWITCH_NONE,
        max_output_tokens: int | None = None,
        call_timeout: float | None = None,
        budget_env: str = ENV_CYCLE_BUDGET,
    ) -> None:
        self.principal = principal
        self.purpose = purpose
        self.budget = budget
        self.stop = stop
        self.stop_switch = stop_switch
        #: Which environment variable funds *this* run, so a refusal names the
        #: one a human can act on: a request run reads
        #: :data:`ENV_REQUEST_BUDGET` and is not affected by the cycle variable
        #: at all.
        self.budget_env = budget_env
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else _positive_int(ENV_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS, minimum=1)
        )
        self.call_timeout = (
            call_timeout
            if call_timeout is not None
            else _positive_float(ENV_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT)
        )
        # Private, and that is the point (P3). A caller handed the provider
        # object makes a call no budget, stop check or report can see, and the
        # import rail cannot notice because holding an object imports nothing:
        # demonstrated at `run.provider.chat(...)` with the budget at 0 and a
        # stop firing, reporting `calls 0, total_tokens 0, stopped True`. What a
        # caller legitimately needs is two numbers, and they are below.
        self._provider = llm.get_provider()
        self.unavailable_reason = llm.unavailable_reason()
        #: Why a configured ``NODUM_LLM_API_KEY`` is not being sent, or ``None``.
        #: Read here rather than from the provider for the reason every other
        #: posture field is (P3): a caller that had to hold the provider to see
        #: it could go on to call it. It is not a failure — the run works, the
        #: local default needs no key — so it rides beside the configuration in
        #: ``nodum llm status`` rather than in ``unavailable_reason``.
        self.api_key_withheld = llm.key_withheld_reason()
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
        return self._provider is not None

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
        """The provider's model id, or ``None`` with no provider configured."""
        return self._provider.model_id if self._provider is not None else None

    @property
    def provider_id(self) -> str | None:
        """The provider's identifier, or ``None`` with no provider configured."""
        return self._provider.provider_id if self._provider is not None else None

    @property
    def context_tokens(self) -> int | None:
        """The window a prompt will be refused against, or ``None`` with no provider.

        One of the two things a caller assembling a prompt needs from the
        provider, and the reason it is answered *here*: a caller that fetched
        the provider to read this number could go on to call it.
        """
        return self._provider.context_tokens if self._provider is not None else None

    @property
    def structured_mode(self) -> str | None:
        """Which ``response_format`` a schema will be sent under, or ``None``.

        Answered here for the same reason :attr:`context_tokens` is: a caller
        that had to fetch the provider to read it could go on to call it (P3).
        And it has to be readable, because a provider that fell back to
        ``json_object`` enforces the schema's *envelope* and none of its
        *contents* — a ``pattern`` that was a constraint under ``json_schema`` is
        a sentence in the prompt under the fallback, and a caller trusting the
        pattern needs to know which it got.
        """
        return self._provider.structured_mode if self._provider is not None else None

    @property
    def thinking(self) -> str | None:
        """The reasoning level this run's provider asks for, or ``None``."""
        return self._provider.thinking if self._provider is not None else None

    @property
    def thinking_applied(self) -> bool:
        """Whether that level actually reaches the endpoint.

        ``False`` is a configured knob doing nothing — ollama answers 400 to
        every graded level, so one is withheld there and the model runs at its
        own default. Silence about that would be a setting a human can read back
        from their own environment and cannot see the effect of.
        """
        return self._provider.thinking_applied if self._provider is not None else True

    def estimate_prompt_tokens(
        self, messages: Sequence[Message], *, schema: dict[str, Any] | None = None
    ) -> int:
        """The provider's own over-count of what these messages will cost.

        The other thing a prompt builder needs — and it must be the *provider's*
        estimate, because what gets fitted has to be exactly what would be
        refused; a second estimator is free to disagree with the one that
        decides.

        Raises:
            ProviderUnavailable: If none is configured. There is no honest
                number to return, and the absence is reported the same way every
                other reach for a missing provider here reports it.
        """
        if self._provider is None:
            raise ProviderUnavailable(self.unavailable_reason or "no LLM provider configured")
        return self._provider.estimate_prompt_tokens(messages, schema=schema)

    def output_reservation(self, max_output_tokens: int | None = None) -> int:
        """How much of the window a call will really keep back for its answer.

        **The one place that arithmetic lives.** A caller fitting a prompt needs
        the same number the provider will refuse against, and
        ``window - max_output_tokens`` computed independently by the caller is a
        second rule free to disagree — which it did the moment the default
        ceiling was sized for a reasoning model: 4 096 subtracted from a
        4 096-token window left a prompt exactly no room, so every ``/ask``
        refused with "the question alone fills this model's context window" on a
        provider that would have answered it.

        Returns 0 with no provider, which is the same "there is no honest
        number" posture :attr:`context_tokens` takes by answering ``None``.
        """
        ceiling = self.max_output_tokens if max_output_tokens is None else max_output_tokens
        if self._provider is None:
            return 0
        return self._provider.output_reservation(ceiling)

    def generated_by(self, prompt_version_hash: str) -> GeneratedBy:
        """The provenance object for a write this run's text caused (A1)."""
        if self._provider is None:
            raise ProviderUnavailable(self.unavailable_reason or "no LLM provider configured")
        return GeneratedBy(
            provider=self._provider.provider_id,
            model_id=self._provider.model_id,
            prompt_version=prompt_version_hash,
        )

    # ── Budgets ──────────────────────────────────────────────────────────────

    def split(self, shares: Mapping[str, float]) -> dict[str, Budget]:
        """Declare the per-job split of this run's budget (B1).

        The returned budgets are remembered, so :meth:`report` lists every job
        the run declared — including one that never made a call, which is how a
        reader tells "this job had no work" from "this job never got a turn".

        Every share this run hands out is measured against the same ceiling,
        across calls — see :meth:`Budget.split`, which is where the accumulation
        lives so that a caller splitting ``run.budget`` directly is held to it
        too. A name already declared is refused rather than replaced.

        Raises:
            ValueError: If a share is not positive, a name is already declared,
                or the shares over-commit this run's budget.
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
        thinking: str | None = None,
    ) -> Generation:
        """Make one provider call, metered, bounded and attributable (P3).

        The order is the whole design. A stop is checked first, because a run
        told to stop must not spend. The call's **worst case** — the prompt's
        over-counted estimate plus the whole output reservation — is measured
        against the budget next, and refused before anything is sent, so the
        budget is never overrun by a call whose cost was only discovered
        afterwards. The per-call timeout is then lowered to whatever is left of
        the run's clock, because a ceiling checked once and never again is not a
        ceiling. The provider refuses an over-long prompt on its own window
        last. Only after all four does a request go out; whatever comes back is
        charged, *including* a failure, and a body the server truncated or cut
        at its output ceiling is discarded rather than returned.

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
            thinking: Per-call-site reasoning level, overriding the global
                :data:`~nodum.llm.ENV_THINKING` one. ``None`` leaves the global
                default in force, which is what almost every call site wants.
                The exceptions are measured rather than guessed: a reachability
                probe returns an **empty body** at any graded level because
                thinking eats its whole ceiling, and a query rewrite is an
                8-term keyword expansion whose thinking varied 26x across twelve
                samples for no change in the terms.

        Returns:
            A whole :class:`Generation`.

        Raises:
            CycleStopped: If a human asked this run to stop.
            BudgetExhausted: If a ceiling was reached. Nothing was spent.
            ProviderUnavailable: If no provider is configured, or the one
                configured could not be reached.
            PromptTooLong: If the prompt does not fit the model's window.
                Nothing was spent — this is the refusal that exists so an
                over-long prompt is never silently answered from a prefix — and
                a named ``item_id`` is recorded as a skip, because something was
                left unexamined even though nothing was billed.
            ContextOverflow: If the server did not read the whole prompt after
                all, by either signal (the report at the configured ceiling, or
                below what the prompt's bytes can cost). Charged and counted as
                a failed call.
            OutputTruncated: If the output ceiling bit. Charged and counted as a
                failed call.
        """
        ledger = job if job is not None else self.budget
        output_ceiling = (
            max_output_tokens if max_output_tokens is not None else self.max_output_tokens
        )
        call_timeout = timeout if timeout is not None else self.call_timeout

        self.check_stop(job=job, item_id=item_id)
        provider = self._provider
        if provider is None:
            raise ProviderUnavailable(self.unavailable_reason or "no LLM provider configured")

        ledger.start()
        # Both halves of "what this call can cost" are the *provider's* numbers,
        # and both used to be somebody else's:
        #
        # `schema=` was passed by no caller. Under `json_object` the provider
        # states the schema as a system message, so a schema the caller will
        # pass costs prompt tokens on the wire — 330 of them for
        # `answers.ASK_SCHEMA`, measured — that a bare estimate cannot see. At
        # `NODUM_LLM_CONTEXT_TOKENS=8192` the fitter in `nodum.answers` sized a
        # prompt at 4 068 against a 4 096 ceiling and `chat` then refused the
        # same prompt at 4 398: `/ask` refusing a prompt its own fitter had just
        # built to fit.
        #
        # And the reservation is what the provider will really send as
        # `max_tokens`, which is the ceiling capped at a share of the window —
        # 2 048 of a 4 096-token one. Charging the uncapped 4 096 made the
        # budget refuse up to 2 048 tokens per call early, on a worst case that
        # could not happen.
        needed = provider.estimate_prompt_tokens(
            messages, schema=schema
        ) + provider.output_reservation(output_ceiling)
        self._require_budget(ledger, needed, job=job, item_id=item_id)

        # Per-call ⊂ per-job ⊂ per-cycle holds for the clock too, and it did not:
        # the wall clock is checked before the call and never again, so with the
        # shipped 120 s call timeout a run with a 2 s ceiling left reported an
        # elapsed 3.0 — and a night overran its ceiling by up to two minutes.
        # `_require_budget` has just refused a run with no time left, so what is
        # left here is positive.
        call_timeout = min(call_timeout, ledger.remaining_seconds)

        try:
            completion = provider.chat(
                messages,
                schema=schema,
                max_output_tokens=output_ceiling,
                timeout=call_timeout,
                thinking=thinking,
            )
        except PromptTooLong as failure:
            # Refused before the wire: no tokens spent, nothing to charge, and
            # not a failure of the provider's. But an item *was* left
            # unexamined, and a run that refused three of them used to report
            # `calls 0, failed_calls 0, skipped []` — byte-identical to a night
            # on which the job found nothing to do.
            if item_id is not None:
                self.record_skip(
                    job=job,
                    item_id=item_id,
                    reason=f"the prompt does not fit the model's window: {failure}",
                )
            raise
        except ProviderUnavailable:
            ledger.note_failure()
            raise

        ledger.charge(completion)
        if completion.context_filled or completion.prompt_truncated:
            ledger.note_failure()
            raise ContextOverflow(
                f"the provider read {completion.prompt_tokens} prompt tokens of a prompt "
                f"estimated at {completion.prompt_estimate} against a configured "
                f"{completion.context_tokens}-token window, so part of it was never read "
                f"and the answer is from a prefix. The call is charged because it was "
                f"really spent. Set {llm.ENV_CONTEXT_TOKENS} to the window the server "
                f"actually serves (for ollama that is num_ctx / OLLAMA_CONTEXT_LENGTH, not "
                f"the model card), or send less context",
                completion,
            )
        if completion.output_truncated:
            ledger.note_failure()
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
            reasoning_tokens=completion.reasoning_tokens,
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
            reasoning_tokens=self.budget.spent_reasoning_tokens,
            cache_hit_tokens=self.budget.cache_hit_tokens,
            cache_miss_tokens=self.budget.cache_miss_tokens,
            elapsed_seconds=round(self.budget.elapsed_seconds, 3),
            exhausted=bool(self._ceilings_hit),
            stopped=self.stopped,
            stop_switch=self.stop_switch,
            structured_mode=self.structured_mode,
            thinking=self.thinking,
            thinking_applied=self.thinking_applied,
            per_job=[
                JobCost(
                    job=budget.name,
                    budget_tokens=budget.tokens,
                    calls=budget.calls,
                    failed_calls=budget.failed_calls,
                    prompt_tokens=budget.spent_prompt_tokens,
                    output_tokens=budget.spent_output_tokens,
                    reasoning_tokens=budget.spent_reasoning_tokens,
                    cache_hit_tokens=budget.cache_hit_tokens,
                    cache_miss_tokens=budget.cache_miss_tokens,
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
            # Two different zeros, and telling a human to set a variable they
            # have already set is worse than saying nothing. A *run* with no
            # budget is the switch being off (K2 level 1) and names the variable
            # that funds this posture — `for_request` reads
            # `NODUM_LLM_REQUEST_BUDGET` and the cycle variable does nothing for
            # it. A funded run whose job share rounded to zero is a split
            # problem on a run whose report says `enabled: true`.
            if self.budget.tokens > 0:
                message = (
                    f"the share of the {self.budget.name!r} budget allotted to "
                    f"{ledger.name!r} rounds to 0 tokens, so no model call is made. Give "
                    f"the job a larger share, or raise the run's {self.budget.tokens}-token "
                    f"budget"
                )
            else:
                message = (
                    f"the LLM budget for {ledger.name!r} is 0, so no model call is made "
                    f"(set {self.budget_env} to a token budget)"
                )
            self._refuse(
                ledger,
                job=job,
                item_id=item_id,
                kind="off",
                remaining=0,
                needed=needed,
                message=message,
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
        stop_switch=STOP_SWITCH_ARMED,
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
        budget_env=ENV_REQUEST_BUDGET,
    )
