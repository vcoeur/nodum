"""The provider seam, and the rail that keeps the service layer LLM-free.

Two halves. The first is design Constraint 4 stated structurally (P2): the
deterministic layer — :mod:`nodum.service`, :mod:`nodum.projectors`,
:mod:`nodum.store`, :mod:`nodum.migrations` — may not reach :mod:`nodum.llm`,
**under any name and by any number of hops**. The second is the provider
itself: what it refuses before it sends, what it carries back, and what it
turns a failure into.

No test here asserts on model output text. Temperature-0 determinism was
measured on the local backend and is a property of *that* backend, not of the
interface; the one real-model test is opt-in behind ``NODUM_RUN_SLOW=1``,
matching the convention ``tests/test_embeddings.py`` already set.
"""

from __future__ import annotations

import ast
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

import nodum
from nodum import llm

# ── The rail: the deterministic layer cannot reach the provider (P2) ──────────
#
# 5a held the mirror of this property over `nodum/consolidate.py` — no sqlite3,
# no `nodum.db`, no service private, imports from an allowlist. This is the same
# move from the other side, and it is stronger than a convention and cheaper
# than a layer: it states the constraint in the form it is actually violated in.
# Not "don't call an LLM in validation" but "validation cannot reach the module
# that could".

#: The modules design Constraint 4 keeps deterministic. Every one of them is
#: checked, and a module missing from this set is not exempt — it is simply not
#: one of the four the constraint names.
LLM_FREE_MODULES = {
    "nodum.service",
    "nodum.projectors",
    "nodum.store",
    "nodum.migrations",
}

#: The module they may not reach.
PROVIDER_MODULE = "nodum.llm"


def _package_modules() -> dict[str, Path]:
    """Every module in the package, by dotted name — ``__init__.py`` included.

    The package's own ``__init__`` is a node like any other and skipping it was
    a hole rather than a tidiness: most modules here say ``from nodum import
    …``, so the ``nodum`` node has inbound edges from most of the graph, and
    with no outbound edges of its own a re-export placed there — the one-line
    convenience somebody eventually adds — would put the provider one hop from
    every guarded module with nothing to notice.
    """
    modules = {
        f"nodum.{path.stem}": path
        for path in Path(nodum.__file__).parent.glob("*.py")
        if path.stem != "__init__"
    }
    modules["nodum"] = Path(nodum.__file__)
    return modules


def _nodum_imports(source: str) -> set[str]:
    """Every ``nodum.*`` module this source can reach *directly*, however spelled.

    The spellings, all of which reach the same module and all of which a
    refactor might reach for:

    ``import nodum.llm`` / ``import nodum.llm as anything``
        A plain import, aliased or not — the alias is irrelevant, the module is
        what matters.
    ``from nodum import llm``
        Names the submodule as an attribute of the package.
    ``from nodum.llm import chat``
        Names it as the module being imported from.
    ``from . import llm`` / ``from .llm import chat``
        The relative spellings. This package is flat, so one level is all there
        is, and a relative import is exactly as reaching as an absolute one.
    ``importlib.import_module("nodum.llm")`` / ``__import__("nodum.llm")``
        The dynamic spellings, which no AST walk over *imports* would see —
        which is precisely why a rail that only read ``ast.Import`` would be a
        rail with a documented way around it. **Positionally or by keyword**:
        both take ``name``, and a walk over ``node.args`` alone was blind to
        ``import_module(name="nodum.llm")`` — a constant string this claims to
        catch, spelled the way an IDE's signature help suggests it.
    ``nodum.llm.something`` after a bare ``import nodum``
        An attribute chain. It only resolves if something else has already
        imported the submodule, so it is not on its own a working import — but
        it is a *reach*, and the rail is about reaching.
    """
    tree = ast.parse(source)
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "nodum" or alias.name.startswith("nodum."):
                    reached.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Flat package: any level resolves to `nodum`.
                base = "nodum" if node.module is None else f"nodum.{node.module}"
            elif node.module == "nodum" or (node.module or "").startswith("nodum."):
                base = node.module or ""
            else:
                continue
            reached.add(base)
            if base == "nodum":
                reached |= {f"nodum.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            if name in {"import_module", "__import__"}:
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                for argument in arguments:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and (argument.value == "nodum" or argument.value.startswith("nodum."))
                    ):
                        reached.add(argument.value)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "nodum"
        ):
            reached.add(f"nodum.{node.attr}")
    return reached


def _import_graph() -> dict[str, set[str]]:
    """The package's own import graph, module name → the modules it reaches."""
    return {
        name: _nodum_imports(path.read_text(encoding="utf-8"))
        for name, path in _package_modules().items()
    }


def _reachable(start: str, graph: dict[str, set[str]]) -> dict[str, list[str]]:
    """Every module reachable from ``start``, with one path to each.

    Transitive, because "may not import" is only the direct half of the rule.
    A service layer that imported a helper that imported the provider would
    satisfy every one-hop check and still have a model inside validation.
    """
    paths = {start: [start]}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for neighbour in sorted(graph.get(current, set())):
            if neighbour not in paths:
                paths[neighbour] = [*paths[current], neighbour]
                frontier.append(neighbour)
    return paths


def test_the_rail_has_something_to_check():
    """A glob that matched nothing, or a renamed module, would make it vacuous."""
    modules = _package_modules()
    assert PROVIDER_MODULE in modules, "nodum.llm is gone; this rail guards nothing"
    assert set(modules) >= LLM_FREE_MODULES, sorted(LLM_FREE_MODULES - set(modules))
    graph = _import_graph()
    assert any(graph[name] for name in LLM_FREE_MODULES), (
        "no guarded module imports anything from nodum; the extractor is broken"
    )


@pytest.mark.parametrize("module", sorted(LLM_FREE_MODULES))
def test_the_deterministic_layer_cannot_reach_the_provider(module: str):
    """Design Constraint 4, structurally: validation cannot reach the model.

    Behaviour cannot catch this. A ``service.create_node`` that consulted a
    model would pass every test in the suite on a machine where the model
    happens to agree, and fail on the machine where it does not — which is the
    whole reason the constraint exists rather than being a habit.

    **What "cannot reach" means here, exactly**, so the claim does not outrun
    the check: every import spelling in
    :func:`test_the_rail_sees_every_spelling_of_the_import`, at any number of
    hops through the package's own modules. It does **not** see a module name
    assembled at runtime — ``exec``, ``eval``, ``getattr(importlib,
    "import_module")``, a string built from parts. That is the boundary of a
    static rail and it is stated rather than implied: this stops the reach a
    refactor makes by accident, which is the reach that actually happens, and
    not one somebody sets out to hide.
    """
    graph = _import_graph()
    paths = _reachable(module, graph)
    assert PROVIDER_MODULE not in paths, (
        f"{module} reaches {PROVIDER_MODULE} via {' -> '.join(paths[PROVIDER_MODULE])}: "
        "design Constraint 4 keeps the model out of validation, the state machine and "
        "the projectors"
    )


def test_the_rail_sees_every_spelling_of_the_import():
    """The detector's own coverage — one case per way to reach a module.

    A rail is only as strong as what it can see, and a rail that read
    ``ast.Import`` alone would have a documented way around it. Each line below
    is a spelling a refactor might reach for; the assertion is that the
    extractor sees all of them.
    """
    spellings = {
        "plain": "import nodum.llm",
        "aliased": "import nodum.llm as backend",
        "from-package": "from nodum import llm",
        "from-module": "from nodum.llm import get_provider",
        "from-module-aliased": "from nodum.llm import get_provider as resolve",
        "relative-package": "from . import llm",
        "relative-module": "from .llm import get_provider",
        "importlib": "import importlib\nbackend = importlib.import_module('nodum.llm')",
        "importlib-keyword": (
            "import importlib\nbackend = importlib.import_module(name='nodum.llm')"
        ),
        "dunder-import": "backend = __import__('nodum.llm')",
        "dunder-keyword": "backend = __import__(name='nodum.llm')",
        "attribute-chain": "import nodum\nbackend = nodum.llm.get_provider()",
    }
    blind = [
        name for name, source in spellings.items() if PROVIDER_MODULE not in _nodum_imports(source)
    ]
    assert blind == [], f"the rail cannot see these spellings: {blind}"


def test_the_rail_sees_a_re_export_placed_in_the_package_init():
    """``nodum/__init__.py`` is a node too, or a re-export there is invisible.

    Most modules here say ``from nodum import …`` somewhere, so the package node
    has an inbound edge from most of the graph. A glob that skips ``__init__.py``
    gives that node no *outbound* edges at all — and ``from nodum.llm import
    get_provider`` placed there, the one-line convenience re-export somebody
    reaches for eventually, would put the provider one hop from every guarded
    module while both properties in this file went on passing.

    The injection is what makes the claim checkable: the real ``__init__``
    correctly re-exports nothing, so an assertion over the real graph alone
    could never exercise this edge.
    """
    modules = _package_modules()
    assert "nodum" in modules, (
        "the package's own __init__.py is not a node in the graph, so a re-export "
        "placed there is invisible to both properties in this file"
    )
    assert modules["nodum"] == Path(nodum.__file__)

    graph = _import_graph()
    graph["nodum"] = graph["nodum"] | {PROVIDER_MODULE}
    paths = _reachable("nodum.service", graph)
    assert PROVIDER_MODULE in paths, (
        "nodum.service does not reach the package node at all, so the re-export above "
        "proves nothing; the extractor has stopped seeing `from nodum import …`"
    )


def test_the_rail_sees_a_reach_through_one_hop():
    """The transitive half, on a graph the test builds rather than the real one.

    The real graph is (correctly) clean, so a direct assertion over it can
    never exercise the multi-hop branch — a fixture that cannot express the
    failure is not coverage of it. This one can.
    """
    graph = {
        "nodum.service": {"nodum.helper"},
        "nodum.helper": {"nodum.llm"},
        "nodum.llm": set(),
    }
    paths = _reachable("nodum.service", graph)
    assert PROVIDER_MODULE in paths
    assert paths[PROVIDER_MODULE] == ["nodum.service", "nodum.helper", "nodum.llm"]


def test_only_the_agent_runtime_reaches_the_provider():
    """P3: :mod:`nodum.agent` is the only module that can reach the provider.

    Deliberately *reachability*, not call sites: this says no other module can
    get to the provider, which is what makes "every provider call goes through
    one entry point" true — a module that cannot import :mod:`nodum.llm` cannot
    call it. It does not inspect call expressions, and it would not catch
    ``nodum.agent`` itself growing a second entry point; that is what
    :func:`test_the_provider_offers_no_second_door` and the ordering property
    in ``tests/test_agent.py`` are for.
    """
    graph = _import_graph()
    importers = sorted(name for name, reached in graph.items() if PROVIDER_MODULE in reached)
    assert importers == ["nodum.agent"], (
        f"these modules import the provider directly: {importers}; "
        "every provider call goes through nodum.agent (design P3)"
    )


# ── The provider is a peer client too (P2) ────────────────────────────────────

#: Call names that mean somebody is talking to SQLite directly. The same
#: register ``tests/test_consolidate.py`` uses for the runner.
CONNECTION_CALLS = {
    "connect",
    "cursor",
    "execute",
    "executemany",
    "executescript",
    "commit",
    "init_db",
    "blobopen",
}


def _provider_ast() -> ast.Module:
    return ast.parse(Path(llm.__file__).read_text(encoding="utf-8"))


def test_the_provider_opens_no_connection_and_knows_no_principal():
    """It converts messages to text. The job holds the principal and the budget.

    A provider that read a row would be a second reader with no grant behind
    it; a provider that bound a principal would be a place where "who is
    answerable for this write" gets an extra hop.
    """
    tree = _provider_ast()
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "sqlite3" not in imported
    assert not any(name.startswith("nodum") for name in imported), (
        f"the provider imports from nodum: {sorted(n for n in imported if n.startswith('nodum'))}"
    )

    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert calls & CONNECTION_CALLS == set(), f"talks to SQLite: {sorted(calls & CONNECTION_CALLS)}"

    principals = [
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "principal"
    ]
    assert principals == [], "the provider binds a principal; the job holds the principal"


def test_the_provider_offers_no_second_door():
    """There is no module-level ``chat`` (P3): the one door is ``nodum.agent``."""
    assert not hasattr(llm, "chat"), (
        "nodum.llm grew a module-level chat(); every provider call goes through nodum.agent"
    )


