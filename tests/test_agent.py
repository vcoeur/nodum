"""The one door to the model: accounting, budgets, and the kill switch.

Everything a provider call has to be — metered, bounded, attributable and
stoppable — is a property of :class:`nodum.agent.AgentRun`, because P3 puts
every such call through it. So this file is where those properties are pinned.

No test asserts on model output: a fake provider returns scripted completions,
exactly as ``tests/conftest.py``'s ``HashEmbedder`` stands in for a real
embedding model.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any

import pytest

from nodum import agent, llm
from nodum.principal import Principal

# ── Fakes ─────────────────────────────────────────────────────────────────────


def _completion(
    *,
    text: str = "answer",
    prompt_tokens: int = 100,
    output_tokens: int = 20,
    finish_reason: str = "stop",
    context_tokens: int = 4096,
) -> llm.Completion:
    return llm.Completion(
        text=text,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        model_id="fake-model",
        provider_id="fake://provider",
        context_tokens=context_tokens,
        latency_ms=7,
    )


class FakeProvider:
    """A provider that records what it was asked and replays what it was given."""

    provider_id = "fake://provider"
    model_id = "fake-model"
    context_tokens = 4096

    def __init__(self, *answers: llm.Completion | BaseException) -> None:
        self.answers = list(answers) or [_completion()]
        self.calls: list[dict[str, Any]] = []

    def estimate_prompt_tokens(self, messages) -> int:
        return llm.estimate_prompt_tokens(messages)

    def chat(self, messages, *, schema=None, max_output_tokens, timeout) -> llm.Completion:
        self.calls.append(
            {
                "messages": list(messages),
                "schema": schema,
                "max_output_tokens": max_output_tokens,
                "timeout": timeout,
            }
        )
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer


HUMAN = Principal(kind="human", id="alice")
GARDENER = Principal(kind="internal", id="builtin-gardener", grants={"main": "edit"})
PROMPT = [llm.Message(role="user", content="hello")]
VERSION = agent.prompt_version("a template")


@pytest.fixture(autouse=True)
def _clean_provider(monkeypatch):
    """No provider resolves from the developer's environment, ever."""
    for name in (llm.ENV_MODEL, llm.ENV_BASE_URL, llm.ENV_API_KEY, llm.ENV_CONTEXT_TOKENS):
        monkeypatch.delenv(name, raising=False)
    for name in (
        agent.ENV_CYCLE_BUDGET,
        agent.ENV_CYCLE_SECONDS,
        agent.ENV_REQUEST_BUDGET,
        agent.ENV_REQUEST_SECONDS,
        agent.ENV_CALL_TIMEOUT,
        agent.ENV_MAX_OUTPUT_TOKENS,
    ):
        monkeypatch.delenv(name, raising=False)
    llm.set_provider(None, reason="test default: no LLM provider")
    yield
    llm.reset_provider()


def _run(
    *,
    provider: FakeProvider | None = None,
    tokens: int = 100_000,
    seconds: float = 600.0,
    started: float | None = None,
    stop: agent.StopCheck | None = None,
    **overrides: Any,
) -> agent.AgentRun:
    if provider is not None:
        llm.set_provider(provider)
    budget = agent.Budget(name="cycle:test", tokens=tokens, seconds=seconds)
    if started is not None:
        budget.started = started
    return agent.AgentRun(
        principal=GARDENER, purpose="cycle:test", budget=budget, stop=stop, **overrides
    )


# ── Budgets nest (B1) ─────────────────────────────────────────────────────────


def test_a_job_budget_is_a_share_of_the_run_budget():
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    jobs = cycle.split({"synthesis": 0.6, "curation": 0.25})
    assert jobs["synthesis"].tokens == 600
    assert jobs["curation"].tokens == 250
    assert jobs["synthesis"].parent is cycle


def test_charging_a_job_charges_the_run_it_is_a_share_of():
    """Per-job alone lets ten jobs cost ten times what anyone agreed to."""
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    jobs = cycle.split({"a": 0.5, "b": 0.5})
    jobs["a"].charge(_completion(prompt_tokens=300, output_tokens=100))
    assert jobs["a"].spent_tokens == 400
    assert cycle.spent_tokens == 400
    assert jobs["b"].spent_tokens == 0
    assert jobs["b"].remaining_tokens == 500, "b's own share is untouched"
    assert cycle.remaining_tokens == 600


