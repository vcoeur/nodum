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
    reasoning_tokens: int = 0,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
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
        reasoning_tokens=reasoning_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


class FakeProvider:
    """A provider that records what it was asked and replays what it was given."""

    provider_id = "fake://provider"
    model_id = "fake-model"
    context_tokens = 4096
    thinking = llm.DEFAULT_THINKING
    thinking_applied = True

    def __init__(self, *answers: llm.Completion | BaseException) -> None:
        self.answers = list(answers) or [_completion()]
        self.calls: list[dict[str, Any]] = []
        self.structured_mode = llm.STRUCTURED_JSON_SCHEMA

    def estimate_prompt_tokens(self, messages, *, schema=None) -> int:
        return llm.estimate_prompt_tokens(messages)

    def output_reservation(self, max_output_tokens: int) -> int:
        """The real provider's rule — a reservation capped at a share of the
        window — because this stands in for a provider and an identity function
        models an endpoint that does not exist."""
        share = int(self.context_tokens * llm.OUTPUT_RESERVATION_FRACTION)
        return max(1, min(max_output_tokens, share))

    def chat(
        self, messages, *, schema=None, max_output_tokens, timeout, thinking=None
    ) -> llm.Completion:
        self.calls.append(
            {
                "messages": list(messages),
                "schema": schema,
                "max_output_tokens": max_output_tokens,
                "timeout": timeout,
                "thinking": thinking,
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
    for name in (
        llm.ENV_MODEL,
        llm.ENV_BASE_URL,
        llm.ENV_API_KEY,
        llm.ENV_CONTEXT_TOKENS,
        llm.ENV_THINKING,
    ):
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

    The child is built directly rather than through a second ``split`` — two
    splits that together over-commit are now refused outright, which is a
    different property (below) — so what this pins is the arithmetic: a job
    holding a 900-token licence still cannot spend more than the cycle has left.
    """
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    first = cycle.split({"a": 0.9})["a"]
    second = agent.Budget(name="b", tokens=900, seconds=60.0, parent=cycle)
    first.charge(_completion(prompt_tokens=900, output_tokens=0))
    assert second.tokens == 900
    assert second.spent_tokens == 0
    assert second.remaining_tokens == 100, "b is bounded by what the cycle has left"


def test_a_split_that_over_commits_the_run_is_refused():
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    with pytest.raises(ValueError, match="over-commits"):
        cycle.split({"a": 0.7, "b": 0.5})


def test_shares_handed_out_in_separate_splits_cannot_over_commit_between_them():
    """The guard has to hold across calls, because that is how it is called.

    ``AgentRun.job`` is one ``split`` per job, so a guard that only saw the
    shares in its own argument let three jobs at 0.6 each hold 600 tokens of a
    1000-token cycle — 180 % of the budget, reported as such in
    ``LLMReport.per_job``. Actual *spending* is still bounded by the minimum
    down the chain; it is the report that lied, and a report nobody can trust
    about the budget is the whole of what this object is for.
    """
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    cycle.split({"a": 0.6})
    with pytest.raises(ValueError, match="over-commits"):
        cycle.split({"b": 0.6})
    assert sorted(cycle.declared_shares) == ["a"], "the refused share was recorded anyway"


def test_the_same_job_name_twice_is_refused_rather_than_replacing_the_first():
    """A second budget under one name loses the first one's recorded spend.

    ``AgentRun._jobs`` is keyed by name, so the replacement took the calls and
    the tokens of the job it displaced out of the report with it — a job that
    ran and cost money, reported as never having had a turn.
    """
    cycle = agent.Budget(name="cycle", tokens=1000, seconds=60.0)
    first = cycle.split({"synthesis": 0.5})["synthesis"]
    first.charge(_completion(prompt_tokens=100, output_tokens=10))
    with pytest.raises(ValueError, match="already"):
        cycle.split({"synthesis": 0.2})
    assert first.spent_tokens == 110


def test_a_split_of_exactly_one_and_of_thirds_is_allowed():
    """Float thirds sum to 1.0000000000000002; the tolerance is for that alone.

    Two budgets, because a whole budget can only be split once now — splitting
    the same one twice is the over-commit the guard exists for.
    """
    halves = agent.Budget(name="cycle", tokens=900, seconds=60.0)
    assert sum(budget.tokens for budget in halves.split({"a": 0.5, "b": 0.5}).values()) == 900
    thirds = agent.Budget(name="cycle", tokens=900, seconds=60.0).split(
        {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
    )
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


def test_the_per_call_timeout_is_clamped_to_what_is_left_of_the_run_clock():
    """Per-call ⊂ per-job ⊂ per-cycle has to hold for the *clock* as well.

    Measured with a 2.0 s ceiling and the shipped 120 s call timeout: the wall
    clock is checked before the call and never again, so one call that hangs
    runs the ceiling 60× over — the run reported ``elapsed 3.0`` against a 2.0 s
    budget, and with shipped defaults a night overruns by up to two minutes.
    """
    provider = FakeProvider()
    run = _run(
        provider=provider,
        seconds=2.0,
        started=time.monotonic() - 1.5,
        call_timeout=120.0,
    )
    run.chat(PROMPT, prompt_version=VERSION)
    assert provider.calls[0]["timeout"] == pytest.approx(0.5, abs=0.2), (
        "the provider was handed a timeout longer than the run's whole remaining clock"
    )


def test_an_explicit_per_call_timeout_is_clamped_too():
    """The clamp is a ceiling on the *effective* timeout, so a caller naming a
    generous one for a single expensive call cannot outlive the run either."""
    provider = FakeProvider()
    run = _run(provider=provider, seconds=5.0, started=time.monotonic() - 4.0)
    run.chat(PROMPT, prompt_version=VERSION, timeout=90.0)
    assert provider.calls[0]["timeout"] == pytest.approx(1.0, abs=0.2)


def test_a_short_per_call_timeout_is_left_alone():
    """The clamp only ever lowers: a run with hours left does not raise a
    caller's 5-second ceiling to hours."""
    provider = FakeProvider()
    run = _run(provider=provider, seconds=3600.0)
    run.chat(PROMPT, prompt_version=VERSION, timeout=5.0)
    assert provider.calls[0]["timeout"] == 5.0


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


def test_a_charged_but_discarded_call_is_counted_as_a_failure():
    """``failed_calls`` is *calls that produced no usable result*, not *calls that
    never reached the wire*.

    Both of these are charged — the tokens were really spent — and both have
    their body discarded. Counting them as plain ``calls`` made a night of three
    truncated answers report ``calls 3, failed_calls 0``: three successes, in a
    report whose only readers are a human asking whether the night worked and a
    later wave computing acceptance rates.
    """
    truncated = FakeProvider(
        _completion(
            text='{"title": "Kaf', prompt_tokens=90, output_tokens=8, finish_reason="length"
        )
    )
    run = _run(provider=truncated)
    with pytest.raises(llm.OutputTruncated):
        run.chat(PROMPT, prompt_version=VERSION)
    assert (run.report().calls, run.report().failed_calls) == (1, 1)

    overflowed = FakeProvider(_completion(prompt_tokens=4096, output_tokens=9, context_tokens=4096))
    other = _run(provider=overflowed)
    with pytest.raises(llm.ContextOverflow):
        other.chat(PROMPT, prompt_version=VERSION)
    assert (other.report().calls, other.report().failed_calls) == (1, 1)


def test_a_prompt_the_server_truncated_is_a_failed_call_even_below_the_configured_window():
    """The provider's second signal reaches the same refusal as the first.

    A window configured above the *serving* one lets an over-long prompt
    through; the server answers from a prefix and reports ``prompt_tokens`` far
    below the number the operator configured, so ``context_filled`` is false.
    The answer is still from a prefix, so it is still discarded and still
    charged.
    """
    provider = FakeProvider(
        llm.Completion(
            text="an answer from a prefix",
            prompt_tokens=4096,
            output_tokens=40,
            finish_reason="stop",
            model_id="fake-model",
            provider_id="fake://provider",
            context_tokens=32768,
            prompt_estimate=30_000,
            latency_ms=7,
        )
    )
    run = _run(provider=provider)
    with pytest.raises(llm.ContextOverflow) as failure:
        run.chat(PROMPT, prompt_version=VERSION)
    assert failure.value.completion.context_filled is False
    assert run.budget.spent_tokens == 4136, "a truncated-prompt call was not charged"
    assert run.report().failed_calls == 1


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


def test_a_prompt_too_long_records_the_item_it_refused():
    """A night that refused three items must not read as a night with no work.

    Nothing was sent, so nothing is billed and nothing is a *failure of the
    provider's* — but something was **not examined**, and that is exactly what
    ``skipped`` is for. Before this, three items refused for over-long prompts
    reported ``calls 0, failed_calls 0, skipped []``: byte-identical to a cycle
    on which the job found nothing to do.
    """
    provider = FakeProvider(llm.PromptTooLong("prompt is about 9000 tokens"))
    run = _run(provider=provider)
    job = run.job("synthesis", share=1.0)
    for item in ("node-1", "node-2", "node-3"):
        with pytest.raises(llm.PromptTooLong):
            run.chat(PROMPT, prompt_version=VERSION, job=job, item_id=item)

    report = run.report()
    assert [(skip.job, skip.id) for skip in report.skipped] == [
        ("synthesis", "node-1"),
        ("synthesis", "node-2"),
        ("synthesis", "node-3"),
    ]
    assert "does not fit" in report.skipped[0].reason
    assert report.exhausted is False, (
        "a prompt that did not fit the window is not a spending ceiling; saying "
        "exhausted would send a human looking for a night that cost too much"
    )
    assert (report.calls, report.failed_calls, report.total_tokens) == (0, 0, 0)


def test_a_prompt_too_long_with_no_item_named_records_no_skip():
    """The same rule the budget refusal follows: no invented id in the journal."""
    run = _run(provider=FakeProvider(llm.PromptTooLong("prompt is about 9000 tokens")))
    with pytest.raises(llm.PromptTooLong):
        run.chat(PROMPT, prompt_version=VERSION)
    assert run.report().skipped == []


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


def test_a_run_reports_a_switch_it_has_and_says_so_when_it_has_none():
    """:attr:`LLMReport.stop_switch`'s two postures, both reachable.

    The field exists so a journal entry never implies a switch that was not
    wired. It used to hold a third value — ``STOP_SWITCH_PENDING``, "the column
    is not in this database's schema yet" — which named a column that was never
    called that (``stop_requested_at``), described a gate that keyed on the
    service function rather than the column, and became unreachable the moment
    migration ``0015`` landed. What is left is the distinction that is real: a
    cycle has a row anyone can stamp, and a human's request has none.
    """
    cycle_run = agent.for_cycle(cycle_id="cycle-1", principal=GARDENER)
    assert cycle_run.stop is not None
    assert cycle_run.report().stop_switch == agent.STOP_SWITCH_ARMED

    request_run = agent.for_request(purpose="ask", principal=HUMAN)
    assert request_run.stop is None, "there is no cycle row to stamp for a request"
    assert request_run.report().stop_switch == agent.STOP_SWITCH_NONE
    assert request_run.report().stopped is False


def test_the_stop_check_forwards_what_it_was_given_and_re_reads_every_time(monkeypatch):
    """:func:`cycle_stop_check`'s own contract: the call shape, and no caching.

    This fake used to be installed with ``raising=False`` over a name that did
    **not exist** — the only way to reach the armed branch before migration
    ``0015``. That made it a fixture standing in for the real thing rather than
    a check on it, and it stayed that way after the real function landed, so the
    test no longer covered what it was written for.

    The fake now goes over the **real** ``service.stop_requested`` (``raising``
    left at its default), which is what makes a renamed or re-signed service read
    fail here instead of being silently shadowed. What it is still worth a fake
    for is the call *shape* — the cycle id positional, ``principal`` and ``path``
    forwarded verbatim, one fresh read per check. Whether the real service
    answers correctly is asserted against a real database with no fake at all, in
    ``tests/test_cycles.py::test_the_stop_switch_is_armed_and_the_runtime_reads_
    the_real_service``.
    """
    asked: list[tuple[str, Principal, Any]] = []

    def stop_requested(cycle_id: str, *, principal: Principal, path: Any = None) -> bool:
        asked.append((cycle_id, principal, path))
        return len(asked) > 1

    monkeypatch.setattr(agent.service, "stop_requested", stop_requested)

    check = agent.cycle_stop_check("cycle-1", principal=GARDENER, path="/tmp/x.db")
    assert check() is False
    assert check() is True, "a cached answer would be a switch nobody can hit mid-run"
    assert asked == [("cycle-1", GARDENER, "/tmp/x.db")] * 2


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


def test_the_per_job_budgets_in_a_report_never_add_up_to_more_than_the_run():
    """The report is the thing a human reads to answer "what did last night cost".

    Three jobs, each asking for 60 % of the cycle through :meth:`AgentRun.job`
    — the ordinary way a runner declares them, one call at a time.
    """
    run = _run(provider=FakeProvider(), tokens=1000)
    run.job("synthesis", share=0.6)
    with pytest.raises(ValueError, match="over-commits"):
        run.job("curation", share=0.6)
    report = run.report()
    assert sum(job.budget_tokens for job in report.per_job) <= report.budget_tokens
    assert [job.job for job in report.per_job] == ["synthesis"]


def test_a_repeated_job_name_does_not_take_the_first_jobs_spend_out_of_the_report():
    run = _run(
        provider=FakeProvider(_completion(prompt_tokens=200, output_tokens=30)), tokens=10_000
    )
    job = run.job("synthesis", share=0.5)
    run.chat(PROMPT, prompt_version=VERSION, job=job)
    with pytest.raises(ValueError, match="already"):
        run.job("synthesis", share=0.2)
    per_job = {cost.job: cost for cost in run.report().per_job}
    assert per_job["synthesis"].calls == 1
    assert per_job["synthesis"].prompt_tokens == 200


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


@pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "1e999", "nan", "NaN"])
def test_a_ceiling_that_is_not_a_finite_number_falls_back_to_the_default(monkeypatch, value: str):
    """``float()`` reads ``inf`` and ``1e999`` happily, and JSON cannot carry either.

    Measured: ``NODUM_LLM_REQUEST_SECONDS=inf`` gave ``POST /api/ask`` a 200
    whose body contained a bare ``"budget_seconds": Infinity`` — both
    serialisers (``cycles.report`` and every HTTP envelope) are ``json.dumps``
    with ``allow_nan`` at its default — and the browser's ``JSON.parse`` threw
    on it. A ceiling of ``nan`` is worse still: every comparison against it is
    false, so ``remaining_seconds <= 0`` never fires and the wall-clock ceiling
    silently stops existing.
    """
    monkeypatch.setenv(agent.ENV_REQUEST_SECONDS, value)
    monkeypatch.setenv(agent.ENV_CALL_TIMEOUT, value)
    llm.set_provider(FakeProvider())
    run = agent.for_request(purpose="ask", principal=HUMAN)
    assert run.budget.seconds == agent.DEFAULT_REQUEST_SECONDS
    assert run.call_timeout == agent.DEFAULT_CALL_TIMEOUT