# ── The token estimate never under-counts ─────────────────────────────────────
#
# Measured on `llama3.2:1b` over the OpenAI-compatible surface in this wave.
# `tokens` is the *marginal* prompt-token cost of the text — the chat template's
# own 25-token preamble subtracted — so it is what the content itself costs.

MEASURED_TOKEN_COSTS = [
    (
        "english",
        "The quick brown fox jumps over the lazy dog near the riverbank at dawn. " * 40,
        641,
    ),
    (
        "french",
        "Le renard brun rapide saute par-dessus le chien paresseux près de la rivière. " * 40,
        921,
    ),
    ("german", "Donaudampfschifffahrtsgesellschaftskapitaenswitwenrentenversicherung " * 40, 922),
    (
        "json",
        '{"id":"a3f9","title":"Compaction","props":{"kind":"claim","weight":0.5}} ' * 40,
        1001,
    ),
    ("cjk", "分散型コミットログはイベント駆動型システムの基盤です。" * 40, 800),
    ("emoji", "🌱🪴🌿🍃🌳🌲🌴🎋🎍🌵" * 40, 1200),
    ("hex-ids", "3f9a1c0e7b2d4856a1f0c3e9d7b52480 " * 40, 1120),
    ("accents-only", "éàèùçôîïüöäñÿœæ " * 160, 2561),
    ("arabic", "الكلاب الكسولة تقفز فوق الثعلب البني السريع. " * 40, 721),
    ("cyrillic", "Быстрая коричневая лиса прыгает через ленивую собаку. " * 40, 961),
]


@pytest.mark.parametrize(
    ("label", "text", "measured"),
    MEASURED_TOKEN_COSTS,
    ids=[row[0] for row in MEASURED_TOKEN_COSTS],
)
def test_the_estimate_never_undercounts_a_measured_prompt(label: str, text: str, measured: int):
    """The bound the refusal rests on, checked against the real tokeniser.

    An estimate that sometimes under-counts is an estimate that sometimes ships
    the silent truncation it exists to prevent. Byte count is an *upper* bound
    for a byte-level BPE tokeniser — every token decodes to at least one byte —
    and these ten samples are the empirical half of that argument.
    """
    assert llm.estimate_tokens(text) >= measured, (
        f"{label}: estimated {llm.estimate_tokens(text)} for a prompt that really costs {measured}"
    )


def test_a_character_based_estimate_would_have_undercounted():
    """Why the estimate counts bytes and not characters.

    The obvious estimator is ``len(text) / 4``. Measured, it under-counts emoji
    by twelve times and a run of accented Latin by four — and an under-count is
    the one failure this may not have. Stated as a test so that "simplify it to
    characters" fails here rather than in a truncated answer nobody can spot.
    """
    undercounted = [
        label for label, text, measured in MEASURED_TOKEN_COSTS if len(text) // 4 < measured
    ]
    assert "emoji" in undercounted and "accents-only" in undercounted, (
        f"a chars/4 estimate under-counts {undercounted}; if that list is empty the "
        "measurement table has been broken"
    )


def test_the_estimate_charges_for_every_message_and_its_wrapping():
    """A chat template spends tokens per message, not only on content."""
    one = llm.estimate_prompt_tokens([llm.Message(role="user", content="hello")])
    two = llm.estimate_prompt_tokens(
        [llm.Message(role="system", content="hi"), llm.Message(role="user", content="hello")]
    )
    assert one >= llm.TEMPLATE_OVERHEAD_TOKENS + llm.MESSAGE_OVERHEAD_TOKENS
    assert two > one


def test_the_estimate_of_an_empty_prompt_is_the_template_overhead():
    assert llm.estimate_prompt_tokens([]) == llm.TEMPLATE_OVERHEAD_TOKENS


# ── A fake wire ───────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: bytes, *, read_raises: BaseException | None = None) -> None:
        self._body = body
        self._read_raises = read_raises

    def read(self) -> bytes:
        if self._read_raises is not None:
            raise self._read_raises
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Wire:
    """Records what was sent and replays a scripted answer.

    ``raises`` is a connection that never produced a response; ``read_raises``
    is a response whose *body* died halfway — two different deaths, and the
    second one only exists on this side of ``urlopen``.
    """

    def __init__(
        self,
        answer: Any = None,
        *,
        raises: BaseException | None = None,
        read_raises: BaseException | None = None,
    ) -> None:
        self.answer = answer
        self.raises = raises
        self.read_raises = read_raises
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        body = self.answer if isinstance(self.answer, bytes) else json.dumps(self.answer).encode()
        return _FakeResponse(body, read_raises=self.read_raises)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].data)


def _answer(
    *,
    text: str = "ok",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    finish_reason: str = "stop",
    reasoning_tokens: int | None = None,
    cache_hit: int | None = None,
    cache_miss: int | None = None,
) -> dict[str, Any]:
    """The OpenAI-compatible shape, exactly as a live server returns it.

    The three optional blocks are **omitted when not asked for**, which is the
    ollama shape and also ``deepseek-v4-flash``'s own shape at
    ``reasoning_effort: "none"`` — measured, the whole
    ``completion_tokens_details`` key disappears rather than reporting zero. A
    builder that always emitted them could not express the absence, and the
    absence is the case a defensive read gets wrong.
    """
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    if cache_hit is not None:
        usage["prompt_cache_hit_tokens"] = cache_hit
    if cache_miss is not None:
        usage["prompt_cache_miss_tokens"] = cache_miss
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}
        ],
        "usage": usage,
    }


@pytest.fixture()
def wire(monkeypatch):
    """Replace the transport; return the recorder."""

    def install(
        answer: Any = None,
        *,
        raises: BaseException | None = None,
        read_raises: BaseException | None = None,
    ) -> _Wire:
        recorder = _Wire(
            answer if answer is not None else _answer(), raises=raises, read_raises=read_raises
        )
        monkeypatch.setattr(urllib.request, "urlopen", recorder)
        return recorder

    return install


def _provider(**overrides: Any) -> llm.OpenAICompatProvider:
    settings: dict[str, Any] = {"model": "test-model", "context_tokens": 4096}
    settings.update(overrides)
    return llm.OpenAICompatProvider(**settings)


# ── Refusal before the call ───────────────────────────────────────────────────


def test_an_over_long_prompt_is_refused_before_anything_is_sent(wire):
    """The finding this interface exists for.

    A 64 000-character prompt and a 70 000-character one both come back with
    4 096 prompt tokens and ``finish_reason: "stop"``. The model does not say it
    dropped the input; ``usage`` shows it only after the call is paid for. So
    the count happens before the request, and the request is not made.
    """
    recorder = wire()
    provider = _provider(context_tokens=1000)
    with pytest.raises(llm.PromptTooLong) as refusal:
        provider.chat(
            [llm.Message(role="user", content="x" * 5000)],
            max_output_tokens=100,
            timeout=5.0,
        )
    assert recorder.requests == [], "the prompt was sent despite being refused"
    assert "900" in str(refusal.value), "the refusal does not say how much would have fit"


def test_the_output_reservation_comes_out_of_the_window(wire):
    """A prompt that fits the window but not window-minus-answer is refused.

    The window holds the prompt *and* the answer; a check against the window
    alone would leave a prompt that fits and an answer that cannot.
    """
    recorder = wire()
    provider = _provider(context_tokens=1000)
    content = "x" * 900
    # Fits with a small reservation…
    provider.chat([llm.Message(role="user", content=content)], max_output_tokens=40, timeout=5.0)
    assert len(recorder.requests) == 1
    # …and not with a large one.
    with pytest.raises(llm.PromptTooLong):
        provider.chat(
            [llm.Message(role="user", content=content)], max_output_tokens=500, timeout=5.0
        )
    assert len(recorder.requests) == 1


def test_an_output_ceiling_that_would_fill_the_window_is_capped_at_a_share_of_it(wire):
    """Was a ``ValueError``; is now a cap, and the change is the point.

    A flat ``window - max_output_tokens`` meant the *absolute* output ceiling
    decided how much prompt fitted, so one number had to suit a 4 096-token
    ollama window and a 1 000 000-token remote one at the same time. It cannot:
    the ceiling that a reasoning model needs (measured worst case 1 520 output
    tokens on a five-note synthesis, 1 277 on a one-word prompt) is the whole of
    the small window, and the same number against the large one reserves 0.4 %
    and guards nothing.

    So the reservation is a share — at most :data:`llm.OUTPUT_RESERVATION_FRACTION`
    of the window — and the clamped number is what is **sent**, which is what
    keeps it safe: the server is never told it may generate into space the
    prompt was allowed to occupy.
    """
    recorder = wire()
    # 200, not 100: the estimate charges 54 tokens of fixed template and role
    # overhead before any content, so a 100-token window has no room for a
    # prompt whatever the reservation does, and the row would pass for the
    # wrong reason.
    provider = _provider(context_tokens=200)
    completion = provider.chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=200, timeout=5.0
    )
    assert provider.output_reservation(200) == 100
    assert recorder.payload["max_tokens"] == 100, (
        "the clamped reservation must also be the ceiling the server is given, or the "
        "server may generate past what was reserved"
    )
    assert completion.output_ceiling == 100, "the completion must report the ceiling that bit"


def test_a_ceiling_below_the_share_is_sent_untouched(wire):
    """The cap only ever lowers, and only when the ask exceeds the share.

    Without this row the test above passes on an implementation that always
    reserved half the window and ignored the caller entirely.
    """
    recorder = wire()
    provider = _provider(context_tokens=4096)
    provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=512, timeout=5.0)
    assert provider.output_reservation(512) == 512
    assert recorder.payload["max_tokens"] == 512


def test_a_zero_output_ceiling_is_a_value_error(wire):
    wire()
    with pytest.raises(ValueError, match="at least 1"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=0, timeout=5.0)


def test_a_zero_context_window_is_a_value_error():
    with pytest.raises(ValueError, match="at least 1"):
        _provider(context_tokens=0)


# ── The wire shape ────────────────────────────────────────────────────────────


def test_usage_and_finish_reason_come_back_verbatim(wire):
    recorder = wire(_answer(text="hello", prompt_tokens=91, completion_tokens=7))
    completion = _provider().chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=64, timeout=5.0
    )
    assert completion.text == "hello"
    assert (completion.prompt_tokens, completion.output_tokens) == (91, 7)
    assert completion.total_tokens == 98
    assert completion.finish_reason == "stop"
    assert completion.model_id == "test-model"
    assert completion.provider_id == llm.DEFAULT_BASE_URL
    assert completion.context_tokens == 4096
    assert completion.latency_ms >= 0
    assert recorder.payload["model"] == "test-model"
    assert recorder.payload["messages"] == [{"role": "user", "content": "hi"}]
    assert recorder.payload["max_tokens"] == 64
    assert recorder.timeouts == [5.0]


def test_temperature_is_pinned_at_zero(wire):
    recorder = wire()
    _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert recorder.payload["temperature"] == 0.0
    assert llm.TEMPERATURE == 0.0


def test_a_schema_becomes_a_json_schema_response_format(wire):
    """Structured output is real and worth using: five free-form probes produced
    one unparseable body, the same five under a schema produced none."""
    recorder = wire()
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    _provider().chat(
        [llm.Message(role="user", content="hi")],
        schema=schema,
        max_output_tokens=8,
        timeout=5.0,
    )
    assert recorder.payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "nodum", "schema": schema},
    }


def test_no_schema_sends_no_response_format(wire):
    recorder = wire()
    _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert "response_format" not in recorder.payload


def test_an_api_key_becomes_a_bearer_header_and_no_key_sends_none(wire):
    recorder = wire()
    _provider(api_key="sk-secret").chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
    )
    assert recorder.requests[-1].get_header("Authorization") == "Bearer sk-secret"

    recorder = wire()
    _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert recorder.requests[-1].get_header("Authorization") is None


def test_a_trailing_slash_on_the_base_url_does_not_double(wire):
    recorder = wire()
    _provider(base_url="http://host:1234/v1/").chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
    )
    assert recorder.requests[-1].full_url == "http://host:1234/v1/chat/completions"


# ── Failure shapes ────────────────────────────────────────────────────────────