def test_a_job_cannot_outspend_the_run_even_inside_its_own_share():
    """The remainder is the *minimum* down the chain, which is what makes the
    nesting a nesting rather than three independent counters.

    Reached by two *separate* splits, each valid on its own — which is the way
    this really happens, since the one-call guard can only see the shares it is
    handed. Without the min, ``b`` would hold a 900-token licence over a cycle
    with 100 left.
    """
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    first = cycle.split({"a": 0.9})["a"]
    second = cycle.split({"b": 0.9})["b"]
    first.charge(_completion(prompt_tokens=900, output_tokens=0))
    assert second.tokens == 900
    assert second.spent_tokens == 0
    assert second.remaining_tokens == 100, "b is bounded by what the cycle has left"


def test_a_split_that_over_commits_the_run_is_refused():
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    with pytest.raises(ValueError, match="over-commits"):
        cycle.split({"a": 0.7, "b": 0.5})


def test_a_split_of_exactly_one_and_of_thirds_is_allowed():
    """Float thirds sum to 1.0000000000000002; the tolerance is for that alone."""
    cycle = agent.Budget(name="cycle", tokens=900, seconds=60.0)
    assert sum(budget.tokens for budget in cycle.split({"a": 0.5, "b": 0.5}).values()) == 900
    thirds = cycle.split({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})
    assert sorted(budget.tokens for budget in thirds.values()) == [300, 300, 300]


@pytest.mark.parametrize("share", [0.0, -0.1])
def test_a_non_positive_share_is_refused(share: float):
    with pytest.raises(ValueError, match="must be positive"):
        agent.Budget(name="cycle", tokens=1000, seconds=60.0).split({"a": share})


def test_a_job_budget_shares_the_run_clock_and_never_holds_an_infinite_ceiling():
    """Wall clock is one clock for the whole nesting, and JSON has no infinity.

    A child with its own ``seconds`` would let a job outlive the night, and a
    child with ``inf`` would put ``Infinity`` in ``cycles.report``, which is not
    JSON at all.
    """
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=30.0)
    cycle.started = time.monotonic() - 25.0
    job = cycle.split({"a": 1.0})["a"]
    assert job.seconds == 30.0
    assert job.started == cycle.started
    assert job.remaining_seconds == pytest.approx(5.0, abs=0.5)


def test_the_wall_clock_exhausts_independently_of_the_tokens():
    """B2: tokens alone do not bound a night — 2 395 prompt tokens cost 47 s."""
    cycle = agent.Budget(name="cycle", tokens=1_000_000, seconds=10.0)
    cycle.started = time.monotonic() - 11.0
    assert cycle.remaining_tokens == 1_000_000
    assert cycle.remaining_seconds < 0
    assert cycle.exhausted is True


# ── The order of operations inside one call ───────────────────────────────────


def test_a_call_that_would_overrun_the_budget_never_reaches_the_provider():
    """B3: refuse the call. Never truncate the prompt to fit."""
    provider = FakeProvider()
    run = _run(provider=provider, tokens=50)
    with pytest.raises(agent.BudgetExhausted) as refusal:
        run.chat(PROMPT, prompt_version=VERSION)
    assert provider.calls == [], "the provider was called despite the refusal"
    assert refusal.value.kind == "tokens"
    assert refusal.value.remaining == 50
    assert refusal.value.needed > 50
    assert run.budget.spent_tokens == 0, "a refusal cost tokens"


def test_the_budget_check_is_the_call_worst_case_not_its_average():
    """The prompt's over-counted estimate *plus the whole output reservation*.

    Checking the prompt alone would let a call whose answer does not fit start
    anyway, and the budget would be overrun by a cost only discovered afterwards.
    """
    provider = FakeProvider()
    estimate = llm.estimate_prompt_tokens(PROMPT)
    run = _run(provider=provider, tokens=estimate + 100)
    with pytest.raises(agent.BudgetExhausted):
        run.chat(PROMPT, prompt_version=VERSION, max_output_tokens=200)
    assert provider.calls == []
    run.chat(PROMPT, prompt_version=VERSION, max_output_tokens=50)
    assert len(provider.calls) == 1