def test_a_report_built_from_the_environment_is_strict_json(monkeypatch):
    """The property the existing infinity test claimed and could not reach.

    It built its budget in Python, where nobody writes ``inf``. The number that
    reaches ``budget_seconds`` on a real run comes from
    :func:`~nodum.agent._positive_float` reading the environment, and that is
    the path an operator can put ``Infinity`` on. ``allow_nan=False`` is what
    ``json.dumps`` is *not* called with anywhere in this system, which is why
    the bare token reached the wire.
    """
    monkeypatch.setenv(agent.ENV_REQUEST_SECONDS, "inf")
    llm.set_provider(FakeProvider())
    run = agent.for_request(purpose="ask", principal=HUMAN)
    rendered = json.dumps(run.report().model_dump(mode="json"), allow_nan=False)
    assert "Infinity" not in rendered


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


def test_a_zero_request_budget_names_the_variable_that_actually_funds_a_request(monkeypatch):
    """``NODUM_LLM_REQUEST_BUDGET=0 nodum ask`` used to advise setting
    ``NODUM_LLM_CYCLE_BUDGET``, which does nothing for a request: ``for_request``
    reads the request variable and never looks at the cycle one. AGENTS.md
    promises the refusal names the variable to set, and "turn the LLM jobs on"
    is cycle vocabulary for something a human asked for by hand.
    """
    monkeypatch.setenv(agent.ENV_REQUEST_BUDGET, "0")
    llm.set_provider(FakeProvider())
    run = agent.for_request(purpose="ask", principal=HUMAN)
    with pytest.raises(agent.BudgetExhausted) as refusal:
        run.chat(PROMPT, prompt_version=VERSION)
    message = str(refusal.value)
    assert refusal.value.kind == "off"
    assert agent.ENV_REQUEST_BUDGET in message
    assert agent.ENV_CYCLE_BUDGET not in message, "the refusal names a variable that does nothing"
    assert "job" not in message, "cycle vocabulary in a refusal a human asked for by hand"