def test_an_http_error_is_unavailable_and_carries_what_the_server_said(wire):
    """Measured: an unknown model on a live server is a 404 with a JSON body in
    about a millisecond, and that body is the only thing naming the mistake."""
    failure = urllib.error.HTTPError(
        url="http://x/v1/chat/completions",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=None,
    )
    failure.read = lambda: b'{"error":{"message":"model \'nope\' not found"}}'  # type: ignore[method-assign]
    wire(raises=failure)
    with pytest.raises(llm.ProviderUnavailable, match="404"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)


def test_a_refused_connection_is_unavailable_and_names_the_endpoint(wire):
    wire(raises=urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")))
    with pytest.raises(llm.ProviderUnavailable, match="not reachable"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)


@pytest.mark.parametrize(
    "raised",
    [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))],
    ids=["bare", "wrapped-in-urlerror"],
)
def test_a_timeout_is_its_own_failure(wire, raised: BaseException):
    """A timeout and a dead server call for different responses, so they are
    different classes. The wrapped spelling is not hypothetical: ``urllib``
    reports a socket timeout as a ``URLError`` whose ``reason`` is the
    ``TimeoutError``, and a check that only caught the bare one would report a
    slow model as a missing server."""
    wire(raises=raised)
    with pytest.raises(llm.ProviderTimeout, match="did not answer within"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert issubclass(llm.ProviderTimeout, llm.ProviderUnavailable)


def test_a_provider_that_dies_mid_response_is_a_provider_failure_and_not_a_traceback(wire):
    """The shape a killed provider, a proxy timeout or a dropped load balancer makes.

    ``response.read()`` raises :class:`http.client.IncompleteRead` when the body
    stops short of its ``Content-Length``. That class derives from
    ``HTTPException``, **not** from ``OSError``, so the clause that catches a
    refused connection misses it entirely: it escaped :class:`~nodum.llm.LLMError`,
    escaped ``answers.ask``'s handler and escaped ``cli._run`` — reproduced as a
    Rich traceback and exit 1 on the CLI, and an HTTP 500 on ``POST /api/ask``.
    Every other way for a provider to die here is already clean, which is what
    made this one worth a test of its own.
    """
    assert not issubclass(http.client.HTTPException, OSError), (
        "the hierarchy this test exists for has changed; re-check the except clauses"
    )
    wire(read_raises=http.client.IncompleteRead(b"x" * 42, 358))
    with pytest.raises(llm.ProviderUnavailable, match="not reachable"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)


def test_every_way_a_provider_can_die_here_is_an_llm_error(wire):
    """The property the CLI and the HTTP surface both rest on.

    ``answers.ask`` turns an :class:`~nodum.llm.LLMError` into a 200 with a
    refusal and ``cli._run`` turns it into a named failure; anything else is a
    traceback. So each death shape below is pinned as a *subclass check* rather
    than as a message.

    Each case's outcome is **recorded rather than skipped**, and a healthy wire
    rides along expecting the opposite outcome, because "nothing escaped" is
    also what a loop that raised nothing at all would report: a death shape that
    quietly produced a completion, or a fixture that never installed itself,
    would leave an escapes-only list empty and pass on a wire nothing was wrong
    with. The control row is what proves this table can express both answers.
    """
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("healthy", {}, "no exception at all"),
        (
            "refused",
            {"raises": urllib.error.URLError(ConnectionRefusedError(111, "refused"))},
            "LLMError",
        ),
        ("timeout", {"raises": TimeoutError("timed out")}, "LLMError"),
        ("reset", {"raises": ConnectionResetError(104, "Connection reset by peer")}, "LLMError"),
        ("half-read", {"read_raises": http.client.IncompleteRead(b"x" * 42, 358)}, "LLMError"),
        ("bad-status", {"read_raises": http.client.BadStatusLine("garbage")}, "LLMError"),
        ("not-json", {"answer": b"<html>gateway timeout</html>"}, "LLMError"),
    ]
    outcomes = {}
    for label, kwargs, _ in cases:
        options = dict(kwargs)
        answer = options.pop("answer", None)
        wire(answer, **options)
        try:
            _provider().chat(
                [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
            )
        except llm.LLMError:
            outcomes[label] = "LLMError"
        except BaseException as failure:  # noqa: BLE001 — that is the finding
            outcomes[label] = f"escaped: {type(failure).__name__}"
        else:
            outcomes[label] = "no exception at all"
    assert outcomes == {label: expected for label, _, expected in cases}, (
        f"a provider death that does not reach the caller as an LLMError: {outcomes}"
    )


@pytest.mark.parametrize(
    ("base_url", "detail"),
    [
        ("api.deepseek.com/v1", "unknown url type"),
        ("http://[bad/v1", "Invalid IPv6 URL"),
    ],
    ids=["no-scheme", "bad-ipv6"],
)
def test_an_unpostable_base_url_is_an_llm_error_and_never_a_value_error(
    wire, base_url: str, detail: str
):
    """The hole the table above could not see, because it holds one base URL.

    ``urllib.request.Request`` parses the URL **in its constructor**, so it is
    the second thing in ``_post`` that raises on bad configuration rather than
    on a bad network — and it was built one line *outside* the ``try``, where no
    except clause could reach it. Driven: ``NODUM_LLM_BASE_URL=api.deepseek.com/v1``
    gave ``ValueError: unknown url type`` and ``http://[bad/v1`` gave
    ``ValueError: Invalid IPv6 URL``, both straight past every handler here.

    That is the same shape ``IncompleteRead`` had one layer down, with the same
    consequences: ``answers.ask`` catches ``(BudgetExhausted, LLMError)``, so a
    malformed *configuration* surfaced as the traceback and the 400 kept for a
    malformed *request*.

    Resolution now refuses these spellings before a provider exists, so this
    class can only be reached by constructing it directly — which is exactly why
    the guard belongs here too: :class:`~nodum.llm.OpenAICompatProvider` is
    public, takes any string, and ``LLMError`` is its total contract.
    """
    recorder = wire()
    provider = _provider(base_url=base_url)
    with pytest.raises(llm.ProviderUnavailable) as raised:
        provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert base_url in str(raised.value), "the failure must name the endpoint that is wrong"
    assert detail in str(raised.value)
    assert raised.value.status is None, (
        "a URL that was never sent has no HTTP status, so `_negotiate` must not "
        "be able to read it as a capability signal"
    )
    assert recorder.requests == [], "nothing may reach the transport"


def test_a_body_that_is_not_json_is_unavailable(wire):
    wire(b"<html>gateway timeout</html>")
    with pytest.raises(llm.ProviderUnavailable, match="not JSON"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)


def test_a_json_body_that_is_not_an_object_is_unavailable(wire):
    wire([1, 2, 3])
    with pytest.raises(llm.ProviderUnavailable, match="not an object"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("no-usage", {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}),
        ("no-choices", {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
        ("empty-choices", {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
        (
            "usage-not-a-number",
            {
                "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": "lots", "completion_tokens": 1},
            },
        ),
    ],
)
def test_a_shape_this_cannot_read_is_unavailable_and_never_a_free_call(
    wire, label: str, body: dict
):
    """A 200 with a shape the reader does not recognise is *not* a completion
    with zeros in it. A silently zeroed ``usage`` would be a call that cost
    nothing according to the budget, which is the one lie a meter may not tell."""
    wire(body)
    with pytest.raises(llm.ProviderUnavailable, match="cannot read"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)


# ── What a completion says about itself ───────────────────────────────────────


def test_an_output_ceiling_is_reported_as_a_length_finish(wire):
    """Measured: under a schema with ``max_tokens=8`` the body is
    ``'{\\n  "title": "Kafka'`` — cut mid-string, with ``finish_reason:
    "length"``. The provider carries the fact; classifying it as a *failed*
    call is :mod:`nodum.agent`'s job, because that is where the budget the
    failed call must still be charged against lives."""
    wire(_answer(text='{\n  "title": "Kafka', finish_reason="length"))
    completion = _provider().chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
    )
    assert completion.output_truncated is True
    assert completion.context_filled is False
    with pytest.raises(json.JSONDecodeError):
        json.loads(completion.text)


def test_a_filled_context_is_visible_only_in_the_prompt_token_count(wire):
    """``finish_reason`` was ``"stop"`` on a prompt truncated from 70 000
    characters to 4 096 tokens — measured. So the count is the only signal."""
    wire(_answer(prompt_tokens=4096, finish_reason="stop"))
    completion = _provider(context_tokens=4096).chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
    )
    assert completion.context_filled is True
    assert completion.output_truncated is False


def test_a_completion_well_inside_the_window_says_so(wire):
    wire(_answer(prompt_tokens=91))
    completion = _provider(context_tokens=4096).chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
    )
    assert completion.context_filled is False
    assert completion.prompt_truncated is False


# ── The truncation the configured window cannot see ───────────────────────────


def test_a_window_configured_above_the_serving_one_hides_the_truncation_from_context_filled(wire):
    """The hole, reproduced: the binding limit is the **server's**, not the model's.

    Measured with ``NODUM_LLM_CONTEXT_TOKENS=32768`` against ollama, which
    serves every model at ``num_ctx`` 4 096 unless ``OLLAMA_CONTEXT_LENGTH``
    says otherwise — while ``llama3.2:1b`` really does have a 128 k window, so
    the docstring's "raise it only for a model that really has the room" is
    advice that produces exactly this. A 30 000-character prompt is not refused,
    ``prompt_tokens`` comes back 4 096, ``finish_reason`` is ``"stop"``, and
    :attr:`~nodum.llm.Completion.context_filled` is **false**, because 4 096 is
    nowhere near 32 768. A whole answer is returned from a prefix with nothing
    saying so.
    """
    wire(_answer(prompt_tokens=4096, finish_reason="stop"))
    completion = _provider(context_tokens=32768).chat(
        [llm.Message(role="user", content="Kafka is a distributed commit log. " * 857)],
        max_output_tokens=512,
        timeout=5.0,
    )
    assert completion.context_filled is False, (
        "context_filled compares the report against the *configured* number, so it "
        "cannot see this case — that is the finding, not a regression"
    )
    assert completion.prompt_truncated is True, (
        "the server read 4 096 tokens of a prompt that cannot cost fewer than "
        f"{completion.prompt_estimate // llm.MAX_BYTES_PER_TOKEN}, and nothing said so"
    )


@pytest.mark.parametrize(
    ("label", "text", "measured"),
    MEASURED_TOKEN_COSTS,
    ids=[row[0] for row in MEASURED_TOKEN_COSTS],
)
def test_a_prompt_the_server_read_whole_is_never_flagged_as_truncated(
    wire, label: str, text: str, measured: int
):
    """The other half, and the one that decides :data:`~nodum.llm.MAX_BYTES_PER_TOKEN`.

    The estimate over-counts — that is what makes it a *bound* — so "below the
    estimate" is true of nearly every honest call and would flag them all. What
    is checkable is "below the fewest tokens this many bytes can possibly be",
    and how few that is depends on the script. These are the same ten measured
    prompts the estimate is calibrated against, replayed with the token count
    the real tokeniser charged (plus the chat template's measured 25-token
    preamble): the loosest is Arabic at 4.55 bytes per token, and a constant
    tighter than that would call a perfectly whole prompt truncated.
    """
    wire(_answer(prompt_tokens=measured + 25))
    completion = _provider(context_tokens=200_000).chat(
        [llm.Message(role="user", content=text)], max_output_tokens=8, timeout=5.0
    )
    assert completion.prompt_truncated is False, (
        f"{label}: a whole prompt of {len(text.encode())} bytes really costing "
        f"{measured} tokens was called truncated; MAX_BYTES_PER_TOKEN is too tight"
    )


def test_the_estimate_the_refusal_used_travels_back_on_the_completion(wire):
    """How the estimate reaches :class:`~nodum.llm.Completion`: the provider puts it
    there, because the provider is the only thing that has it — it computes the
    number for the pre-send refusal one line earlier, and a caller recomputing it
    would be a second estimator that can disagree with the one that decided."""
    wire(_answer(prompt_tokens=91))
    messages = [llm.Message(role="user", content="hi")]
    completion = _provider().chat(messages, max_output_tokens=8, timeout=5.0)
    assert completion.prompt_estimate == llm.estimate_prompt_tokens(messages)


