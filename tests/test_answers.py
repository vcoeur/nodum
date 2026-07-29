"""The read-only smart surface: what it answers, and what it refuses to.

Every property here is about the *deterministic* half. The model produces text;
this module decides what happens to it, and the decisions are the ones the
design pass measured a local model getting wrong:

* a schema-valid object claiming to have answered a question its context could
  not answer,
* a citation naming a node the answer is not in,
* a citation echoing the prompt's own ``id=`` prefix rather than an id,
* an output ceiling producing unparseable JSON rather than a short object.

So no test asserts on model output text. A fake provider replays scripted
completions, exactly as ``tests/conftest.py``'s ``HashEmbedder`` stands in for a
real embedding model, and the assertions are about what
:mod:`nodum.answers` does with them.
"""

from __future__ import annotations

import ast
import json
import os
import re
import urllib.request
from pathlib import Path

import pytest
from helpers import owner

from nodum import agent, answers, llm, service
from nodum import search as search_module

# ── Fakes ─────────────────────────────────────────────────────────────────────


def _completion(
    *,
    text: str = "{}",
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
    """Replays scripted completions and records what it was asked."""

    provider_id = "fake://provider"
    model_id = "fake-model"

    def __init__(
        self, *answers_: llm.Completion | BaseException, context_tokens: int = 4096
    ) -> None:
        self.answers = list(answers_) or [_completion()]
        self.calls: list[dict] = []
        self.context_tokens = context_tokens

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


def _reply(answer: str, cited: list[str]) -> llm.Completion:
    return _completion(text=json.dumps({"answer": answer, "cited": cited}))


def _summary_reply(summary: str, cited: list[str]) -> llm.Completion:
    return _completion(text=json.dumps({"summary": summary, "cited": cited}))


@pytest.fixture()
def graph(fresh_db):
    """Three nodes whose text differs enough for BM25 to tell them apart."""
    principal = owner()
    first = service.create_node(
        type="note",
        title="Log compaction",
        content="A compacted topic keeps the newest value per key, so it works as a state store.",
        principal=principal,
    )
    second = service.create_node(
        type="note",
        title="Consumer group rebalancing",
        content="A rebalance reassigns partitions across the consumers of a group.",
        principal=principal,
    )
    third = service.create_node(
        type="note",
        title="Watercolour paper",
        content="Cold-pressed paper holds a wash without buckling.",
        principal=principal,
    )
    return {"compaction": first.id, "rebalance": second.id, "paper": third.id}


def _run(*, tokens: int = 100_000) -> agent.AgentRun:
    """A request run with an explicit budget, so no environment reaches a test."""
    return agent.for_request(
        purpose="test",
        principal=owner(),
        budget=agent.Budget(name="request:test", tokens=tokens, seconds=600.0),
    )


# ── This module is a peer client too (P2/P3) ─────────────────────────────────


def _module_ast() -> ast.Module:
    return ast.parse(Path(answers.__file__).read_text(encoding="utf-8"))


def test_the_smart_surface_opens_no_connection_and_mints_no_identity():
    """It receives a principal and reads through the public service, like the
    runner and the runtime before it.

    The reachability half of P3 — that this module cannot import
    :mod:`nodum.llm` — is held package-wide by
    ``test_only_the_agent_runtime_reaches_the_provider`` in
    ``tests/test_llm.py``, whose glob covers every module in the package
    including this one. Restating it here with a weaker extractor would be a
    second rail that looks like the first and sees less, so what is asserted
    here is the part that rail does not cover.
    """
    tree = _module_ast()
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "sqlite3" not in imported

    from_nodum = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "nodum"
        for alias in node.names
    }
    assert "auth" not in from_nodum, "an identity enters through the caller, never through auth"
    assert "llm" not in from_nodum, "reach the model through nodum.agent (design P3)"
    assert "agent" in from_nodum, "this test is broken if the runtime is not imported at all"

    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    # Non-vacuity: an extractor that found nothing would satisfy every
    # disjointness assertion below by describing an empty module.
    assert {"search", "subgraph", "chat"} <= calls, f"the call extractor is broken: {sorted(calls)}"
    forbidden = {"connect", "cursor", "execute", "commit", "init_db", "Principal"}
    assert calls & forbidden == set(), f"reaches past the service: {sorted(calls & forbidden)}"

    private = [
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"service", "agent", "search_module"}
        and node.attr.startswith("_")
    ]
    assert private == [], f"reaches into a private: {private}"