def test_a_zero_cycle_budget_still_names_the_cycle_variable(monkeypatch):
    """The other posture, so the fix above is a routing and not a rename."""
    monkeypatch.setattr(agent.service, "stop_requested", lambda *_, **__: False)
    llm.set_provider(FakeProvider())
    run = agent.for_cycle(cycle_id="c1", principal=GARDENER)
    with pytest.raises(agent.BudgetExhausted) as refusal:
        run.chat(PROMPT, prompt_version=VERSION)
    assert agent.ENV_CYCLE_BUDGET in str(refusal.value)
    assert agent.ENV_REQUEST_BUDGET not in str(refusal.value)


def test_a_job_whose_share_rounds_to_zero_says_so_instead_of_saying_nothing_is_funded():
    """A funded run with an unfunded job is a *split* problem, not a switch problem.

    ``int(1000 * 0.0004)`` is 0, and the refusal that followed told the human to
    set ``NODUM_LLM_CYCLE_BUDGET`` — on a run whose report says ``enabled:
    true`` and whose budget is the number they already set.
    """
    run = _run(provider=FakeProvider(), tokens=1000)
    job = run.job("tiny", share=0.0004)
    assert job.tokens == 0
    with pytest.raises(agent.BudgetExhausted) as refusal:
        run.chat(PROMPT, prompt_version=VERSION, job=job)
    message = str(refusal.value)
    assert refusal.value.kind == "off"
    assert "share" in message
    assert agent.ENV_CYCLE_BUDGET not in message
    report = run.report()
    assert report.enabled is True
    assert report.exhausted is False, "nothing was skipped through spending"