def test_a_refusal_itemises_the_item_it_did_not_examine():
    """The defence against a silently worse cycle is a loud, itemised list."""
    run = _run(provider=FakeProvider(), tokens=10)
    job = run.job("synthesis", share=1.0)
    with pytest.raises(agent.BudgetExhausted):
        run.chat(PROMPT, prompt_version=VERSION, job=job, item_id="node-42")
    report = run.report()
    assert [(item.job, item.id) for item in report.skipped] == [("synthesis", "node-42")]
    assert "10 left" in report.skipped[0].reason


def test_a_refusal_with_no_item_named_records_no_skip():
    """A caller that named nothing gets no invented id in the journal."""
    run = _run(provider=FakeProvider(), tokens=10)
    with pytest.raises(agent.BudgetExhausted):
        run.chat(PROMPT, prompt_version=VERSION)
    assert run.report().skipped == []


def test_a_budget_that_was_never_turned_on_is_refused_as_off_not_as_exhausted():
    """K2 level 1. Nothing was skipped through *spending*, and a journal saying
    otherwise would send a human looking for a night that cost too much."""
    run = _run(provider=FakeProvider(), tokens=0)
    with pytest.raises(agent.BudgetExhausted) as refusal:
        run.chat(PROMPT, prompt_version=VERSION)
    assert refusal.value.kind == "off"
    assert agent.ENV_CYCLE_BUDGET in str(refusal.value)
    report = run.report()
    assert report.enabled is False
    assert report.exhausted is False


def test_a_used_up_wall_clock_refuses_before_the_tokens_are_even_consulted():
    provider = FakeProvider()
    run = _run(provider=provider, tokens=1_000_000, seconds=5.0, started=time.monotonic() - 60.0)
    with pytest.raises(agent.BudgetExhausted) as refusal:
        run.chat(PROMPT, prompt_version=VERSION, item_id="node-7")
    assert refusal.value.kind == "seconds"
    assert provider.calls == []
    assert run.report().skipped[0].id == "node-7"
    assert run.report().exhausted is True


def test_a_completed_call_is_charged_and_returns_its_provenance():
    """A1/A2: the model and the prompt version travel with the write."""
    provider = FakeProvider(_completion(text="a synthesis", prompt_tokens=321, output_tokens=45))
    run = _run(provider=provider)
    generation = run.chat(PROMPT, prompt_version=VERSION, schema={"type": "object"})

    assert generation.text == "a synthesis"
    assert generation.generated_by.model_id == "fake-model"
    assert generation.generated_by.provider == "fake://provider"
    assert generation.generated_by.prompt_version == VERSION
    assert (generation.prompt_tokens, generation.output_tokens) == (321, 45)
    assert generation.latency_ms == 7
    assert run.budget.spent_tokens == 366
    assert run.budget.spent_prompt_tokens == 321
    assert run.budget.spent_output_tokens == 45
    assert run.budget.calls == 1
    assert provider.calls[0]["schema"] == {"type": "object"}


def test_the_per_call_ceilings_reach_the_provider():
    """Per-call ⊂ per-job ⊂ per-cycle: the innermost pair is what the wire gets."""
    provider = FakeProvider()
    run = _run(provider=provider, max_output_tokens=333, call_timeout=44.0)
    run.chat(PROMPT, prompt_version=VERSION)
    assert provider.calls[0]["max_output_tokens"] == 333
    assert provider.calls[0]["timeout"] == 44.0
    run.chat(PROMPT, prompt_version=VERSION, max_output_tokens=8, timeout=1.5)
    assert provider.calls[1]["max_output_tokens"] == 8
    assert provider.calls[1]["timeout"] == 1.5


# ── A failed call is still a call ─────────────────────────────────────────────