def test_this_module_writes_nothing_at_all(fresh_db):
    """E1's headline, asserted over the module rather than over one call.

    Every ``ask``/``summarize`` test below checks its own path; this checks the
    thing those cannot — that there is no write to reach. A service writer named
    here would be a write one refactor away, however carefully the current call
    sites read.
    """
    writers = {
        "create_node",
        "update_node",
        "create_edge",
        "propose_edges",
        "transition",
        "accept_proposals",
        "reject_proposals",
        "open_cycle",
        "close_cycle",
        "merge_nodes",
        "retype",
    }
    named = {
        node.func.attr
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # Non-vacuity, twice over. The extractor must be finding the reads this
    # module really makes, and every name in `writers` must still be a writer —
    # a renamed service function would otherwise retire its own guard silently.
    assert {"get_node", "subgraph", "search"} <= named, f"the extractor is broken: {sorted(named)}"
    missing = [name for name in writers if not hasattr(service, name)]
    assert missing == [], f"these are not service functions any more: {missing}"
    assert named & writers == set(), (
        f"the read-only surface calls a writer: {sorted(named & writers)}"
    )


# ── A citation is validated against the graph, never trusted (E2) ─────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", "first"),
        ("[1]", "first"),
        ("  2 ", "second"),
        # The measured artefact: the model echoed the prompt's literal `id=`
        # prefix rather than the id behind it.
        ("id=1", "first"),
        ("id: 2", "second"),
        ("node_id=1", "first"),
        ("#1", "first"),
        ('"1"', "first"),
        # The real id is accepted too, because the prompt carries both and the
        # model may echo either.
        ("__FIRST_ID__", "first"),
        ("id=__SECOND_ID__", "second"),
    ],
)
def test_a_citation_is_read_defensively_and_resolved_against_what_was_retrieved(
    raw: str, expected: str
):
    offered = [
        answers.Offered(marker=1, node_id="a1b2", title="First", space_id="main", text=""),
        answers.Offered(marker=2, node_id="c3d4", title="Second", space_id="main", text=""),
    ]
    ids = {"first": "a1b2", "second": "c3d4"}
    raw = raw.replace("__FIRST_ID__", "a1b2").replace("__SECOND_ID__", "c3d4")
    resolved = answers.resolve_citation(raw, offered)
    assert resolved is not None and resolved.node_id == ids[expected]


@pytest.mark.parametrize(
    "raw",
    [
        "3",  # a marker outside what was offered
        "n2",  # the design pass's wrong-node citation, on ids this search never returned
        "id=n0",
        "",
        "   ",
        "the first one",
        "0",
        "-1",
    ],
)
def test_a_citation_that_names_nothing_retrieved_resolves_to_nothing(raw: str):
    offered = [
        answers.Offered(marker=1, node_id="a1b2", title="First", space_id="main", text=""),
        answers.Offered(marker=2, node_id="c3d4", title="Second", space_id="main", text=""),
    ]
    assert answers.resolve_citation(raw, offered) is None


# ── What the prompt may and may not contain (measured on the local models) ────


def test_the_prompt_never_shows_a_node_id(graph):
    """Measured: with ``11660be27af84685afe404f31e02253a`` beside the marker,
    the local model cited ``"116"`` and ``"749"`` — mining the id for digits.
    Every extra number in a prompt is another thing that can come back as a
    citation, so the prompt carries exactly one number per note.

    Removing the ids took ``llama3.2:1b`` from 4/6 to 6/6 on a six-question
    battery, with the rest of the prompt unchanged.
    """
    provider = FakeProvider(_reply("x", ["1"]))
    llm.set_provider(provider)
    answers.ask("compacted topic state store", principal=owner(), run=_run())
    prompt = provider.calls[0]["messages"][0].content
    assert graph["compaction"] not in prompt
    assert "[1]" in prompt, "the marker is what a citation names"


def test_the_prompt_offers_no_number_a_model_could_copy_as_a_citation(graph):
    """The other half of the same finding, from the other side.

    An earlier prompt gave the format as a worked example — ``write exactly:
    ["1", "3"]`` — and the model copied ``"3"`` on every single call. It scored
    *better* that way (5/6 against 4/6) and it was still wrong to ship: on this
    graph marker 3 did not exist so validation dropped it, but on a graph where
    the search returns three hits that copied number resolves to a real note the
    answer did not come from, and nothing downstream can tell.
    """
    provider = FakeProvider(_reply("x", ["1"]))
    llm.set_provider(provider)
    answers.ask("compacted topic state store", principal=owner(), k=1, run=_run())
    instructions = provider.calls[0]["messages"][0].content.split("Notes:")[0]
    assert "1" not in instructions, "an example citation is an example the model copies"