def test_a_completion_with_no_recorded_estimate_claims_no_truncation():
    """0 means "nobody measured", not "a prompt of nothing" — a hand-built
    completion (a test fake, a replayed row) must not read as truncated."""
    completion = llm.Completion(
        text="ok",
        prompt_tokens=4096,
        output_tokens=5,
        finish_reason="stop",
        model_id="m",
        provider_id="p",
        context_tokens=32768,
        latency_ms=1,
    )
    assert completion.prompt_estimate == 0
    assert completion.prompt_truncated is False


# ── Resolution ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_resolution(monkeypatch):
    """Every test starts with an unresolved provider and a clean environment."""
    for name in (
        llm.ENV_MODEL,
        llm.ENV_BASE_URL,
        llm.ENV_API_KEY,
        llm.ENV_CONTEXT_TOKENS,
        llm.ENV_THINKING,
    ):
        monkeypatch.delenv(name, raising=False)
    llm.reset_provider()
    yield
    llm.reset_provider()


def test_no_model_name_means_no_provider_and_the_reason_names_the_variable():
    """D10's "no key = smart features off", generalised: no provider = off."""
    assert llm.get_provider() is None
    reason = llm.unavailable_reason()
    assert reason is not None
    assert llm.ENV_MODEL in reason
    assert llm.DEFAULT_BASE_URL in reason


def test_a_model_name_alone_resolves_the_local_default(monkeypatch):
    monkeypatch.setenv(llm.ENV_MODEL, "llama3.2:1b")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.model_id == "llama3.2:1b"
    assert provider.provider_id == llm.DEFAULT_BASE_URL
    assert provider.context_tokens == llm.DEFAULT_CONTEXT_TOKENS
    assert llm.unavailable_reason() is None


def test_a_blank_model_name_is_not_a_provider(monkeypatch):
    """``NODUM_LLM_MODEL=`` and ``NODUM_LLM_MODEL='  '`` are both "unset"."""
    monkeypatch.setenv(llm.ENV_MODEL, "   ")
    assert llm.get_provider() is None


def test_the_remote_half_is_the_same_class(monkeypatch):
    """One implementation covers both halves — that is the whole of P1's size."""
    monkeypatch.setenv(llm.ENV_MODEL, "gpt-4o-mini")
    monkeypatch.setenv(llm.ENV_BASE_URL, "https://api.example.com/v1")
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-remote")
    monkeypatch.setenv(llm.ENV_CONTEXT_TOKENS, "128000")
    provider = llm.get_provider()
    assert isinstance(provider, llm.OpenAICompatProvider)
    assert provider.provider_id == "https://api.example.com/v1"
    assert provider.context_tokens == 128000


def test_an_unparseable_context_window_is_no_provider_with_a_reason(monkeypatch):
    monkeypatch.setenv(llm.ENV_MODEL, "llama3.2:1b")
    monkeypatch.setenv(llm.ENV_CONTEXT_TOKENS, "lots")
    assert llm.get_provider() is None
    assert "lots" in (llm.unavailable_reason() or "")


def test_a_context_window_below_one_is_no_provider(monkeypatch):
    monkeypatch.setenv(llm.ENV_MODEL, "llama3.2:1b")
    monkeypatch.setenv(llm.ENV_CONTEXT_TOKENS, "0")
    assert llm.get_provider() is None
    assert "unusable" in (llm.unavailable_reason() or "")


def test_resolution_makes_no_network_call(monkeypatch):
    """Where this deliberately differs from the embedding seam.

    ``embeddings.get_provider`` loads a model, so an unusable model fails at
    resolution. Here "configured" and "reachable" are different facts — a
    server down at 03:00 and up at 03:05 is not a configuration change — and a
    probe would cache one instant's answer for the life of the process.
    """

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("resolution made a network call")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    monkeypatch.setenv(llm.ENV_MODEL, "llama3.2:1b")
    assert llm.get_provider() is not None


def test_resolution_is_cached_and_reset_re_resolves(monkeypatch):
    monkeypatch.setenv(llm.ENV_MODEL, "first")
    first = llm.get_provider()
    monkeypatch.setenv(llm.ENV_MODEL, "second")
    assert llm.get_provider() is first, "resolution is not cached"
    llm.reset_provider()
    assert (llm.get_provider() or first).model_id == "second"


def test_set_provider_is_the_seam_and_none_forces_the_absence():
    sentinel = _provider(model="injected")
    llm.set_provider(sentinel)
    assert llm.get_provider() is sentinel
    assert llm.unavailable_reason() is None
    llm.set_provider(None, reason="test default: no LLM provider")
    assert llm.get_provider() is None
    assert llm.unavailable_reason() == "test default: no LLM provider"


# ── The one real-model test (opt-in) ──────────────────────────────────────────


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
def test_the_shipped_provider_drives_the_live_model():
    """Structure only — never output text.

    Temperature-0 determinism was measured on this backend and is a property of
    *that* backend; a remote provider makes no such promise, so an assertion on
    what the model said would be a test of the weather. What is asserted is the
    contract this module rests on: a usage block with both counts, a
    ``finish_reason``, and a schema that produces a parseable envelope.
    """
    model = os.environ.get("NODUM_LLM_MODEL", "llama3.2:1b")
    provider = llm.OpenAICompatProvider(model=model)
    completion = provider.chat(
        [llm.Message(role="user", content="Name one colour. One word.")],
        max_output_tokens=16,
        timeout=300.0,
    )
    assert completion.prompt_tokens > 0
    assert completion.output_tokens > 0
    assert completion.total_tokens == completion.prompt_tokens + completion.output_tokens
    assert completion.finish_reason in {"stop", "length"}
    assert completion.model_id == model
    assert completion.latency_ms >= 0

    schema = {
        "type": "object",
        "properties": {"colour": {"type": "string"}},
        "required": ["colour"],
        "additionalProperties": False,
    }
    structured = provider.chat(
        [llm.Message(role="user", content="Name one colour as JSON.")],
        schema=schema,
        max_output_tokens=64,
        timeout=300.0,
    )
    assert isinstance(json.loads(structured.text), dict), (
        "a schema makes the envelope reliable — it makes the content no truer at all"
    )


@pytest.mark.skipif(
    os.environ.get("NODUM_RUN_SLOW") != "1",
    reason="real-model smoke test: set NODUM_RUN_SLOW=1 (needs a model serving)",
)
@pytest.mark.skipif(not _live_server_is_up(), reason="no OpenAI-compatible server on :11434")
def test_the_live_model_still_truncates_silently_when_the_window_is_misconfigured():
    """The measurement the refusal exists for, reproduced end to end.

    Configure a window wider than the one the server *serves* — the operator
    mistake ``NODUM_LLM_CONTEXT_TOKENS`` invites, and the one ollama makes easy
    by serving every model at ``num_ctx`` 4 096 whatever its card says — and the
    refusal passes, the server drops what did not fit, and **nothing in the
    response body says so**. ``context_filled`` cannot see it either, because it
    compares the report against that same wrong number; ``prompt_truncated``
    can, because it compares against what the bytes really cost.

    Note what is deliberately *not* asserted: ``finish_reason``. It describes
    the output and only the output. This call comes back ``"length"`` because
    the eight-token answer ceiling bit; the same call with a generous ceiling
    comes back ``"stop"``. Neither value is about the 66 000 characters the
    server never read — and reading it as an input signal is exactly the
    mistake this interface exists to make impossible. (The first cut of this
    test asserted ``"stop"``, carried from a probe that happened to finish
    early, and the live model failed it.)
    """
    model = os.environ.get("NODUM_LLM_MODEL", "llama3.2:1b")
    real_window = int(os.environ.get("NODUM_LLM_CONTEXT_TOKENS", "4096"))
    prompt = [llm.Message(role="user", content="Kafka is a distributed commit log. " * 2000)]

    misconfigured = llm.OpenAICompatProvider(model=model, context_tokens=1_000_000)
    completion = misconfigured.chat(prompt, max_output_tokens=8, timeout=900.0)
    assert completion.prompt_tokens <= real_window, (
        "the prompt was not truncated; this model's window is wider than "
        "NODUM_LLM_CONTEXT_TOKENS says"
    )
    assert completion.context_filled is False, (
        "the configured-window check cannot see this truncation, which is the hole"
    )
    assert completion.prompt_truncated is True, (
        "the estimate-based check is what closes it: the server read "
        f"{completion.prompt_tokens} tokens of a prompt that cannot cost fewer than "
        f"{completion.prompt_estimate // llm.MAX_BYTES_PER_TOKEN}"
    )

    # Configured honestly, the same prompt never leaves the process.
    honest = llm.OpenAICompatProvider(model=model, context_tokens=real_window)
    with pytest.raises(llm.PromptTooLong):
        honest.chat(prompt, max_output_tokens=8, timeout=1.0)


# ── Reasoning models: what `usage` carries, and what it costs ─────────────────
#
# Measured against `deepseek-v4-flash` over the live API, not reasoned about.
# Three facts shape everything below:
#
#   1. `usage.completion_tokens_details.reasoning_tokens` is a **subset** of
#      `completion_tokens`, never an addition — `total_tokens` is
#      `prompt + completion` on every call measured.
#   2. At `reasoning_effort: "none"` the whole `completion_tokens_details` block
#      is **absent from `usage`**, so a reader that indexes into it raises on the
#      one setting whose cost is predictable.
#   3. Prompt caching is reported as `prompt_cache_hit_tokens` /
#      `prompt_cache_miss_tokens`, and the two sum to `prompt_tokens`.


def test_reasoning_tokens_come_off_the_wire_and_are_a_share_of_the_output(wire):
    """A reasoning model's thinking is billed inside ``completion_tokens``.

    So this is *not* added to :attr:`Completion.total_tokens` — doing that would
    double-charge the one number budgets are denominated in. It is carried
    separately because a night that spent 90 % of its output allowance thinking
    and 10 % writing is a different night from one that wrote the whole time,
    and the report is the only place a human can tell them apart.
    """
    recorder = wire(_answer(prompt_tokens=100, completion_tokens=80, reasoning_tokens=60))
    completion = _provider().chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=512, timeout=5.0
    )
    assert completion.reasoning_tokens == 60
    assert completion.output_tokens == 80, "reasoning must not be added to the output count"
    assert completion.total_tokens == 180, "reasoning must not be double-charged"
    assert recorder.payload["max_tokens"] == 512


def test_a_usage_block_with_no_details_reports_zero_reasoning_rather_than_raising(wire):
    """``reasoning_effort: "none"`` drops ``completion_tokens_details`` entirely.

    Measured on the live API across nine independent calls: at ``none`` the key
    is not present at all — not present-and-zero. A reader that indexed into it
    would turn the one predictable setting into a ``ProviderUnavailable``.
    """
    answer = _answer(prompt_tokens=10, completion_tokens=5)
    assert "completion_tokens_details" not in answer["usage"], "fixture built wrong"
    wire(answer)
    completion = _provider().chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=64, timeout=5.0
    )
    assert completion.reasoning_tokens == 0


def test_the_prompt_cache_counters_come_off_the_wire(wire):
    """Prompt caching is priced 50x cheaper and matches on prefix, so a report
    that cannot say how much of a prompt was cached cannot say what a night cost."""
    wire(_answer(prompt_tokens=154, completion_tokens=25, cache_hit=128, cache_miss=26))
    completion = _provider().chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=64, timeout=5.0
    )
    assert (completion.cache_hit_tokens, completion.cache_miss_tokens) == (128, 26)
    assert completion.cache_hit_tokens + completion.cache_miss_tokens == completion.prompt_tokens


def test_a_wire_with_no_cache_counters_reports_zero(wire):
    """ollama returns neither counter; absence is 0 hits, not an unreadable shape."""
    answer = _answer()
    assert "prompt_cache_hit_tokens" not in answer["usage"], "fixture built wrong"
    wire(answer)
    completion = _provider().chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=64, timeout=5.0
    )
    assert (completion.cache_hit_tokens, completion.cache_miss_tokens) == (0, 0)


# ── Capability negotiation: json_schema → json_object ─────────────────────────