@pytest.mark.parametrize("value", ["0", "-1", "lots"])
def test_a_zero_output_ceiling_falls_back_rather_than_failing_every_call(monkeypatch, value: str):
    """``NODUM_LLM_MAX_OUTPUT_TOKENS=0`` is an install misconfiguration, and it
    used to reach the provider as ``ValueError: max_output_tokens must be at
    least 1`` — a 400 in the client-error voice on every ``POST /api/ask``, about
    a request that was perfectly well formed. The budget variables are different:
    0 there is a *decision* (off), which is why the floor is a parameter and not
    a change to the reader.
    """
    monkeypatch.setenv(agent.ENV_MAX_OUTPUT_TOKENS, value)
    llm.set_provider(FakeProvider())
    run = agent.for_request(purpose="ask", principal=HUMAN)
    assert run.max_output_tokens == agent.DEFAULT_MAX_OUTPUT_TOKENS
    run.chat(PROMPT, prompt_version=VERSION)


def test_a_zero_budget_is_still_a_legitimate_setting(monkeypatch):
    """The floor above must not creep onto the budgets: 0 is *off*, and off is
    what a cycle budget defaults to."""
    monkeypatch.setenv(agent.ENV_CYCLE_BUDGET, "0")
    llm.set_provider(FakeProvider())
    assert agent.for_cycle(cycle_id="c1", principal=GARDENER).budget.tokens == 0