def test_an_output_ceiling_is_a_failed_call_and_the_text_never_comes_back():
    """B3, measured: that body is cut mid-string and does not parse. It is no
    result rather than a partial one — and it still counts against the budget."""
    provider = FakeProvider(
        _completion(
            text='{"title": "Kaf', prompt_tokens=90, output_tokens=8, finish_reason="length"
        )
    )
    run = _run(provider=provider)
    with pytest.raises(llm.OutputTruncated) as failure:
        run.chat(PROMPT, prompt_version=VERSION)
    assert failure.value.completion.text == '{"title": "Kaf'
    assert run.budget.spent_tokens == 98, "a truncated call was not charged"
    assert run.budget.calls == 1


def test_a_filled_context_is_a_failed_call_and_the_text_never_comes_back():
    """The second line of defence, for a window configured wider than the real
    one: the prompt passed the refusal, the server dropped what did not fit, and
    the only signal is ``prompt_tokens`` at the ceiling."""
    provider = FakeProvider(_completion(prompt_tokens=4096, output_tokens=9, context_tokens=4096))
    run = _run(provider=provider)
    with pytest.raises(llm.ContextOverflow) as failure:
        run.chat(PROMPT, prompt_version=VERSION)
    assert failure.value.completion.prompt_tokens == 4096
    assert run.budget.spent_tokens == 4105, "a truncated-context call was not charged"


def test_a_provider_failure_costs_no_tokens_and_is_still_counted():
    """A night of timeouts must not read as a night with no work."""
    provider = FakeProvider(llm.ProviderTimeout("did not answer within 120s"))
    run = _run(provider=provider)
    with pytest.raises(llm.ProviderTimeout):
        run.chat(PROMPT, prompt_version=VERSION)
    assert run.budget.spent_tokens == 0
    assert run.budget.calls == 0
    assert run.budget.failed_calls == 1
    assert run.report().failed_calls == 1


def test_a_prompt_too_long_costs_nothing_at_all():
    """The refusal that exists so an over-long prompt is never answered from a
    prefix. Nothing was sent, so nothing is billed and nothing is a *failure*
    of the provider's."""
    provider = FakeProvider(llm.PromptTooLong("prompt is about 9000 tokens"))
    run = _run(provider=provider)
    with pytest.raises(llm.PromptTooLong):
        run.chat(PROMPT, prompt_version=VERSION)
    assert run.budget.spent_tokens == 0
    assert run.budget.calls == 0
    assert run.budget.failed_calls == 0


def test_no_provider_is_reported_as_an_absence_and_not_as_a_budget_problem():
    run = _run()
    assert run.available is False
    assert run.enabled is False
    with pytest.raises(llm.ProviderUnavailable, match="test default"):
        run.chat(PROMPT, prompt_version=VERSION)


# ── The kill switch (K1–K3) ───────────────────────────────────────────────────


def test_a_stop_is_checked_before_every_provider_call():
    """K3's third checkpoint. Worst-case latency is one provider call."""
    provider = FakeProvider()
    stops = [False, True]
    run = _run(provider=provider, stop=lambda: stops.pop(0))
    run.chat(PROMPT, prompt_version=VERSION)
    assert len(provider.calls) == 1
    with pytest.raises(agent.CycleStopped):
        run.chat(PROMPT, prompt_version=VERSION)
    assert len(provider.calls) == 1, "the provider was called after a stop was requested"
    assert run.report().stopped is True


def test_the_order_inside_a_call_is_stop_then_budget_then_provider():
    """ "Before *every* call" as a property of the code, not of two examples.

    The behavioural tests above drive the door twice each; this reads
    :meth:`AgentRun.chat` itself and pins the sequence, because the ordering is
    the whole design. A stop is checked first, because a run told to stop must
    not spend. The budget is checked next and refuses before anything is sent,
    so a call whose cost is only discovered afterwards can never overrun it.
    Only then does a request go out.
    """
    tree = ast.parse(Path(agent.__file__).read_text(encoding="utf-8"))
    body = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "chat"
    )
    order: dict[str, int] = {}
    for node in ast.walk(body):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            order.setdefault(node.func.attr, node.lineno)
    assert {"check_stop", "_require_budget", "chat"} <= set(order), (
        f"chat() no longer performs all three steps: {sorted(order)}"
    )
    assert order["check_stop"] < order["_require_budget"] < order["chat"], (
        f"the order inside chat() has moved: {order}"
    )