def _rejects_then(
    detail: str, answer: dict[str, Any] | None = None, *, first_attempt_seconds: float = 0.0
):
    """A wire that answers HTTP 400 once with ``detail``, then succeeds.

    Built as *two scripted responses* rather than by asking the implementation
    what it would send, because a fixture assembled from the code's own
    predicates would only prove the grammar equals itself. ``detail`` is the
    server's real sentence, copied from a live 400.

    ``first_attempt_seconds`` makes the refused attempt take real wall clock.
    Without it every attempt returns in microseconds, and "the re-send is
    charged what the first attempt spent" cannot be told apart from "the re-send
    is charged an arbitrary shave off the ceiling" — the timeouts differ in the
    fifth decimal either way. ``started_at`` records when each attempt was
    handed to the transport, which is what turns the two into different numbers.
    """

    class _Negotiating:
        def __init__(self) -> None:
            self.requests: list[urllib.request.Request] = []
            self.timeouts: list[float] = []
            self.started_at: list[float] = []
            self.rejected_once = False

        def __call__(self, request, timeout=None):
            self.requests.append(request)
            self.timeouts.append(timeout)
            self.started_at.append(time.monotonic())
            if not self.rejected_once:
                self.rejected_once = True
                if first_attempt_seconds:
                    time.sleep(first_attempt_seconds)
                failure = urllib.error.HTTPError(
                    url="http://x/v1/chat/completions",
                    code=400,
                    msg="Bad Request",
                    hdrs=None,
                    fp=None,
                )
                body = json.dumps({"error": {"message": detail}}).encode()
                failure.read = lambda: body  # type: ignore[method-assign]
                raise failure
            return _FakeResponse(json.dumps(answer or _answer()).encode())

        @property
        def payloads(self) -> list[dict[str, Any]]:
            return [json.loads(request.data) for request in self.requests]

    return _Negotiating()


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "cited": {"type": "array"}},
    "required": ["answer", "cited"],
}


def test_a_provider_that_refuses_json_schema_falls_back_to_json_object(monkeypatch):
    """Measured: ``deepseek-v4-flash`` answers ``HTTP 400 {"error":{"message":
    "This response_format type is unavailable now"}}`` to a ``json_schema``
    request and accepts ``json_object``.

    Without the fallback every structured call on that provider — which is every
    call ``/ask`` and ``/summarize`` make — is a hard failure out of the box.
    """
    recorder = _rejects_then("This response_format type is unavailable now")
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    completion = provider.chat(
        [llm.Message(role="user", content="hi")],
        schema=SCHEMA,
        max_output_tokens=512,
        timeout=30.0,
    )
    assert len(recorder.requests) == 2, "the call was not re-sent under the weaker form"
    assert recorder.payloads[0]["response_format"]["type"] == llm.STRUCTURED_JSON_SCHEMA
    assert recorder.payloads[1]["response_format"] == {"type": llm.STRUCTURED_JSON_OBJECT}
    assert completion.structured_mode == llm.STRUCTURED_JSON_OBJECT


def test_the_fallback_is_detected_once_and_remembered(monkeypatch):
    """A failed round trip per call would double the cost of every call.

    So the belief is downgraded **on the instance**: the second call goes
    straight out under ``json_object`` with no rejected attempt at all.
    """
    recorder = _rejects_then("This response_format type is unavailable now")
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    prompt = [llm.Message(role="user", content="hi")]
    provider.chat(prompt, schema=SCHEMA, max_output_tokens=512, timeout=30.0)
    assert len(recorder.requests) == 2

    provider.chat(prompt, schema=SCHEMA, max_output_tokens=512, timeout=30.0)
    assert len(recorder.requests) == 3, "the second call paid the rejected round trip again"
    assert recorder.payloads[2]["response_format"] == {"type": llm.STRUCTURED_JSON_OBJECT}
    assert provider.structured_mode == llm.STRUCTURED_JSON_OBJECT


def test_the_fallback_states_the_schema_in_the_prompt_and_says_the_word_json(monkeypatch):
    """Two things at once, and the second is not decoration.

    ``json_object`` fixes only that the body *is* an object, so every constraint
    inside the schema has to be stated in words or it is not stated at all. And
    the endpoint refuses the request outright unless the prompt contains the
    word "json" — measured, ``HTTP 400 "Prompt must contain the word 'json' in
    some form to use 'response_format' of type 'json_object'."`` — so a
    fallback that stated the schema without that word would trade one 400 for
    another.
    """
    recorder = _rejects_then("This response_format type is unavailable now")
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    provider.chat(
        [llm.Message(role="user", content="hi")],
        schema=SCHEMA,
        max_output_tokens=512,
        timeout=30.0,
    )
    sent = " ".join(message["content"] for message in recorder.payloads[1]["messages"])
    assert "json" in sent.casefold(), "the request will be a 400 without the word"
    assert '"cited"' in sent, "the schema itself never reached the model"
    assert '"required"' in sent


def test_the_schema_statement_goes_ahead_of_the_variable_content(monkeypatch):
    """Prompt caching matches on a **prefix** and is priced ~50x cheaper, so the
    stable block belongs at the front.

    The instruction is inserted after the caller's leading system messages,
    keeping instructions contiguous ahead of whatever varies per call — a
    schema appended last would put a stable block behind a variable one and
    shorten every cacheable prefix to nothing.
    """
    recorder = _rejects_then("This response_format type is unavailable now")
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    provider.chat(
        [
            llm.Message(role="system", content="STABLE INSTRUCTIONS"),
            llm.Message(role="user", content="VARIABLE QUESTION"),
        ],
        schema=SCHEMA,
        max_output_tokens=512,
        timeout=30.0,
    )
    roles = [message["role"] for message in recorder.payloads[1]["messages"]]
    contents = [message["content"] for message in recorder.payloads[1]["messages"]]
    assert roles == ["system", "system", "user"]
    assert contents[0] == "STABLE INSTRUCTIONS"
    assert contents[2] == "VARIABLE QUESTION"


def test_the_schema_statement_is_byte_stable_across_equivalent_schemas(monkeypatch):
    """A prefix cache matches bytes. Two dicts meaning the same schema must
    render the same, or every call is a cache miss for a reason nobody can see."""
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_OBJECT)
    first = provider._outgoing(
        [llm.Message(role="user", content="q")], {"type": "object", "properties": {}}
    )
    second = provider._outgoing(
        [llm.Message(role="user", content="q")], {"properties": {}, "type": "object"}
    )
    assert first[0].content == second[0].content


def test_the_prompt_estimate_includes_the_schema_it_will_state(monkeypatch):
    """The estimate must bound what is actually sent.

    Under the fallback the schema costs prompt tokens, so a caller that fitted
    its context to an estimate excluding them would be refused by the very
    check it fitted against — or worse, sent and truncated.
    """
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_OBJECT)
    prompt = [llm.Message(role="user", content="hi")]
    bare = provider.estimate_prompt_tokens(prompt)
    with_schema = provider.estimate_prompt_tokens(prompt, schema=SCHEMA)
    assert with_schema > bare
    assert with_schema >= bare + len(json.dumps(SCHEMA, sort_keys=True, separators=(",", ":")))


def test_a_non_capability_400_is_not_negotiated(monkeypatch):
    """A 400 that says nothing about a field is a caller error, not a downgrade.

    Reading every 400 as "drop a feature" would let one malformed request
    permanently weaken a provider for the life of the process.
    """
    recorder = _rejects_then("your request had a malformed message array")
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    with pytest.raises(llm.ProviderUnavailable, match="400"):
        provider.chat(
            [llm.Message(role="user", content="hi")],
            schema=SCHEMA,
            max_output_tokens=512,
            timeout=30.0,
        )
    assert len(recorder.requests) == 1, "a caller error must not be re-sent"
    assert provider.structured_mode == llm.STRUCTURED_JSON_SCHEMA


@pytest.mark.parametrize(
    "status",
    [500, 502, 503, 429],
    ids=["server-error", "bad-gateway", "unavailable", "rate-limited"],
)
def test_only_a_400_is_read_as_a_capability_signal(wire, status: int):
    """A 5xx or a 429 is a bad minute, not a missing feature. Treating one as a
    capability signal would cripple a healthy provider permanently, and — since
    the belief is never re-raised — for the life of the process."""
    failure = urllib.error.HTTPError(
        url="http://x/v1/chat/completions", code=status, msg="nope", hdrs=None, fp=None
    )
    failure.read = lambda: b'{"error":{"message":"response_format is unavailable"}}'  # type: ignore[method-assign]
    wire(raises=failure)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    with pytest.raises(llm.ProviderUnavailable):
        provider.chat(
            [llm.Message(role="user", content="hi")],
            schema=SCHEMA,
            max_output_tokens=512,
            timeout=30.0,
        )
    assert provider.structured_mode == llm.STRUCTURED_JSON_SCHEMA


def test_the_negotiation_does_not_buy_a_second_full_timeout(monkeypatch):
    """One call is one ceiling — measured as **wall clock**, not as a smaller number.

    The whole call must still finish inside the deadline the caller's ``timeout``
    established, so what binds the re-send is *what is left of that deadline*,
    not "something under 30". The distinction is the entire content of the
    property: ``remaining = timeout - 0.001`` also produces a second timeout
    under 30 and is exactly the bug — a downgrade arriving 29 seconds in would
    then buy 29.999 more, doubling the wall clock the run's budget priced.

    So the refused attempt is made to burn real time, and the assertion is that
    the re-send's ceiling expires no later than the original deadline. That is
    unsatisfiable for any rule that does not read the clock.
    """
    burned = 0.25
    ceiling = 30.0
    recorder = _rejects_then(
        "This response_format type is unavailable now", first_attempt_seconds=burned
    )
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA).chat(
        [llm.Message(role="user", content="hi")],
        schema=SCHEMA,
        max_output_tokens=512,
        timeout=ceiling,
    )
    assert len(recorder.timeouts) == 2, "non-vacuity: there really was a re-send"
    assert recorder.timeouts[0] == ceiling, "the first attempt gets the ceiling exactly"
    # The whole call, worst case: the re-send's ceiling must expire no later
    # than the deadline the first attempt's ceiling established. `slack` covers
    # only the microseconds of payload arithmetic between `time.monotonic()` and
    # the socket — three orders of magnitude below what `burned` moves, so it
    # cannot hide the failure this is looking for.
    slack = 0.05
    deadline = recorder.started_at[0] + ceiling
    assert recorder.started_at[1] + recorder.timeouts[1] <= deadline + slack, (
        "the re-send may run past the one deadline the caller bought"
    )
    # The same property with no tolerance at all, since `burned` is known: the
    # re-send is charged what the first attempt really spent, which a fixed
    # shave off the ceiling cannot reproduce.
    assert recorder.timeouts[1] <= ceiling - burned


def test_a_negotiation_with_the_ceiling_already_spent_still_gets_a_positive_timeout(monkeypatch):
    """The floor exists so ``urlopen`` is never handed 0, and 0 is not benign.

    Measured against a live local server: ``timeout=0`` raises
    ``URLError(BlockingIOError(115, 'Operation now in progress'))`` in under
    10 ms — the socket goes non-blocking and the connect returns
    ``EINPROGRESS`` — while the same POST at ``0.001`` raises
    ``TimeoutError('timed out')``. It is ``timeout=None`` that blocks forever.
    So 0 does not hang; it lies: ``_post`` reads a non-``TimeoutError``
    ``URLError`` as :class:`ProviderUnavailable` "is not reachable", and a
    downgrade that ran out of clock would report a healthy server as down
    instead of naming the ceiling that bit.
    """
    recorder = _rejects_then(
        "This response_format type is unavailable now", first_attempt_seconds=0.2
    )
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA).chat(
        [llm.Message(role="user", content="hi")],
        schema=SCHEMA,
        max_output_tokens=512,
        timeout=0.05,
    )
    # Non-vacuity: the deadline really had passed before the re-send.
    assert recorder.started_at[1] - recorder.started_at[0] > 0.05
    assert recorder.timeouts[1] == llm._MIN_TIMEOUT > 0


# ── The reasoning knob ────────────────────────────────────────────────────────


def test_the_accepted_reasoning_levels_are_exactly_four():
    """The set nodum exposes, pinned so a widening is a deliberate edit."""
    assert llm.THINKING_LEVELS == ("none", "low", "medium", "high")
    assert llm.DEFAULT_THINKING in llm.THINKING_LEVELS


@pytest.mark.parametrize("level", ["none", "low", "medium", "high"])
def test_every_accepted_level_resolves(monkeypatch, level: str):
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash")
    monkeypatch.setenv(llm.ENV_THINKING, level)
    provider = llm.get_provider()
    assert provider is not None
    assert provider.thinking == level