def test_the_wall_clock_starts_at_the_first_call_and_not_when_the_run_is_built(monkeypatch):
    """``for_cycle`` is built when the cycle opens, and the LLM jobs run last.

    With the clock started at construction, the four deterministic jobs' minutes
    were charged to the LLM's wall-clock ceiling — a report saying the model
    spent time it never had, and a ceiling partly spent before the first prompt
    was built. On a slow graph that is the whole ceiling.
    """
    now = [1000.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: now[0])
    run = _run(provider=FakeProvider(), seconds=60.0)

    now[0] += 600.0  # the deterministic jobs run for ten minutes
    assert run.budget.elapsed_seconds == 0.0
    assert run.budget.remaining_seconds == 60.0
    assert run.report().elapsed_seconds == 0.0

    run.chat(PROMPT, prompt_version=VERSION)
    now[0] += 5.0
    assert run.budget.elapsed_seconds == pytest.approx(5.0)
    assert run.report().elapsed_seconds == pytest.approx(5.0)


def test_a_job_declared_before_the_first_call_shares_the_clock_that_starts_later(monkeypatch):
    """A child copies its parent's clock at ``split`` time, so the lazy start has
    to reach the whole chain or a job declared up front keeps a clock of its own.
    """
    now = [1000.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: now[0])
    run = _run(provider=FakeProvider(), seconds=60.0)
    job = run.job("synthesis", share=1.0)

    now[0] += 600.0
    run.chat(PROMPT, prompt_version=VERSION, job=job)
    now[0] += 5.0
    assert job.elapsed_seconds == pytest.approx(5.0)
    assert run.budget.elapsed_seconds == pytest.approx(5.0)
    assert job.started == run.budget.started


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