def test_the_stop_flag_is_read_every_time_and_never_cached():
    """A check answered from a value read at the top of the run would be a kill
    switch that cannot be hit after the run starts, which is the only time
    anybody hits one."""
    reads = []

    def stop() -> bool:
        reads.append(len(reads))
        return False

    run = _run(provider=FakeProvider(), stop=stop)
    run.chat(PROMPT, prompt_version=VERSION)
    run.chat(PROMPT, prompt_version=VERSION)
    run.check_stop()
    assert len(reads) == 3


def test_a_stop_between_items_records_what_it_cost():
    """The runner's own checkpoints — between jobs and between items — go
    through the same call, so a stop itemises like a budget refusal does."""
    run = _run(provider=FakeProvider(), stop=lambda: True)
    job = run.job("curation", share=1.0)
    with pytest.raises(agent.CycleStopped):
        run.check_stop(job=job, item_id="node-9")
    report = run.report()
    assert [(item.job, item.id) for item in report.skipped] == [("curation", "node-9")]
    assert report.stopped is True


def test_a_run_with_no_stop_check_never_stops():
    run = _run(provider=FakeProvider())
    run.check_stop()
    assert run.report().stopped is False


def test_the_stop_switch_is_not_wired_until_the_migration_lands():
    """The runtime half is written; the column and its read are a later wave's.

    A journal entry must not imply a switch that was not wired: "no stop was
    requested" and "no stop *could* be requested" are different facts.
    """
    check = agent.cycle_stop_check("cycle-1", principal=GARDENER)
    if agent.stop_switch_available():
        pytest.skip("migration 0015 has landed; the gated branch no longer exists")
    assert check() is False
    run = agent.for_cycle(cycle_id="cycle-1", principal=GARDENER)
    assert run.report().stop_switch == agent.STOP_SWITCH_PENDING


def test_the_stop_check_reads_the_service_the_moment_that_read_exists(monkeypatch):
    """The gate's other branch, which the shipped tree cannot reach today.

    A fixture that cannot express the behaviour is not coverage of it — so the
    service read is installed here under the name :data:`nodum.agent.
    SERVICE_STOP_READ` and the check is driven through it, which is exactly
    what migration ``0015``'s wave will make real.
    """
    asked: list[tuple[str, Principal, Any]] = []

    def stop_requested(cycle_id: str, *, principal: Principal, path: Any = None) -> bool:
        asked.append((cycle_id, principal, path))
        return len(asked) > 1

    monkeypatch.setattr(agent.service, agent.SERVICE_STOP_READ, stop_requested, raising=False)
    assert agent.stop_switch_available() is True

    check = agent.cycle_stop_check("cycle-1", principal=GARDENER, path="/tmp/x.db")
    assert check() is False
    assert check() is True
    assert asked[0] == ("cycle-1", GARDENER, "/tmp/x.db")

    run = agent.for_cycle(cycle_id="cycle-1", principal=GARDENER)
    assert run.report().stop_switch == agent.STOP_SWITCH_ARMED


def test_the_kill_switch_is_not_a_reuse_of_abandon_cycle():
    """K1, structurally. ``abandon_cycle`` is a *repair* — a human declaring
    somebody else's dead process dead — and the kill switch is an instruction to
    a live run. A journal that could not tell "the operator stopped this" from
    "this process died" would fail the human reading a ``failed`` cycle at 09:00.
    """
    source = Path(agent.__file__).read_text(encoding="utf-8")
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert "abandon_cycle" not in calls
    assert "close_cycle" not in calls, "the runtime does not close cycles; the runner does"


# ── The report (A1, B3) ───────────────────────────────────────────────────────


def test_the_report_carries_the_cost_of_the_run_and_of_each_job():
    """A1: cost is a property of *the run*, not of any one write."""
    provider = FakeProvider(_completion(prompt_tokens=200, output_tokens=30))
    run = _run(provider=provider, tokens=10_000)
    jobs = run.split({"synthesis": 0.5, "curation": 0.5})
    run.chat(PROMPT, prompt_version=VERSION, job=jobs["synthesis"])
    run.chat(PROMPT, prompt_version=VERSION, job=jobs["synthesis"])

    report = run.report()
    assert report.provider == "fake://provider"
    assert report.model_id == "fake-model"
    assert report.calls == 2
    assert (report.prompt_tokens, report.output_tokens, report.total_tokens) == (400, 60, 460)
    assert report.budget_tokens == 10_000
    assert report.elapsed_seconds >= 0
    by_job = {job.job: job for job in report.per_job}
    assert by_job["synthesis"].calls == 2
    assert by_job["synthesis"].prompt_tokens == 400
    assert by_job["curation"].calls == 0, (
        "a declared job that never got a turn must still appear, or a reader cannot "
        "tell 'no work' from 'no turn'"
    )