@pytest.mark.parametrize(
    "level",
    ["max", "xhigh", "minimal", "off", "true", "HIGHER", "1", ""],
    ids=["max", "xhigh", "minimal", "off", "true", "higher", "one", "empty-after-strip"],
)
def test_a_level_outside_the_accepted_set_is_no_provider_with_a_reason(monkeypatch, level: str):
    """Refused, never passed through — and this is where the module parts
    company with ``nodum.agent``'s "an unparseable value falls back" rule.

    That rule is right for a *number*: the fallback is a smaller ceiling and the
    worst case is less work. A reasoning level is a **name**, and a name this
    endpoint does not know is not a slower call. The live API validates the enum
    strictly (``unknown variant`` on a bogus value), so a value nodum passed
    through would either 400 the request or — for a level the API knows and
    nodum does not want — quietly run under a setting nobody chose while the
    report named the level that was asked for.

    The empty case is the exception and is *not* a refusal: an unset variable
    means "use the default", which is the same thing every other setting here
    means by absence.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash")
    monkeypatch.setenv(llm.ENV_THINKING, level)
    if not level.strip():
        assert llm.get_provider() is not None, "an unset knob is the default, not a refusal"
        return
    assert llm.get_provider() is None
    reason = llm.unavailable_reason() or ""
    assert llm.ENV_THINKING in reason
    for accepted in llm.THINKING_LEVELS:
        assert accepted in reason, "the refusal must name the set the caller may choose from"


def test_the_level_is_always_sent_when_the_endpoint_takes_one(wire):
    """Leaving ``reasoning_effort`` off is not neutral: unset measured 1 492
    reasoning tokens on a fixture where ``none`` measured 0. So the field is
    stated rather than omitted."""
    recorder = wire()
    _provider(thinking="high", graded_thinking=True).chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=512, timeout=5.0
    )
    assert recorder.payload["reasoning_effort"] == "high"


def test_a_per_call_level_overrides_the_configured_one(wire):
    recorder = wire()
    _provider(thinking="high", graded_thinking=True).chat(
        [llm.Message(role="user", content="hi")],
        max_output_tokens=512,
        timeout=5.0,
        thinking=llm.THINKING_NONE,
    )
    assert recorder.payload["reasoning_effort"] == "none"


def test_an_endpoint_that_takes_no_graded_level_still_gets_none(wire):
    """``none`` is the one level every endpoint measured accepts.

    ollama answers 400 to ``low``/``medium``/``high`` — including on
    ``qwen3:8b``, which really does think — and 200 to ``none``. Withholding
    ``none`` along with the rest would give up the one setting that fixes the
    documented ``qwen3:8b`` failure, where a ``<think>`` block eats the whole
    output ceiling and the body comes back empty.
    """
    recorder = wire()
    _provider(thinking=llm.THINKING_NONE, graded_thinking=False).chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=512, timeout=5.0
    )
    assert recorder.payload["reasoning_effort"] == "none"


def test_a_graded_level_is_withheld_from_an_endpoint_that_refuses_them(wire):
    """The regression this exists to stop: a graded level sent unconditionally
    is HTTP 400 on **every** ollama call, which is the whole local half."""
    recorder = wire()
    provider = _provider(thinking="high", graded_thinking=False)
    provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=512, timeout=5.0)
    assert "reasoning_effort" not in recorder.payload
    assert provider.thinking == "high", "the configured level is still what was asked for"
    assert provider.thinking_applied is False, "and the run must be able to see it did nothing"


@pytest.mark.parametrize(
    "detail",
    [
        '"llama3.2:1b" does not support thinking',
        'think value "low" is not supported for this model',
        "reasoning_effort: unknown variant",
    ],
    ids=["ollama-no-thinking-model", "ollama-thinking-model", "unknown-variant"],
)
def test_a_provider_that_refuses_a_graded_level_is_negotiated_down(monkeypatch, detail: str):
    """Three different sentences from two servers, all meaning one thing.

    The first two are measured verbatim from ollama — one server, two messages,
    depending on whether the model has thinking at all — which is why the match
    is a list rather than a string.
    """
    recorder = _rejects_then(detail)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(thinking="high", graded_thinking=True)
    provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=512, timeout=30.0)
    assert len(recorder.requests) == 2
    assert recorder.payloads[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in recorder.payloads[1]
    assert provider.thinking_applied is False


def test_a_bad_level_passed_per_call_is_a_value_error(wire):
    wire()
    with pytest.raises(ValueError, match="thinking must be one of"):
        _provider().chat(
            [llm.Message(role="user", content="hi")],
            max_output_tokens=512,
            timeout=5.0,
            thinking="max",
        )


# ── Out of the box ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_a_deepseek_model_name_alone_configures_the_whole_endpoint(monkeypatch, model: str):
    """ "Nothing to configure but the key", stated as a test.

    The four things that would otherwise have to be set by hand are all
    measured facts about the endpoint rather than preferences — and the one an
    operator is most likely to get wrong (the window) is the one that silently
    re-opens the truncation hole this module is mostly about.

    Both ids are exercised because both are what ``GET
    https://api.deepseek.com/models`` really lists, and the match is on that
    list rather than on a name shape.
    """
    monkeypatch.setenv(llm.ENV_MODEL, model)
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-test")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.provider_id == llm.DEEPSEEK_BASE_URL
    assert provider.context_tokens == 1_000_000
    assert provider.structured_mode == llm.STRUCTURED_JSON_OBJECT, (
        "a known-json_object endpoint must not pay a rejected round trip to learn it"
    )
    assert provider.thinking_applied is True
    assert llm.unavailable_reason() is None


def test_an_explicit_setting_always_beats_the_profile(monkeypatch):
    """A profile supplies defaults and decides nothing an operator has decided.

    Spelled at the profile's *own* host, so the profile really is in force and
    the assertion is about an override rather than about an absence.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash")
    monkeypatch.setenv(llm.ENV_BASE_URL, "https://api.deepseek.com/v1")
    monkeypatch.setenv(llm.ENV_CONTEXT_TOKENS, "8192")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.provider_id == llm.DEEPSEEK_BASE_URL
    assert provider.context_tokens == 8192
    # Non-vacuity: the profile matched, so 8192 displaced a real 1 000 000.
    matched = llm.profile_for(model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1")
    assert matched is not None
    assert matched.context_tokens == 1_000_000


# ── A model name may not move a call to another company's server ──────────────
#
# `deepseek-` is a *prefix an ollama library model shares with a hosted API*,
# which is the whole hazard: matching on it took a local install's prompts to
# `https://api.deepseek.com/v1`.


#: ollama library model ids that a `deepseek-` prefix match captured. Every one
#: of these is pulled and served locally, with no key and no vendor account.
LOCALLY_SERVED_DEEPSEEK_NAMES = (
    "deepseek-r1:8b",
    "deepseek-r1",
    "deepseek-coder:6.7b",
    "deepseek-coder-v2",
    "deepseek-llm",
    "deepseek-v2",
    "deepseek-v3",
    "deepseek-v3.1",
)


@pytest.mark.parametrize("model", LOCALLY_SERVED_DEEPSEEK_NAMES)
def test_a_locally_served_model_name_never_selects_a_remote_endpoint(monkeypatch, model: str):
    """The blocker: "run this locally" must not mean "POST it to a vendor".

    Every name here is an **ollama library model**. Under the old prefix match,
    ``NODUM_LLM_MODEL=deepseek-r1:8b`` with nothing else set resolved to
    ``https://api.deepseek.com/v1`` — so an install whose only statement about
    where to run was "the default endpoint" sent private graph text off the
    machine whenever ``NODUM_LLM_API_KEY`` happened to be set (the README tells
    operators to set one), and 401'd against a host nobody had configured when
    it was not. The same configuration on the previous release ran against
    local ollama.

    A guessed *hosted* model id is not a symmetric mistake to a guessed local
    one: the local guess costs a 404 the operator reads, and this one costs
    egress they never see.
    """
    monkeypatch.setenv(llm.ENV_MODEL, model)
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-test")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.provider_id == llm.DEFAULT_BASE_URL
    assert provider.context_tokens == llm.DEFAULT_CONTEXT_TOKENS
    assert llm.profile_for(model=model, base_url=None) is None


def test_an_explicit_local_endpoint_keeps_its_own_window(monkeypatch):
    """A profile may not impose a window on a server the operator named.

    The URL half of this already worked — an explicit :data:`ENV_BASE_URL` wins
    — and that made the hole harder to see, not smaller: ``context_tokens``
    still came from the profile, so a **1 000 000-token** belief was carried
    against a server serving 4 096. The refusal is computed against that belief,
    so nothing would have been refused, and the silent truncation this module is
    mostly about was reopened by the very table written to close it.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash")
    monkeypatch.setenv(llm.ENV_BASE_URL, llm.DEFAULT_BASE_URL)
    provider = llm.get_provider()
    assert provider is not None
    assert provider.provider_id == llm.DEFAULT_BASE_URL
    assert provider.context_tokens == llm.DEFAULT_CONTEXT_TOKENS
    # And the rest of the profile goes with it: this is ollama, which answers
    # 400 to every graded level.
    assert provider.thinking_applied is False


@pytest.mark.parametrize(
    "base_url",
    [
        "https://deepseek-gw.lan/v1",
        "http://deepseek.internal:8080/v1",
        "https://api.deepseek.com.evil.example/v1",
    ],
    ids=["prefixed-proxy", "internal-host", "suffixed-lookalike"],
)
def test_a_host_that_merely_contains_the_vendor_name_is_not_the_vendor(monkeypatch, base_url: str):
    """Host matching is a parsed hostname, never a substring of a URL.

    ``key in host`` handed the whole hosted profile — its window, its structured
    mode, its graded thinking — to any endpoint whose URL happened to contain
    the string, including a lookalike domain somebody else owns.
    """
    assert llm.profile_for(model="some-model", base_url=base_url) is None
    monkeypatch.setenv(llm.ENV_MODEL, "some-model")
    monkeypatch.setenv(llm.ENV_BASE_URL, base_url)
    provider = llm.get_provider()
    assert provider is not None
    assert provider.context_tokens == llm.DEFAULT_CONTEXT_TOKENS


def test_a_base_url_that_does_not_parse_is_a_stranger_and_not_a_traceback(monkeypatch):
    """Reading a hostname is new work at resolution time, and it may not raise.

    The substring match it replaced could not fail on a malformed URL;
    ``urlsplit`` can — measured, ``ValueError("Invalid IPv6 URL")`` on
    ``http://[bad/v1``. So :func:`profile_for` answers "not a profiled endpoint"
    rather than raising, and resolution goes on to have an opinion about the URL
    in words rather than in a traceback out of ``get_provider``.

    **What that opinion is changed**, and this test carries the new one: it used
    to build a provider that failed against the endpoint the operator typed, and
    the failure was a bare ``ValueError`` that no handler in this package
    catches — see
    :func:`test_an_unpostable_base_url_is_an_llm_error_and_never_a_value_error`.
    Unusable *configuration* is now no provider with a reason, at resolution
    time, exactly as an unparseable window already was.
    """
    assert llm.profile_for(model="deepseek-v4-flash", base_url="http://[bad/v1") is None
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash")
    monkeypatch.setenv(llm.ENV_BASE_URL, "http://[bad/v1")
    assert llm.get_provider() is None
    reason = llm.unavailable_reason()
    assert reason is not None
    assert llm.ENV_BASE_URL in reason
    assert "Invalid IPv6 URL" in reason


@pytest.mark.parametrize(
    ("base_url", "detail"),
    [
        ("api.deepseek.com/v1", "unknown url type"),
        ("http://[bad/v1", "Invalid IPv6 URL"),
    ],
    ids=["no-scheme", "bad-ipv6"],
)
def test_a_base_url_urllib_cannot_post_to_is_no_provider_with_a_reason(
    monkeypatch, base_url: str, detail: str
):
    """Unusable configuration is an absence with a sentence, never a failed call.

    Both spellings are driven rather than imagined:
    ``NODUM_LLM_BASE_URL=api.deepseek.com/v1`` and
    ``NODUM_LLM_BASE_URL=http://[bad/v1`` each raised out of
    ``urllib.request.Request`` — which is *configuration* failing, not a
    network. ``nodum llm status`` says "nothing here is an error", so this
    belongs beside an unparseable ``NODUM_LLM_CONTEXT_TOKENS`` and an
    unrecognised ``NODUM_LLM_THINKING``: no provider, a reason naming the
    variable and the fix, and exit 0.

    **The scheme is not guessed back in**, which is the decision this pins. A
    repair would choose ``http`` or ``https`` for the operator, and that choice
    decides whether ``NODUM_LLM_API_KEY`` crosses the network in clear text.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash")
    monkeypatch.setenv(llm.ENV_BASE_URL, base_url)
    assert llm.get_provider() is None
    reason = llm.unavailable_reason()
    assert reason is not None
    assert llm.ENV_BASE_URL in reason, "the reason must name the variable a human can act on"
    assert detail in reason, "the reason must carry what urllib actually said"
    assert llm.DEFAULT_BASE_URL in reason, "and an example of a spelling that works"


def test_a_scheme_less_base_url_is_still_read_as_a_host_for_the_profile_match(monkeypatch):
    """The two halves are deliberately not the same rule, so both are stated.

    :func:`_hostname` is forgiving on purpose — it reads a host out of a
    spelling nothing can POST to, so the refusal above can be about *that
    endpoint* rather than about a string, and so
    :func:`test_the_profiled_host_is_matched_however_the_url_is_spelled` keeps
    matching every spelling of the one URL it exists to know. Refusing the same
    spelling at resolution is a separate question with a separate answer.
    """
    assert llm.profile_for(model="anything-at-all", base_url="api.deepseek.com/v1") is not None
    assert llm.base_url_problem("api.deepseek.com/v1") is not None


def test_the_profiled_host_is_matched_however_the_url_is_spelled(monkeypatch):
    """The other side of the same rule: the endpoint really is the endpoint.

    A hostname comparison must not be so strict that it misses the port-free,
    scheme-free and path-varying spellings of the one URL it exists to know.
    """
    for spelling in (
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com",
        "https://API.DeepSeek.com/beta",
        "https://api.deepseek.com:443/v1",
        "api.deepseek.com/v1",
    ):
        assert llm.profile_for(model="anything-at-all", base_url=spelling) is not None, spelling


def test_the_local_default_still_resolves_to_ollama_and_withholds_graded_levels(monkeypatch):
    """The half that already worked must not regress.

    ``llama3.2:1b`` with nothing else set is 5b-i's verified configuration. A
    graded ``reasoning_effort`` sent there is HTTP 400 on every call, so the
    default endpoint starts out disbelieving them rather than paying a 400 to
    find out.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "llama3.2:1b")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.provider_id == llm.DEFAULT_BASE_URL
    assert provider.context_tokens == llm.DEFAULT_CONTEXT_TOKENS
    assert provider.structured_mode == llm.STRUCTURED_JSON_SCHEMA
    assert provider.thinking_applied is False


def test_an_unknown_remote_provider_starts_optimistic(monkeypatch):
    """A profile is an optimisation, never a gate: an endpoint nobody has
    profiled gets the strongest form and negotiates down on the first 400."""
    monkeypatch.setenv(llm.ENV_MODEL, "some-new-model")
    monkeypatch.setenv(llm.ENV_BASE_URL, "https://api.example.com/v1")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.structured_mode == llm.STRUCTURED_JSON_SCHEMA
    assert provider.thinking_applied is True


def test_the_thinking_ladder_is_walked_one_rung_at_a_time(monkeypatch):
    """``graded`` → ``off-only`` → ``absent``, and never two rungs at once.

    An endpoint that refuses ``low`` may still take ``none`` — ollama does,
    measured, and there ``none`` is the documented cure for ``qwen3:8b``
    answering with an empty body. So the evidence "a graded level was refused"
    may not be read as "the field is not understood": collapsing the ladder
    would give up a working setting on the strength of a different refusal.
    """
    seen: list[Any] = []

    class _AlwaysRefusesThinking:
        def __init__(self) -> None:
            self.requests: list[urllib.request.Request] = []

        def __call__(self, request, timeout=None):
            payload = json.loads(request.data)
            self.requests.append(request)
            seen.append(payload.get("reasoning_effort", "<absent>"))
            if "reasoning_effort" in payload:
                failure = urllib.error.HTTPError(
                    url="http://x", code=400, msg="Bad", hdrs=None, fp=None
                )
                failure.read = lambda: b'{"error":{"message":"does not support thinking"}}'
                raise failure
            return _FakeResponse(json.dumps(_answer()).encode())

    recorder = _AlwaysRefusesThinking()
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(thinking="high", graded_thinking=True)
    prompt = [llm.Message(role="user", content="hi")]
    provider.chat(prompt, max_output_tokens=512, timeout=30.0)
    # `high` is refused, so the graded level is withheld and this call — which
    # asked for `high` — sends nothing.
    assert seen == ["high", "<absent>"]
    assert provider.thinking_applied is False

    # **The rung that a collapsed ladder loses.** This is the ollama shape
    # exactly: a global `high` that never lands, and a call site that pins
    # `none`. `none` is still believed in, so it is still sent — and it is the
    # one setting that fixes `qwen3:8b` returning an empty body. A downgrade
    # straight to "the field is not understood" would have thrown it away on the
    # evidence of a refusal that was only ever about a *graded* level.
    seen.clear()
    provider.chat(prompt, max_output_tokens=512, timeout=30.0, thinking=llm.THINKING_NONE)
    assert seen[0] == "none", "the ladder dropped two rungs and lost a working setting"


def test_an_endpoint_that_refuses_none_itself_stops_sending_the_field(monkeypatch):
    """The rung a two-state boolean could not express.

    A server that rejects unknown fields refuses ``reasoning_effort: "none"``
    along with everything else. With only "graded or not", ``none`` was sent
    unconditionally and such a server was permanently unusable — every call a
    400, with the downgrade already spent. Found by driving the status probe,
    which is the one call site that pins ``none``.
    """
    seen: list[Any] = []

    class _RefusesTheFieldEntirely:
        def __call__(self, request, timeout=None):
            payload = json.loads(request.data)
            seen.append(payload.get("reasoning_effort", "<absent>"))
            if "reasoning_effort" in payload:
                failure = urllib.error.HTTPError(
                    url="http://x", code=400, msg="Bad", hdrs=None, fp=None
                )
                failure.read = lambda: b'{"error":{"message":"unknown field reasoning_effort"}}'
                raise failure
            return _FakeResponse(json.dumps(_answer()).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _RefusesTheFieldEntirely())
    provider = _provider(thinking=llm.THINKING_NONE, graded_thinking=False)
    completion = provider.chat(
        [llm.Message(role="user", content="hi")], max_output_tokens=512, timeout=30.0
    )
    assert seen == ["none", "<absent>"], "the field was never withdrawn"
    assert completion.text == "ok"
    assert provider.thinking_applied is False


def test_a_short_prompt_is_not_reported_truncated_by_its_own_overheads():
    """A live false positive, found by driving ``nodum llm status``.

    :func:`estimate_prompt_tokens` adds :data:`TEMPLATE_OVERHEAD_TOKENS` (32)
    plus :data:`MESSAGE_OVERHEAD_TOKENS` (16) plus the role — about 52 tokens
    that are *nodum's guess at a chat template*, not bytes anybody sent. On a
    long prompt they vanish into the content. On a 33-byte prompt they are 60 %
    of the estimate, so ``prompt_tokens * 6 < estimate`` fires on a completion
    the server read in full: measured, the reachability probe came back with
    ``prompt_tokens: 12`` against an 85-token estimate, was raised as
    ``ContextOverflow``, and ``llm status`` reported ``failed_calls: 1`` on a
    perfectly healthy install — the one command whose job is to say whether the
    install is well.

    The truncation check must therefore weigh only what was really sent. That
    keeps the defence exactly as strong where it matters, because a truncation
    worth catching is one that lost a *quarter of a long prompt*, and the
    overheads are noise at that size.
    """
    prompt = [llm.Message(role="user", content="Reply with exactly one word: pong")]
    estimate = llm.estimate_prompt_tokens(prompt)
    content_only = llm.estimate_content_tokens(prompt)
    # Non-vacuity: the overheads really do dominate at this size, so the row is
    # about them and not about two numbers that happen to agree.
    assert estimate > 2 * content_only, "fixture is not in the regime this test is about"

    completion = llm.Completion(
        text="pong",
        prompt_tokens=12,
        output_tokens=2,
        finish_reason="stop",
        model_id="m",
        provider_id="p",
        context_tokens=1_000_000,
        latency_ms=1,
        prompt_estimate=estimate,
        prompt_content_estimate=content_only,
    )
    assert completion.prompt_truncated is False, (
        "a short prompt was called truncated because of overheads nobody sent"
    )


def test_a_real_truncation_is_still_caught_after_the_overheads_are_excluded():
    """The other side of the same change: the defence must not have been widened
    into uselessness.

    A 70 000-byte prompt reported back as 4 096 tokens is the measured silent
    truncation this check exists for, and excluding ~52 tokens of overhead from a
    70 000-token estimate cannot rescue it.
    """
    prompt = [llm.Message(role="user", content="x" * 70_000)]
    completion = llm.Completion(
        text="an answer from a prefix",
        prompt_tokens=4096,
        output_tokens=9,
        finish_reason="stop",
        model_id="m",
        provider_id="p",
        context_tokens=32768,
        latency_ms=1,
        prompt_estimate=llm.estimate_prompt_tokens(prompt),
        prompt_content_estimate=llm.estimate_content_tokens(prompt),
    )
    assert completion.context_filled is False, "the configured-window check is blind here"
    assert completion.prompt_truncated is True


# ── A model name may not move a *credential* either ───────────────────────────
#
# The sibling of the section above, and the half it missed. That one stopped a
# model name moving the *prompt* to another company's server. This one is the
# mirror: the prompt correctly stayed local while the vendor's bearer token went
# with it, because `_post` attached `NODUM_LLM_API_KEY` to whatever host
# resolution happened to produce.


def test_a_key_is_withheld_when_nothing_named_the_endpoint_it_would_go_to(monkeypatch):
    """The leak, driven end to end before it was closed.

    ``NODUM_LLM_MODEL`` naming a hosted model this ships no profile for — a
    typo, a newer id — plus ``NODUM_LLM_API_KEY`` resolved to the **local
    default**, and the key went there: captured on a throwaway listener as
    ``Authorization: Bearer …``, ready to be read out of any local process's
    logs. The prompt stayed on the machine; the credential did not stay with the
    vendor.

    The rule is that a key travels only to an endpoint *somebody named* — the
    operator through ``NODUM_LLM_BASE_URL``, or the model id through a shipped
    profile. ``http://localhost:11434/v1`` here was named by neither: this
    module chose it.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash-0711")
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-vendor-secret")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.provider_id == llm.DEFAULT_BASE_URL, "fixture is not the fallback case"

    reason = llm.key_withheld_reason()
    assert reason is not None, "a key that is not being sent must be said out loud"
    assert llm.ENV_API_KEY in reason and llm.ENV_BASE_URL in reason
    assert "sk-vendor-secret" not in reason, "the sentence about a secret may not carry it"


def test_the_withheld_key_never_reaches_the_wire(wire, monkeypatch):
    """Withheld at resolution, so there is no key to leak later.

    Asserted on the transport rather than on the resolution, because that is
    where the capture happened: the header is what a listener on the resolved
    host reads.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash-0711")
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-vendor-secret")
    recorder = wire()
    provider = llm.get_provider()
    assert provider is not None
    provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert recorder.requests[-1].get_header("Authorization") is None


@pytest.mark.parametrize(
    ("model", "base_url", "expected_host"),
    [
        ("deepseek-v4-flash", None, "https://api.deepseek.com/v1"),
        ("any-model-at-all", "https://api.example.com/v1", "https://api.example.com/v1"),
        ("qwen3:8b", "http://localhost:11434/v1", "http://localhost:11434/v1"),
        ("gateway-model", "http://gw.lan:8080/v1", "http://gw.lan:8080/v1"),
    ],
    ids=["profiled-model-id", "remote-gateway", "named-local-default", "self-hosted-gateway"],
)
def test_a_named_endpoint_keeps_its_key(
    wire, monkeypatch, model: str, base_url: str | None, expected_host: str
):
    """Every legitimate way to hold a key, and it must survive all of them.

    The last two rows are the case the fix may not break, and they are the same
    case: **many local gateways require a key.** One of them is on
    ``localhost:11434`` itself, which is why the rule cannot be "no key goes to
    localhost" — it is "no key goes to a host nodum picked". Naming that very
    host in ``NODUM_LLM_BASE_URL`` is one variable, and it is the operator
    saying the key belongs there.
    """
    monkeypatch.setenv(llm.ENV_MODEL, model)
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-configured")
    if base_url is not None:
        monkeypatch.setenv(llm.ENV_BASE_URL, base_url)
    recorder = wire()
    provider = llm.get_provider()
    assert provider is not None
    assert provider.provider_id == expected_host
    assert llm.key_withheld_reason() is None

    provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert recorder.requests[-1].get_header("Authorization") == "Bearer sk-configured"


def test_a_401_says_the_key_was_withheld_rather_than_leaving_it_to_llm_status(wire, monkeypatch):
    """The configuration this fix can break, made legible where it breaks.

    A local gateway that legitimately requires a key, reached on the default
    base URL: withholding is still right — nothing named an endpoint for that
    key — but the call now 401s, and the sentence explaining why lived only in
    ``nodum llm status``. An operator meeting a bare 401 reads it as a wrong
    key and goes looking in the wrong place.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash-0711")
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-vendor-secret")
    failure = urllib.error.HTTPError(
        url="http://x/v1/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=None,
    )
    failure.read = lambda: b'{"error":{"message":"authorization required"}}'  # type: ignore[method-assign]
    wire(raises=failure)
    provider = llm.get_provider()
    assert provider is not None
    assert llm.key_withheld_reason() is not None, "fixture is not the withheld case"

    with pytest.raises(llm.ProviderUnavailable) as raised:
        provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)

    assert llm.ENV_BASE_URL in str(raised.value), "the failure must name the way out"
    assert "sk-vendor-secret" not in str(raised.value), "still no secret in a message"