def test_a_run_hands_nobody_the_provider_object():
    """P3's rail checks *imports*, and a module holding ``run.provider`` imports
    nothing.

    Demonstrated: a call through ``run.provider.chat(...)`` succeeded with the
    budget at 0 and a stop firing, and the run reported ``calls 0, total_tokens
    0, stopped True`` — unmetered, unstoppable, unattributed, and invisible to
    both the import rail and the ordering property, because neither one can see
    a call made on an object somebody was handed.
    """
    provider = FakeProvider()
    run = _run(provider=provider)
    handed_out = []
    for name in dir(run):
        if name.startswith("_"):
            continue
        try:
            value = getattr(run, name)
        except Exception:  # noqa: BLE001 — an accessor that refuses hands out nothing
            continue
        if value is provider:
            handed_out.append(name)
    assert handed_out == [], (
        f"AgentRun.{handed_out} hands back the provider itself; a caller holding it "
        "makes a call that no budget, stop check or report can see"
    )


def test_the_run_answers_what_a_prompt_builder_needs_without_handing_over_the_provider():
    """What replaces it: the two numbers a caller fitting a prompt actually asks
    for.

    Both are the *provider's*, not the run's own arithmetic — so a caller cannot
    end up fitting a prompt against one number and having it refused against
    another. Both fakes answer something no default could coincide with, because
    a run computing its own estimate from the same module-level function would
    otherwise pass this unchanged.
    """
    provider = FakeProvider()
    provider.context_tokens = 31_337
    provider.estimate_prompt_tokens = lambda messages, *, schema=None: 4_242
    run = _run(provider=provider)
    assert run.context_tokens == 31_337
    assert run.estimate_prompt_tokens(PROMPT) == 4_242


def test_the_accessors_are_absent_in_the_same_voice_the_rest_of_the_run_is():
    """No provider is an absence, not an exception at the top of a handler:
    ``context_tokens`` is ``None`` like ``model_id`` and ``provider_id`` already
    are, and the estimate — which has no honest ``None`` — refuses in the one
    way every other reach for a missing provider here refuses."""
    run = _run()
    assert run.available is False
    assert run.context_tokens is None
    with pytest.raises(llm.ProviderUnavailable, match="test default"):
        run.estimate_prompt_tokens(PROMPT)


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


# ── A reasoning model's cost is reported, not just charged ────────────────────


def test_reasoning_tokens_reach_the_run_report_without_being_double_charged():
    """The cost report has to say where the output allowance actually went.

    Reasoning tokens are a **share** of ``completion_tokens`` on the wire
    (measured on ``deepseek-v4-flash``: ``total_tokens`` is always
    ``prompt + completion``, and ``reasoning_tokens`` never exceeds
    ``completion_tokens``), so adding them to the total would report a night as
    costing up to twice the bill. Leaving them out of the report entirely is the
    other failure: a job that spent 1 420 of its 1 520 output tokens thinking
    reads exactly like one that wrote 1 520 tokens of proposal, and only the
    first is one sample away from returning nothing at all.
    """
    provider = FakeProvider(_completion(prompt_tokens=200, output_tokens=300, reasoning_tokens=250))
    run = _run(provider=provider)
    run.chat(PROMPT, prompt_version=VERSION)
    report = run.report()
    assert report.reasoning_tokens == 250
    assert report.output_tokens == 300, "reasoning must stay inside the output count"
    assert report.total_tokens == 500, "reasoning must not be added to the total"