def test_a_citation_is_constrained_by_the_schema_as_well_as_checked_after(graph):
    """Belt and braces, and they fail differently.

    The ``pattern`` is enforced by the provider's constrained decoding, which
    makes ``"]"``, ``"space main"`` and a chat template's own
    ``<|start_header_id|>`` marker *unrepresentable* rather than merely
    discouraged — all three were measured coming back where a note number
    belongs. It proves nothing about the content, which is what
    :func:`resolve_citation` is for, and it is the provider's promise rather
    than this package's, which is why the parser stays generous anyway.
    """
    provider = FakeProvider(_reply("x", ["1"]))
    llm.set_provider(provider)
    answers.ask("compacted topic state store", principal=owner(), run=_run())
    citation_schema = provider.calls[0]["schema"]["properties"]["cited"]["items"]
    assert citation_schema["pattern"] == answers._CITATION_PATTERN
    assert re.fullmatch(citation_schema["pattern"], "12")
    assert not re.fullmatch(citation_schema["pattern"], "]")
    assert not re.fullmatch(citation_schema["pattern"], "space main")


# ── /ask: answered is computed, never taken from the model (E2) ───────────────


def test_a_cited_answer_is_answered_and_names_the_node_it_cited(graph):
    llm.set_provider(
        FakeProvider(_reply("A compacted topic keeps the newest value per key.", ["1"]))
    )
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is True
    assert result.answer
    assert [citation.node_id for citation in result.citations] == [graph["compaction"]]
    assert result.refusal is None
    assert result.used.calls == 1


def test_an_answer_whose_every_citation_is_unresolvable_is_not_an_answer(graph):
    """The measurement this rule exists for, in the shape it was measured in.

    Under a JSON schema the model answered a question its context could not
    answer with a schema-valid object. Nothing in the envelope says so — the
    only deterministic signal is whether what it cited is something the search
    actually returned.
    """
    llm.set_provider(FakeProvider(_reply("Yes, definitely.", ["id=n0", "n2", "17"])))
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert result.answer is None, "an unanswered question does not hand back the text"
    assert result.citations == []
    assert result.unresolved == ["id=n0", "n2", "17"]
    assert result.refusal and "cited" in result.refusal


def test_a_partly_wrong_citation_list_keeps_the_part_that_resolves(graph):
    llm.set_provider(FakeProvider(_reply("A compacted topic keeps the newest value.", ["1", "n2"])))
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is True
    assert [citation.node_id for citation in result.citations] == [graph["compaction"]]
    assert result.unresolved == ["n2"]


def test_an_empty_answer_with_a_good_citation_is_still_not_an_answer(graph):
    llm.set_provider(FakeProvider(_reply("   ", ["1"])))
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert result.answer is None


def test_the_schema_never_asks_the_model_whether_it_answered(graph):
    """A field nobody may read is a field the schema should not carry.

    The measured failure is ``{"answer": "false", "cited_ids": [], "answered":
    true}``. Keeping ``answered`` out of the schema entirely is what makes it
    impossible for a later reader to wire it up by mistake.
    """
    provider = FakeProvider(_reply("A compacted topic keeps the newest value.", ["1"]))
    llm.set_provider(provider)
    answers.ask("compacted topic state store", principal=owner(), run=_run())
    schema = provider.calls[0]["schema"]
    assert set(schema["properties"]) == {"answer", "cited"}


def test_a_question_the_search_cannot_serve_costs_no_model_call(graph):
    provider = FakeProvider(_reply("anything", ["1"]))
    llm.set_provider(provider)
    result = answers.ask("zzzznothingmatchesthis", principal=owner(), run=_run())
    assert result.answered is False
    assert provider.calls == [], "nothing to answer from is not a question worth spending on"
    assert result.considered == []
    assert result.refusal and "search" in result.refusal