def test_a_provider_absence_and_a_budget_exhaustion_are_different_report_shapes():
    """B3, the property this whole section exists for.

    An absent provider is a *stable* statement about this install, true every
    night until somebody changes it. An exhausted budget is a statement about
    *this* run, false again tomorrow. A human who reads "degraded" and shrugs is
    right about the first and wrong about the second, so the journal must not
    print them in one voice.
    """
    absent = _run().report()
    assert absent.available is False
    assert absent.unavailable_reason is not None
    assert absent.exhausted is False
    assert absent.skipped == []

    spent = _run(provider=FakeProvider(), tokens=10)
    with pytest.raises(agent.BudgetExhausted):
        spent.chat(PROMPT, prompt_version=VERSION, item_id="node-1")
    exhausted = spent.report()
    assert exhausted.available is True
    assert exhausted.unavailable_reason is None, (
        "exhaustion borrowed the absence's vocabulary; they are different facts"
    )
    assert exhausted.exhausted is True
    assert [item.id for item in exhausted.skipped] == ["node-1"]


def test_exhaustion_is_never_written_as_a_note():
    """5a's degraded path owns the ``notes`` vocabulary, and this must not join it.

    ``JobOutcome.notes`` carries the sentences that interpret a *configuration*
    — "no embedding provider, so near-duplicates worded differently are not
    found". This report has no such field, on purpose: exhaustion is a named
    boolean plus an itemised list, and nothing here can end up beside a note.

    Scope, stated so the claim does not outrun the check: this pins the shape
    :class:`~nodum.agent.LLMReport` **can** take. A runner is still free to
    write its own ``JobOutcome.notes`` sentence about a budget, and no test
    here can stop it — the defence against that is that the runner has a named
    field to use instead, which is what this file supplies.
    """
    fields = set(agent.LLMReport.model_fields)
    assert "notes" not in fields and "note" not in fields
    assert {"exhausted", "skipped", "available", "unavailable_reason"} <= fields
    assert set(agent.JobCost.model_fields).isdisjoint({"note", "notes"})


def test_the_report_is_json_and_holds_no_infinity():
    """``cycles.report`` is stored with ``json.dumps``, which happily writes a
    bare ``Infinity`` that is not JSON at all — so no ceiling may be infinite."""
    run = _run(provider=FakeProvider(), tokens=10_000, seconds=1800.0)
    run.split({"a": 0.5})
    run.chat(PROMPT, prompt_version=VERSION)
    rendered = json.dumps(run.report().model_dump(mode="json"), allow_nan=False)
    assert "Infinity" not in rendered
    assert json.loads(rendered)["model_id"] == "fake-model"


def test_the_report_key_and_the_props_key_are_named_once():
    """The later waves splice these in; a second spelling would be a second home."""
    assert agent.REPORT_KEY == "llm"
    assert agent.GENERATED_BY_PROP == "generated_by"


# ── Provenance (A1–A3) ────────────────────────────────────────────────────────


def test_generated_by_carries_three_fields_and_no_cost():
    """Cost is a property of the run; provenance is a property of the write."""
    provenance = agent.GeneratedBy(
        provider="fake://provider", model_id="fake-model", prompt_version="abc123def456"
    )
    assert set(provenance.model_dump()) == {"provider", "model_id", "prompt_version"}
    assert provenance.as_props() == {
        "generated_by": {
            "provider": "fake://provider",
            "model_id": "fake-model",
            "prompt_version": "abc123def456",
        }
    }