def test_the_cache_counters_reach_the_run_report():
    """Priced ~50x apart, so a night's cost is not knowable without them."""
    provider = FakeProvider(
        _completion(prompt_tokens=154, output_tokens=25, cache_hit_tokens=128, cache_miss_tokens=26)
    )
    run = _run(provider=provider)
    run.chat(PROMPT, prompt_version=VERSION)
    report = run.report()
    assert (report.cache_hit_tokens, report.cache_miss_tokens) == (128, 26)


def test_reasoning_and_cache_are_reported_per_job_as_well_as_per_run():
    """``per_job`` is the number a human checks a night against, job by job."""
    provider = FakeProvider(
        _completion(prompt_tokens=100, output_tokens=200, reasoning_tokens=180, cache_hit_tokens=40)
    )
    run = _run(provider=provider)
    job = run.job("abstraction", share=0.5)
    run.chat(PROMPT, prompt_version=VERSION, job=job)
    costs = {cost.job: cost for cost in run.report().per_job}
    assert costs["abstraction"].reasoning_tokens == 180
    assert costs["abstraction"].cache_hit_tokens == 40


def test_a_failed_call_still_reports_the_reasoning_it_burned():
    """The case the accounting exists for.

    A ``length`` finish is *no result* on this interface, and on a reasoning
    model the overwhelmingly likely reason is that thinking ate the ceiling —
    measured, ``'ping'`` at a graded level returned an **empty body** with every
    one of its 8 output tokens spent reasoning. A report that dropped the
    reasoning count on exactly the calls that failed would hide the cause of the
    failure it is reporting.
    """
    provider = FakeProvider(
        _completion(
            prompt_tokens=100, output_tokens=512, reasoning_tokens=512, finish_reason="length"
        )
    )
    run = _run(provider=provider)
    with pytest.raises(agent.OutputTruncated):
        run.chat(PROMPT, prompt_version=VERSION)
    report = run.report()
    assert report.failed_calls == 1
    assert report.reasoning_tokens == 512, "a charged failure must report what it burned"
    assert report.total_tokens == 612


def test_a_generation_carries_the_reasoning_its_call_spent():
    provider = FakeProvider(_completion(prompt_tokens=10, output_tokens=90, reasoning_tokens=60))
    run = _run(provider=provider)
    generation = run.chat(PROMPT, prompt_version=VERSION)
    assert generation.reasoning_tokens == 60
    assert generation.output_tokens == 90


# ── The reasoning level is per call site, over a global default ───────────────


def test_the_run_passes_its_thinking_level_through_to_the_provider():
    provider = FakeProvider()
    run = _run(provider=provider)
    run.chat(PROMPT, prompt_version=VERSION)
    assert provider.calls[-1]["thinking"] is None, (
        "with nothing named the provider's own configured level must decide, not the run's"
    )


def test_a_call_site_may_pin_its_own_thinking_level():
    """The call sites do not want the same thing, measured.

    A reachability probe has nothing to reason about and spent 506 thinking
    tokens returning an **empty body** when it was allowed to try; a query
    rewrite is an 8-term keyword expansion that ran 3.4x faster and perfectly
    deterministically at ``none``. An answer worth reviewing is the opposite
    case. One global level cannot serve all of them.
    """
    provider = FakeProvider()
    run = _run(provider=provider)
    run.chat(PROMPT, prompt_version=VERSION, thinking=llm.THINKING_NONE)
    assert provider.calls[-1]["thinking"] == llm.THINKING_NONE


def test_the_run_reports_which_structured_mode_the_provider_will_use():
    """A drop to ``json_object`` weakens what a caller may assume about the body,
    so it has to be legible from the run rather than only from the provider the
    run deliberately does not hand out."""
    provider = FakeProvider()
    provider.structured_mode = llm.STRUCTURED_JSON_OBJECT
    run = _run(provider=provider)
    assert run.structured_mode == llm.STRUCTURED_JSON_OBJECT