def test_the_prompt_carries_only_what_was_retrieved(graph):
    provider = FakeProvider(_reply("x", ["1"]))
    llm.set_provider(provider)
    result = answers.ask("compacted topic state store", principal=owner(), k=1, run=_run())
    prompt = "\n".join(message.content for message in provider.calls[0]["messages"])
    assert "Log compaction" in prompt
    assert "Watercolour paper" not in prompt
    assert result.considered == [graph["compaction"]]


# ── The context is fitted to the window before the call, not after (E1) ──────


def test_the_prompt_carries_the_node_s_own_text_and_not_the_search_snippet(graph):
    """A 200-character snippet with match markers in it ranks a node; it does
    not answer from one. The measurement that makes this matter is the other
    way round — a prompt that carries *too much* is silently truncated — which
    is what :func:`_fit_prompt` bounds."""
    provider = FakeProvider(_reply("x", ["1"]))
    llm.set_provider(provider)
    answers.ask("compacted topic state store", principal=owner(), k=1, run=_run())
    prompt = provider.calls[0]["messages"][0].content
    assert "so it works as a state store." in prompt, "the node's own text, not the index's summary"


def test_a_context_too_wide_for_the_window_drops_the_worst_ranked_note(graph):
    """The measured failure this prevents: a 51 KB prompt and a 207 KB prompt
    both report 4 096 prompt tokens and the same wall time, with nothing in the
    response saying half of it was never read."""
    long_text = "compaction " * 400
    for _ in range(3):
        service.create_node(
            type="note", title="More compaction", content=long_text, principal=owner()
        )
    provider = FakeProvider(_reply("x", ["1"]), context_tokens=1800)
    llm.set_provider(provider)
    result = answers.ask("compaction", principal=owner(), k=4, run=_run())
    assert len(provider.calls) == 1, "a fitted prompt is still one call"
    assert result.dropped, "a note the window could not carry is reported, never silently missing"
    assert len(result.considered) < 4
    assert set(result.considered).isdisjoint(result.dropped)


def test_narrowing_the_excerpts_is_tried_before_dropping_a_note(graph):
    """A shorter excerpt of the top-ranked note is worth more than its absence."""
    long_text = "compaction " * 400
    for _ in range(3):
        service.create_node(
            type="note", title="More compaction", content=long_text, principal=owner()
        )
    provider = FakeProvider(_reply("x", ["1"]), context_tokens=4096)
    llm.set_provider(provider)
    result = answers.ask("compaction", principal=owner(), k=4, run=_run())
    assert result.dropped == [], "every note fits once the excerpts narrow"
    assert len(result.considered) == 4
    prompt = provider.calls[0]["messages"][0].content
    assert "…[truncated]" in prompt, "the excerpts really did narrow"


def test_a_window_nothing_fits_is_a_refusal_rather_than_a_prompt_nobody_reads(graph):
    provider = FakeProvider(_reply("x", ["1"]), context_tokens=600)
    llm.set_provider(provider)
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert provider.calls == [], "nothing is sent when nothing fits"
    assert result.considered == []
    assert result.dropped == [graph["compaction"]]
    assert result.refusal and "context window" in result.refusal


# ── Every failure is a refusal, never a traceback and never an empty answer ───


def test_no_provider_is_a_refusal_that_names_what_to_set(graph):
    llm.set_provider(None, reason="no LLM provider configured (set NODUM_LLM_MODEL to a model)")
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert result.used.available is False
    assert result.refusal and "NODUM_LLM_MODEL" in result.refusal


def test_an_output_ceiling_is_reported_as_the_failure_it_is(graph):
    """B3: the body at a ``length`` finish is cut mid-token, so it is no result.

    The runtime raises rather than handing back a truncated string; the caller's
    job is to render that as a failure rather than as an empty answer.
    """
    llm.set_provider(
        FakeProvider(_completion(text='{"answer": "Kafka Str', finish_reason="length"))
    )
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert result.answer is None
    assert result.refusal and "ceiling" in result.refusal
    assert result.used.output_tokens > 0, "a failed call is charged, because it was really spent"


def test_a_filled_context_is_reported_rather_than_answered_from_a_prefix(graph):
    llm.set_provider(
        FakeProvider(_completion(text='{"answer": "x", "cited": ["1"]}', prompt_tokens=4096))
    )
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert result.refusal and "context" in result.refusal


def test_an_unreachable_provider_is_a_refusal_rather_than_a_traceback(graph):
    llm.set_provider(FakeProvider(llm.ProviderUnavailable("connection refused: localhost:11434")))
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert result.refusal and "connection refused" in result.refusal