def test_a_prompt_version_changes_when_and_only_when_the_template_does():
    """A2: two cycles a month apart can name the same model and differ because
    the prompt changed; a journal that cannot tell them apart reports a mixture."""
    first = agent.prompt_version("Summarise the following nodes:\n{nodes}")
    again = agent.prompt_version("Summarise the following nodes:\n{nodes}")
    changed = agent.prompt_version("Summarise the following nodes concisely:\n{nodes}")
    assert first == again
    assert first != changed
    assert len(first) == 12 and first.isalnum()


def test_the_provenance_of_a_run_with_no_provider_cannot_be_invented():
    run = _run()
    with pytest.raises(llm.ProviderUnavailable):
        run.generated_by(VERSION)


# ── Construction (B1, K2) ─────────────────────────────────────────────────────


def test_a_cycle_run_is_off_by_default():
    """K2 level 1, and the same posture as ``NODUM_CONSOLIDATE_AT`` being unset:
    a background process spending the human's night is not enabled by surprise."""
    llm.set_provider(FakeProvider())
    run = agent.for_cycle(cycle_id="c1", principal=GARDENER)
    assert run.budget.tokens == 0
    assert run.available is True
    assert run.enabled is False


def test_a_cycle_budget_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(agent.ENV_CYCLE_BUDGET, "40000")
    monkeypatch.setenv(agent.ENV_CYCLE_SECONDS, "900")
    llm.set_provider(FakeProvider())
    run = agent.for_cycle(cycle_id="c1", principal=GARDENER)
    assert (run.budget.tokens, run.budget.seconds) == (40000, 900.0)
    assert run.enabled is True
    assert run.purpose == "cycle:c1"


def test_a_request_run_is_on_by_default_and_bounded():
    """ "Off by default" exists to stop an unattended process spending the
    human's night. A human pressing a button is not that — but it is still
    bounded, and bounded small."""
    llm.set_provider(FakeProvider())
    run = agent.for_request(purpose="ask", principal=HUMAN)
    assert run.budget.tokens == agent.DEFAULT_REQUEST_BUDGET
    assert run.enabled is True
    assert run.stop is None, "a request has no cycle row to be stopped through"
    assert run.purpose == "request:ask"


@pytest.mark.parametrize("value", ["", "  ", "lots", "-5", "3.5"])
def test_an_unparseable_budget_falls_back_to_the_default_rather_than_raising(
    monkeypatch, value: str
):
    """The scheduler's precedent: a server that will not boot over a stray
    character in an optional setting is worse than one that says what it
    skipped. And the fallback here is 0 — *off* — so a typo cannot accidentally
    authorise spending."""
    monkeypatch.setenv(agent.ENV_CYCLE_BUDGET, value)
    llm.set_provider(FakeProvider())
    assert agent.for_cycle(cycle_id="c1", principal=GARDENER).budget.tokens == 0


def test_an_explicit_budget_overrides_the_environment(monkeypatch):
    monkeypatch.setenv(agent.ENV_CYCLE_BUDGET, "40000")
    llm.set_provider(FakeProvider())
    budget = agent.Budget(name="cycle:c1", tokens=7, seconds=1.0)
    assert agent.for_cycle(cycle_id="c1", principal=GARDENER, budget=budget).budget is budget


def test_the_per_call_ceilings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(agent.ENV_MAX_OUTPUT_TOKENS, "1024")
    monkeypatch.setenv(agent.ENV_CALL_TIMEOUT, "30")
    llm.set_provider(FakeProvider())
    run = agent.for_request(purpose="ask", principal=HUMAN)
    assert run.max_output_tokens == 1024
    assert run.call_timeout == 30.0


# ── The runtime is a peer client too (P2/P3) ─────────────────────────────────


def test_the_runtime_opens_no_connection_and_mints_no_principal():
    """It receives a principal; it never makes one. And it reads no table — the
    one row it will read (the stop flag) goes through a public service function
    like every other read this package's clients make."""
    tree = ast.parse(Path(agent.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "sqlite3" not in imported

    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    forbidden = {"connect", "cursor", "execute", "executemany", "commit", "init_db", "Principal"}
    assert calls & forbidden == set(), f"reaches past the service: {sorted(calls & forbidden)}"

    private = [
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"service", "llm"}
        and node.attr.startswith("_")
    ]
    assert private == [], f"reaches into a private: {private}"