@pytest.mark.parametrize(
    ("code", "carries_reason"),
    [(401, True), (403, True), (400, False), (404, False), (429, False), (500, False)],
    ids=["401", "403", "400", "404", "429", "500"],
)
def test_only_an_auth_status_carries_the_withheld_key_sentence(
    wire, monkeypatch, code: int, carries_reason: bool
):
    """The status gate is the part of that fix that keeps it honest.

    Withholding the key explains a 401 and a 403 and nothing else. A 404 is the
    measured commonest misconfiguration — an unknown model id — and hanging a
    447-character paragraph about credentials off it sends the operator to the
    wrong variable. Parametrised in both directions on purpose: widening the
    gate to every status fails the four negatives, narrowing it to ``{401}``
    fails the 403, and neither was caught by anything before this.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "deepseek-v4-flash-0711")
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-vendor-secret")
    failure = urllib.error.HTTPError(
        url="http://x/v1/chat/completions", code=code, msg="nope", hdrs=None, fp=None
    )
    failure.read = lambda: b'{"error":{"message":"nope"}}'  # type: ignore[method-assign]
    wire(raises=failure)
    provider = llm.get_provider()
    assert provider is not None
    reason = llm.key_withheld_reason()
    assert reason is not None, "fixture is not the withheld case"

    with pytest.raises(llm.ProviderUnavailable) as raised:
        provider.chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)

    assert (reason in str(raised.value)) is carries_reason


def test_a_bad_default_endpoint_is_not_blamed_on_a_variable_nobody_set(monkeypatch):
    """The refusal names whatever produced the URL, not always the env var.

    Only an operator-set ``NODUM_LLM_BASE_URL`` is the operator's to fix;
    blaming it for a profile's URL or the shipped default sends them to edit
    something they never set. Reached by breaking the default on purpose, since
    both shipped constants parse — which is how long a wrong sentence here
    would otherwise go unnoticed.
    """
    monkeypatch.setattr(llm, "DEFAULT_BASE_URL", "localhost/v1")
    monkeypatch.setenv(llm.ENV_MODEL, "llama3.2:1b")
    monkeypatch.delenv(llm.ENV_BASE_URL, raising=False)

    assert llm.get_provider() is None
    reason = llm.unavailable_reason()
    assert reason is not None
    # Read the whole sentence, not just the label: the first cut of this branch
    # passed a substring check while rendering "Give it a full …" with nothing
    # for "it" to refer to, and dropping the one variable the operator can set.
    assert reason.startswith("the endpoint this resolved to, 'localhost/v1',")
    assert f"{llm.ENV_BASE_URL}='localhost" not in reason, "not the operator's variable"
    assert f"Override it with {llm.ENV_BASE_URL}" in reason, "must still name the way out"


def test_no_key_configured_is_not_a_withheld_key(monkeypatch):
    """``key_withheld_reason`` is about a key that exists and is being left
    behind. An install with no key at all has nothing to report, and a sentence
    there would send a human looking for a variable they never set."""
    monkeypatch.setenv(llm.ENV_MODEL, "llama3.2:1b")
    assert llm.get_provider() is not None
    assert llm.key_withheld_reason() is None


def test_a_forced_provider_reports_no_withheld_key():
    """The test seam builds a provider from a base URL and a key together, so
    the caller has named both and this module withheld nothing."""
    llm.set_provider(_provider(api_key="sk-injected"))
    assert llm.key_withheld_reason() is None
    llm.set_provider(None, reason="off")
    assert llm.key_withheld_reason() is None


# ── The model name is trimmed once, where it is read ──────────────────────────


def test_a_model_name_with_stray_whitespace_still_selects_its_profile(monkeypatch):
    """``profile_for`` no longer strips, so the one strip left must be enough.

    ``_resolve_default`` trims ``NODUM_LLM_MODEL`` before it does anything with
    it, and a second trim inside ``profile_for`` was code no test could tell
    from its absence. Removing it is only safe while *this* stays true — an
    operator's ``export NODUM_LLM_MODEL="deepseek-v4-flash "`` still lands on the
    profiled endpoint, and the provider is given the trimmed name.
    """
    monkeypatch.setenv(llm.ENV_MODEL, "  deepseek-v4-flash \n")
    monkeypatch.setenv(llm.ENV_API_KEY, "sk-configured")
    provider = llm.get_provider()
    assert provider is not None
    assert provider.model_id == "deepseek-v4-flash"
    assert provider.provider_id == llm.DEEPSEEK_BASE_URL
    assert provider.context_tokens == 1_000_000


# ── A `usage` block that contradicts itself ───────────────────────────────────


def test_reasoning_above_the_completion_is_clamped_and_said_out_loud(wire, caplog):
    """``reasoning <= completion`` was claimed as an invariant and never checked.

    :attr:`Completion.reasoning_tokens` documents itself as a **share** of
    :attr:`Completion.output_tokens` — measured true on every
    ``deepseek-v4-flash`` call — and :attr:`Completion.content_tokens` is the one
    minus the other. A wire reporting ``completion_tokens: 50`` with
    ``reasoning_tokens: 5000`` makes that difference ``0`` and prints a report in
    which the thinking is larger than the output it is part of.

    No budget moves either way: reasoning is correctly never summed into the
    spend, and ``total_tokens`` here is still ``prompt + completion``. It is the
    *report* that would be nonsense, which is why this clamps rather than
    refuses — and logs, because a provider that has started saying this is worth
    somebody knowing.
    """
    wire(_answer(prompt_tokens=10, completion_tokens=50, reasoning_tokens=5000))
    with caplog.at_level("WARNING", logger="nodum.llm"):
        completion = _provider().chat(
            [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
        )
    assert completion.reasoning_tokens == 50, "reasoning may not exceed the output it is inside"
    assert completion.content_tokens == 0
    assert completion.total_tokens == 60, "the bill is unchanged — this was never a budget hole"
    assert any("reasoning" in record.message for record in caplog.records), (
        "a provider contradicting itself must be noticed, not smoothed"
    )


def test_reasoning_inside_the_completion_is_left_exactly_alone(wire, caplog):
    """The control: the ordinary case must not be clamped, rounded or logged."""
    wire(_answer(prompt_tokens=10, completion_tokens=1520, reasoning_tokens=1420))
    with caplog.at_level("WARNING", logger="nodum.llm"):
        completion = _provider().chat(
            [llm.Message(role="user", content="hi")], max_output_tokens=2048, timeout=5.0
        )
    assert completion.reasoning_tokens == 1420
    assert completion.content_tokens == 100
    assert caplog.records == []


def test_a_provider_that_disagrees_about_the_bill_is_noticed(wire, caplog):
    """``usage.total_tokens`` was never read at all, which is two decisions in one.

    Overriding it is right — :attr:`Completion.total_tokens` is what budgets are
    denominated in and there may be one rule for it everywhere. *Not reading it*
    is different: a provider that bills differently from the way this meters
    could never be noticed, on any call, ever.
    """
    answer = _answer(prompt_tokens=100, completion_tokens=20)
    answer["usage"]["total_tokens"] = 999
    wire(answer)
    with caplog.at_level("WARNING", logger="nodum.llm"):
        completion = _provider().chat(
            [llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0
        )
    assert completion.total_tokens == 120, "nodum's own arithmetic still decides the bill"
    assert any("total_tokens" in record.message for record in caplog.records)


def test_a_wire_that_carries_no_total_is_not_a_disagreement(wire, caplog):
    """``0`` from :func:`_optional_count` means "the wire did not say" — the
    ordinary ollama shape — and never "this provider claims the call was free"."""
    answer = _answer(prompt_tokens=100, completion_tokens=20)
    del answer["usage"]["total_tokens"]
    wire(answer)
    with caplog.at_level("WARNING", logger="nodum.llm"):
        _provider().chat([llm.Message(role="user", content="hi")], max_output_tokens=8, timeout=5.0)
    assert caplog.records == []


# ── The 400 markers, from the side that must *not* match ──────────────────────


@pytest.mark.parametrize(
    "detail",
    [
        "Invalid schema for response_format.schema.properties[0]: expected an object",
        "response_format.json_schema.name is required",
        "your request had a malformed message array",
    ],
    ids=["schema-path", "field-path", "no-marker-at-all"],
)
def test_a_400_that_is_not_a_capability_statement_never_downgrades(monkeypatch, detail: str):
    """The negative half of a matcher this module keeps deliberately blunt.

    :data:`_STRUCTURED_REJECTIONS` is a substring list, so the boundary has to be
    pinned from the outside or a later broadening swallows unrelated errors in
    silence. The first two rows are the ones a plain ``"response_format" in
    detail`` gets wrong: a server naming ``response_format.schema.properties``
    has **parsed** the field and is validating what is inside it, which is proof
    that it *serves* it and that the fault is nodum's own schema. Downgrading
    there trades a loud, fixable "your schema is wrong" for an envelope quietly
    weakened for the life of the process — the exact harm
    :func:`test_a_non_capability_400_is_not_negotiated` exists to prevent,
    reached through a message that happens to contain the marker.

    All three must behave identically: one request, no re-send, the failure
    reaching the caller, and the provider still believing in ``json_schema``.
    """
    recorder = _rejects_then(detail)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    with pytest.raises(llm.ProviderUnavailable, match="400"):
        provider.chat(
            [llm.Message(role="user", content="hi")],
            schema=SCHEMA,
            max_output_tokens=512,
            timeout=30.0,
        )
    assert len(recorder.requests) == 1, "this 400 was read as a capability signal and re-sent"
    assert provider.structured_mode == llm.STRUCTURED_JSON_SCHEMA


def test_the_measured_capability_400_still_downgrades(monkeypatch):
    """Non-vacuity for the row above: the sharpening may not have turned the
    negotiation off. ``deepseek-v4-flash``'s own sentence still has to work, or
    every structured call on that provider is a hard failure out of the box."""
    recorder = _rejects_then("This response_format type is unavailable now")
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    provider = _provider(structured_mode=llm.STRUCTURED_JSON_SCHEMA)
    provider.chat(
        [llm.Message(role="user", content="hi")],
        schema=SCHEMA,
        max_output_tokens=512,
        timeout=30.0,
    )
    assert len(recorder.requests) == 2
    assert provider.structured_mode == llm.STRUCTURED_JSON_OBJECT