def test_a_reply_that_is_not_json_is_a_refusal(graph):
    llm.set_provider(FakeProvider(_completion(text="I think the answer is compaction.")))
    result = answers.ask("compacted topic state store", principal=owner(), run=_run())
    assert result.answered is False
    assert result.refusal and "JSON" in result.refusal


def test_an_exhausted_budget_refuses_before_it_spends(graph):
    provider = FakeProvider(_reply("x", ["1"]))
    llm.set_provider(provider)
    result = answers.ask("compacted topic state store", principal=owner(), run=_run(tokens=1))
    assert result.answered is False
    assert provider.calls == [], "a refusal costs nothing"
    assert result.used.exhausted is True


def test_a_blank_question_is_a_value_error_and_not_a_model_call(graph):
    provider = FakeProvider(_reply("x", ["1"]))
    llm.set_provider(provider)
    with pytest.raises(ValueError):
        answers.ask("   ", principal=owner(), run=_run())
    assert provider.calls == []


def test_k_is_clamped_rather_than_refused(graph):
    """``subgraph``'s rule: a caller passing an enormous cap gets the ceiling."""
    llm.set_provider(FakeProvider(_reply("x", ["1"])))
    result = answers.ask("compacted topic", principal=owner(), k=10_000, run=_run())
    assert result.k == answers.MAX_ASK_K


def test_a_k_below_one_is_still_the_refusal_every_capped_read_gives(graph):
    llm.set_provider(FakeProvider(_reply("x", ["1"])))
    with pytest.raises(ValueError):
        answers.ask("compacted topic", principal=owner(), k=0, run=_run())


# ── /summarize reads and never writes ────────────────────────────────────────


def test_a_summary_cites_the_nodes_it_was_given(graph):
    llm.set_provider(
        FakeProvider(_summary_reply("Compaction keeps the newest value per key.", ["1"]))
    )
    result = answers.summarize(graph["compaction"], principal=owner(), run=_run())
    assert result.summarized is True
    assert result.summary
    assert [citation.node_id for citation in result.citations] == [graph["compaction"]]


def test_a_summary_citing_nothing_that_resolves_is_not_a_summary(graph):
    llm.set_provider(FakeProvider(_summary_reply("A confident paragraph.", ["n7"])))
    result = answers.summarize(graph["compaction"], principal=owner(), run=_run())
    assert result.summarized is False
    assert result.summary is None
    assert result.unresolved == ["n7"]


def test_summarising_writes_nothing(graph):
    before = max(event.seq for event in service.list_events(owner(), limit=5000))
    llm.set_provider(FakeProvider(_summary_reply("A summary.", ["1"])))
    answers.summarize(graph["compaction"], principal=owner(), run=_run())
    after = max(event.seq for event in service.list_events(owner(), limit=5000))
    assert after == before, "nothing writes by default (E1)"


def test_summarising_a_node_that_does_not_resolve_is_the_services_refusal(graph):
    llm.set_provider(FakeProvider(_summary_reply("A summary.", ["1"])))
    with pytest.raises(service.RecordNotFound):
        answers.summarize("no-such-node", principal=owner(), run=_run())


def test_a_summary_with_no_provider_refuses_and_names_the_variable(graph):
    llm.set_provider(None, reason="no LLM provider configured (set NODUM_LLM_MODEL to a model)")
    result = answers.summarize(graph["compaction"], principal=owner(), run=_run())
    assert result.summarized is False
    assert result.refusal and "NODUM_LLM_MODEL" in result.refusal


# ── The rewrite layers on the quorum; it never replaces it (E3) ───────────────


def test_a_rewrite_runs_the_ordinary_search_with_the_model_s_terms(graph):
    llm.set_provider(FakeProvider(_completion(text=json.dumps({"terms": ["compacted", "topic"]}))))
    result = answers.natural_search("What did I write about compacted topics?", principal=owner())
    assert result.rewrite.applied is True
    assert result.rewrite.terms == ["compacted", "topic"]
    assert result.query == "compacted topic"
    assert [hit.node_id for hit in result.hits] == [graph["compaction"]]


def test_a_rewrite_with_no_provider_is_a_no_op_that_still_searches(graph):
    """E3's second reason: search must work without a provider."""
    llm.set_provider(None, reason="no LLM provider configured (set NODUM_LLM_MODEL to a model)")
    result = answers.natural_search("compacted topic state store", principal=owner())
    assert result.rewrite.applied is False
    assert result.rewrite.refusal and "NODUM_LLM_MODEL" in result.rewrite.refusal
    assert result.query == "compacted topic state store"
    assert [hit.node_id for hit in result.hits] == [graph["compaction"]]


def test_a_rewrite_that_produces_nothing_usable_falls_back_to_the_human_s_words(graph):
    llm.set_provider(FakeProvider(_completion(text=json.dumps({"terms": ["", "   "]}))))
    result = answers.natural_search("compacted topic state store", principal=owner())
    assert result.rewrite.applied is False
    assert result.query == "compacted topic state store"
    assert [hit.node_id for hit in result.hits] == [graph["compaction"]]


def test_a_hallucinated_term_cannot_empty_the_result_set(graph):
    """The E3 prerequisite, driven end to end rather than argued.

    A term the index has never seen is dropped before the quorum is computed,
    so a rewrite that invents one still answers. Under the conjunctive rule this
    query returned nothing.
    """
    llm.set_provider(
        FakeProvider(
            _completion(text=json.dumps({"terms": ["compacted", "topic", "onceonce semantics"]}))
        )
    )
    result = answers.natural_search("What did I write about compacted topics?", principal=owner())
    assert result.rewrite.applied is True
    assert [hit.node_id for hit in result.hits] == [graph["compaction"]]
    # The control: the invented term really is one this index has never seen, so
    # the branch under test (df = 0, dropped before the quorum) is the branch
    # that ran rather than a term that happened to match anyway.
    alone = search_module.search("onceonce", principal=owner())
    assert alone.hits == []


def test_a_rewrite_failure_leaves_the_search_working(graph):
    llm.set_provider(FakeProvider(llm.ProviderUnavailable("connection refused")))
    result = answers.natural_search("compacted topic state store", principal=owner())
    assert result.rewrite.applied is False
    assert result.rewrite.refusal and "connection refused" in result.rewrite.refusal
    assert [hit.node_id for hit in result.hits] == [graph["compaction"]]


def test_the_rewrite_sets_no_per_call_output_ceiling_of_its_own(graph):
    """Measured, and it cost the feature on one of the two local models.

    A rewrite needs about fifty output tokens, and an earlier version said so
    with ``max_output_tokens=200``. Against ``qwen3:8b`` that **failed every
    single call**: a reasoning model spends its thinking tokens out of the same
    allowance — ollama charges them to ``completion_tokens`` and strips them
    from ``content``, so the reply comes back empty at ``finish_reason:
    "length"`` — and B3 correctly discards a truncated body, which turns the
    whole feature off on that model with a message about a ceiling nobody set
    deliberately.

    So there is one output knob and it is the human's
    (``NODUM_LLM_MAX_OUTPUT_TOKENS``). A tight per-call number here is not a
    saving; it is a model-compatibility setting in disguise.
    """
    provider = FakeProvider(_completion(text=json.dumps({"terms": ["compacted"]})))
    llm.set_provider(provider)
    run = _run()
    answers.natural_search("compacted topic", principal=owner(), run=run)
    assert provider.calls[0]["max_output_tokens"] == run.max_output_tokens
    assert run.max_output_tokens == agent.DEFAULT_MAX_OUTPUT_TOKENS


def test_a_rewrite_is_capped_so_one_call_cannot_become_a_long_query(graph):
    terms = [f"term{index}" for index in range(50)]
    llm.set_provider(FakeProvider(_completion(text=json.dumps({"terms": terms}))))
    result = answers.natural_search("compacted topic", principal=owner())
    assert len(result.rewrite.terms) == answers.MAX_REWRITE_TERMS


# ── Provider status: configured and reachable are different facts ─────────────


def test_status_with_no_provider_is_configured_false_and_reachable_unknown(fresh_db):
    llm.set_provider(None, reason="no LLM provider configured (set NODUM_LLM_MODEL to a model)")
    status = answers.provider_status(principal=owner())
    assert status.configured is False
    assert status.reachable is None, "an unconfigured provider was never asked"
    assert status.detail and "NODUM_LLM_MODEL" in status.detail
    assert status.model is None


def test_status_probes_a_configured_provider_and_says_it_answered(fresh_db):
    llm.set_provider(FakeProvider(_completion(text="pong")))
    status = answers.provider_status(principal=owner())
    assert (status.configured, status.reachable) == (True, True)
    assert (status.model, status.provider) == ("fake-model", "fake://provider")
    assert status.probe_ms is not None and status.probe_ms >= 0


def test_a_probe_that_hits_its_own_output_ceiling_still_proves_reachability(fresh_db):
    """The trap this probe is most likely to fall into.

    The probe asks for a handful of tokens, so a live server answering normally
    comes back ``finish_reason: "length"`` — a *failed call* by B3's rule, and a
    perfectly good proof that something is listening. Reading it as unreachable
    would report every healthy server as down.
    """
    llm.set_provider(FakeProvider(_completion(text="po", finish_reason="length")))
    status = answers.provider_status(principal=owner())
    assert status.reachable is True


def test_status_reports_an_unreachable_configured_provider_without_raising(fresh_db):
    llm.set_provider(FakeProvider(llm.ProviderUnavailable("connection refused: localhost:11434")))
    status = answers.provider_status(principal=owner())
    assert status.configured is True
    assert status.reachable is False
    assert status.detail and "connection refused" in status.detail


def test_a_timeout_is_unreachable_and_says_which_ceiling_bit(fresh_db):
    llm.set_provider(FakeProvider(llm.ProviderTimeout("timed out after 120.0s")))
    status = answers.provider_status(principal=owner())
    assert status.reachable is False
    assert status.detail and "timed out" in status.detail


def test_status_can_skip_the_probe_entirely(fresh_db):
    provider = FakeProvider(_completion(text="pong"))
    llm.set_provider(provider)
    status = answers.provider_status(principal=owner(), probe=False)
    assert status.configured is True
    assert status.reachable is None
    assert provider.calls == []


def test_status_reports_the_ceilings_a_request_would_spend_under(fresh_db):
    llm.set_provider(FakeProvider(_completion(text="pong")))
    status = answers.provider_status(principal=owner(), probe=False)
    assert status.budget_tokens == agent.DEFAULT_REQUEST_BUDGET
    assert status.max_output_tokens == agent.DEFAULT_MAX_OUTPUT_TOKENS
    assert status.context_tokens == 4096


# ── The one real-model test (opt-in) ──────────────────────────────────────────
#
# The same gate ``tests/test_llm.py`` and ``tests/test_embeddings.py`` already
# use: `NODUM_RUN_SLOW=1` plus a skip when nothing is serving. There is one
# convention for this in the repo and this is it.


def _live_server_is_up() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    os.environ.get("NODUM_RUN_SLOW") != "1",
    reason="real-model smoke test: set NODUM_RUN_SLOW=1 (needs a model serving)",
)
@pytest.mark.skipif(not _live_server_is_up(), reason="no OpenAI-compatible server on :11434")
def test_ask_drives_a_live_model_end_to_end(fresh_db, monkeypatch):
    """Structure and provenance only — never the model's words.

    What a live run proves that a fake cannot: that the provider really honours
    the citation ``pattern`` (constrained decoding is the server's job, not this
    package's), that the prompt this module builds fits inside the model's real
    context window, and that a real reply parses. Whether the answer is *right*
    is a property of the model and is measured by hand, not asserted here —
    temperature-0 determinism was measured on one backend and is a property of
    that backend.
    """
    monkeypatch.setenv(llm.ENV_MODEL, os.environ.get("NODUM_LLM_MODEL", "llama3.2:1b"))
    monkeypatch.setenv(agent.ENV_CALL_TIMEOUT, "600")
    llm.reset_provider()
    service.create_node(
        type="note",
        title="Log compaction",
        content=(
            "A compacted topic keeps only the newest value for each key, so it converges "
            "to a snapshot of current state and can be replayed to rebuild a table."
        ),
        principal=owner(),
    )
    run = agent.for_request(
        purpose="live",
        principal=owner(),
        budget=agent.Budget(name="request:live", tokens=100_000, seconds=900.0),
    )

    result = answers.ask("compacted topic state store", principal=owner(), run=run)

    assert result.used.calls == 1
    assert result.used.total_tokens > 0
    assert result.used.model_id
    # Whatever it said, every citation it produced is either a node this search
    # returned or is in `unresolved`. That invariant is the endpoint's whole
    # contract and it must hold against a real model, not only a scripted one.
    assert all(citation.node_id in result.considered for citation in result.citations)
    assert result.answered == bool(result.citations and result.answer)
    if not result.answered:
        assert result.answer is None
        assert result.refusal
