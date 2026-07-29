"""The read-only smart surface: ask, summarize, and a natural-language rewrite.

Phase 5b-i's three endpoints (design E1–E3) and the provider status behind
them. **Nothing here writes.** Every function reads the graph through the
ordinary public :mod:`nodum.service` and :mod:`nodum.search` calls, reaches the
model only through :class:`nodum.agent.AgentRun`, and hands back a result plus
the accounting the run produced.

## Why this is a module and not three route handlers

``http_api`` route handlers are thin delegates — one domain call each, no
behaviour the domain lacks — and the behaviour here is not thin. Deciding
whether a question was answered is a rule about the graph, not about HTTP, and
the CLI asks the identical question through the identical code. So the rule
lives here and both surfaces delegate to it, which is the same shape
``nodum.ingest`` has for the two upload paths.

The result models live here too, beside the code that produces them, following
:mod:`nodum.agent`'s own precedent in this phase (``LLMReport``, ``Generation``
and ``GeneratedBy`` are pydantic models in that module rather than in
:mod:`nodum.models`). They are this surface's shapes; nothing else emits them.

## The one rule everything here is built on (E2)

**A schema fixes the envelope and nothing else.** Measured on the local model:
asked a question its supplied context could not answer, under a JSON schema, it
returned ``{"answer": "false", "cited_ids": [], "answered": true}`` — well
formed, schema-valid, and claiming to have answered a question it could not.
Free-form, the same call produced visibly broken text, which a caller retries
or degrades on. **The schema converted a visible failure into an invisible
one.**

So ``answered`` is never read from the model. It is computed:

1. Every id the model cites is resolved against the items this request actually
   retrieved (:func:`resolve_citation`). Ids outside that set are dropped and
   reported in ``unresolved``.
2. **After validation, zero surviving citations means not answered**, whatever
   the model said, and the answer text is not returned.

Two further measurements shape the parsing. The model cited ``n2`` (the wrong
node) for an answer that was in ``n3``, and it returned ``["id=n0", …]`` —
echoing the prompt's literal ``id=`` prefix rather than the id behind it. So a
citation is read defensively (brackets, quotes, an ``id=`` prefix) and then
validated, and a citation that survives parsing but names nothing retrieved is
worth exactly as much as one that does not parse.

The schema deliberately has **no ``answered`` field at all**. A field nobody
may read is a field that should not be in the contract, because the next reader
will wire it up.

## What the prompt may contain, measured rather than reasoned

The first version of ``/ask`` scored **1 of 6** on a six-question battery
against ``llama3.2:1b``, and every failure was a citation that did not parse —
``"]"``, ``"/1"``, ``"space main"``, and once a chat template's own
``<|start_header_id|>`` marker where a note number belonged. The validation
above was doing exactly its job and the endpoint was useless, which is the
distinction between a correct rule and a working one. Three changes took the
same battery to **6 of 6**, and each is a property a test now pins:

* **The citation format is a grammar, not an instruction**
  (:data:`_CITATION_PATTERN`). A ``pattern`` on the schema's array items is
  enforced by the server's constrained decoding, so every string above becomes
  unrepresentable rather than merely discouraged.
* **A note is identified by a small integer and nothing else**
  (:func:`_context_block`). With the 32-hex node id printed beside the marker
  the model cited ``"116"`` and ``"749"`` — mining the id for digits.
* **The instructions contain no number the model can copy.** An earlier prompt
  gave a worked example (``write exactly: ["1", "3"]``) and the model returned
  ``"3"`` on *every* call. It scored better that way — 5 of 6 — and was still
  wrong: on this battery marker 3 did not exist so validation dropped it, and
  on a graph where the search returns three hits that copied number resolves to
  a real note the answer did not come from.

The general lesson, and the one to keep when the models change: **every number
in the prompt is a candidate citation.** Put exactly the ones there that mean
something.

## What a failure looks like from here

Never a traceback, never silence, and never an empty answer. Every way a
provider call can fail — no provider configured, an unreachable one, a prompt
that will not fit, a filled context, an output ceiling, an exhausted budget —
becomes ``answered: false`` with a ``refusal`` sentence and the run's own
accounting in ``used``. That accounting is :class:`nodum.agent.LLMReport`, the
same object a cycle files under ``report["llm"]``, so a request's cost and a
night's cost are one shape; ``used.available`` and ``used.unavailable_reason``
are where the provider's absence is stated, exactly as they are for a cycle.

An **output ceiling is a failure, not a short answer** (B3): the body at
``finish_reason: "length"`` is cut mid-string and does not parse. The runtime
raises rather than handing one back, and rendering that as an empty answer
would be presenting no result as a result.

## Uncertainty is reported, not smoothed over

A 1B model is weak, and the design record's mitigation for that ("conservative
thresholds, journal the acceptance rates") is necessary and not sufficient. So
every result here carries what it was built from — ``considered`` (what the
retrieval offered), ``citations`` (what survived validation) and ``unresolved``
(what the model named that does not exist) — and the caller can see the gap.
Nothing here scores or thresholds the model's own confidence: it was measured
uncalibrated (0.9 on a wrong accept, 0.0 on a correct reject), so the schema
does not ask for it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nodum import agent, service
from nodum import search as search_module
from nodum.models import SearchHit
from nodum.principal import Principal

__all__ = [
    "AskOut",
    "Citation",
    "Offered",
    "ProviderStatus",
    "QueryRewrite",
    "NaturalSearchOut",
    "SummaryOut",
    "ask",
    "natural_search",
    "provider_status",
    "resolve_citation",
    "summarize",
]

#: Hits ``ask`` retrieves by default. Small on purpose: the prompt is the
#: expensive half (measured: 2 395 prompt tokens cost 47 s on the local model),
#: and a question answered from six nodes is a question whose citations a human
#: can check.
DEFAULT_ASK_K = 6

#: The ceiling on ``k`` (design E1). Clamped rather than refused, which is
#: ``service.subgraph``'s rule for the same reason: a caller passing an enormous
#: cap gets the ceiling, not the graph.
MAX_ASK_K = 20

#: Nodes ``summarize`` puts in front of the model. The same bound the
#: abstraction job's cluster size uses, and for the same reason — a dozen nodes
#: of node text is comfortably inside the measured 4 096-token window, and forty
#: is not.
MAX_SUMMARY_NODES = 12

#: How deep ``summarize`` walks by default, and the furthest it will.
DEFAULT_SUMMARY_DEPTH = 1
MAX_SUMMARY_DEPTH = 3

#: The widest excerpt one node contributes to a prompt, and the narrowest worth
#: sending. The context is fitted between them (:func:`_fit_prompt`).
#:
#: **This is E1's bound, not the truncation B3 forbids**, and the difference is
#: which ceiling is being fitted to. B3 rejects shortening a prompt to fit the
#: remaining *spending* budget, because the resulting worse answer is
#: indistinguishable from a good one and nothing records that it happened. This
#: fits the assembled context to the model's *context window* before anything is
#: counted or sent, which E1 names as ``/ask``'s bound in as many words — and
#: what reached the model is reported in ``considered``, so a note that was
#: dropped is a note the caller can see was dropped.
MAX_CONTEXT_CHARS = 1200
MIN_CONTEXT_CHARS = 240

#: Terms a query rewrite may contribute. One call, bounded output, bounded
#: query — a rewrite that could return fifty terms would be a rewrite that can
#: turn one search into a scan.
MAX_REWRITE_TERMS = 8

#: Output tokens the reachability probe asks for. Sized to be cheap rather than
#: useful: the probe's question is whether anything answers, and a live server
#: hitting this ceiling answers it just as well as one that stops on its own.
PROBE_OUTPUT_TOKENS = 8

#: Wall clock the reachability probe waits. A failure is fast and legible —
#: nothing listening is a refused connection in well under a millisecond, and a
#: model the server does not have is an HTTP 404 in about one — so a long
#: ceiling here only lengthens the *success* path, where a first call may have
#: to load the model.
PROBE_TIMEOUT_SECONDS = 30.0


ASK_TEMPLATE = """You answer questions from a personal knowledge graph.

Answer ONLY from the numbered notes below. If they do not contain the answer,
say so plainly in `answer` and return an empty `cited` list — that is a correct
outcome, not a failure.

Return JSON with two keys:
  answer: your answer, in at most four sentences.
  cited:  the number of every note the answer came from, each as a string of
          plain digits — digits only, no brackets, no titles, no punctuation.

Cite a note only if the answer is actually in it. Never cite a number that is
not in the list below.

Notes:
{context}

Question: {question}
"""

SUMMARIZE_TEMPLATE = """You summarise a region of a personal knowledge graph.

Summarise ONLY the numbered notes below, in at most six sentences. Do not add
anything the notes do not say.

Return JSON with two keys:
  summary: the summary.
  cited:   the number of every note the summary drew on, each as a string of
           plain digits — digits only, no brackets, no titles, no punctuation.

Never cite a number that is not in the list below.

Notes:
{context}
"""

REWRITE_TEMPLATE = """You turn a question into keyword search terms.

Return JSON with one key:
  terms: up to {max_terms} search terms, as single words or short phrases.

Use the question's own words wherever they are specific. Do not invent
terminology the question does not use.

Question: {question}
"""

#: :func:`nodum.agent.prompt_version` of each template (A2). Computed once at
#: import time from the constant, so it changes when and only when the template
#: does — which is what stops a journal reporting two prompts as one model.
ASK_PROMPT_VERSION = agent.prompt_version(ASK_TEMPLATE)
SUMMARIZE_PROMPT_VERSION = agent.prompt_version(SUMMARIZE_TEMPLATE)
REWRITE_PROMPT_VERSION = agent.prompt_version(REWRITE_TEMPLATE)

#: The structured-output schemas. Each one is an envelope and **nothing more**:
#: there is deliberately no ``answered`` and no ``confidence``, because the
#: measurement says the first is a lie and the second is not a quantity.
#: What a citation is *allowed* to be, as a grammar rather than an instruction.
#: Measured: the local models return ``"]"``, ``"/1"``, ``"space main"`` and —
#: once — a chat template's own ``<|start_header_id|>`` marker where a note
#: number belongs. A ``pattern`` here is enforced by the server's constrained
#: decoding (verified against ``ollama``), so those strings become
#: *unrepresentable* rather than merely discouraged.
#:
#: It narrows the envelope and proves nothing about the content: the model can
#: still emit ``"3"`` for a note that says nothing of the sort, which is why
#: :func:`resolve_citation` validates against the retrieval regardless. Nothing
#: here replaces that check; it removes one whole class of noise from in front
#: of it.
_CITATION_PATTERN = "^[0-9]{1,3}$"

ASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "cited": {"type": "array", "items": {"type": "string", "pattern": _CITATION_PATTERN}},
    },
    "required": ["answer", "cited"],
    "additionalProperties": False,
}

SUMMARIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "cited": {"type": "array", "items": {"type": "string", "pattern": _CITATION_PATTERN}},
    },
    "required": ["summary", "cited"],
    "additionalProperties": False,
}

REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
    "required": ["terms"],
    "additionalProperties": False,
}

#: Prefixes stripped from a citation before it is matched. The first one is
#: measured: asked for ids, the model returned ``["id=n0", "id=n1", …]``,
#: copying the prompt's literal label out of the context block.
_CITATION_PREFIXES = ("node_id=", "node_id:", "node=", "note=", "id=", "id:", "#")

#: Characters stripped from both ends of a citation. A model that is asked for
#: ``[3]`` returns ``[3]``, ``"3"`` or ``3`` depending on the day.
_CITATION_TRIM = " \t\r\n[](){}<>\"'`.,;:"


class Offered(BaseModel):
    """One item this request put in front of the model, and its real identity.

    The ``marker`` is what the prompt asks the model to cite: a small integer a
    weak model can copy, where a 32-hex node id is a string it measurably
    cannot. It is not a weakening of E2's rule — the markers are a bijection
    with the ids actually retrieved, so validating a marker against them *is*
    validating against the retrieval.

    ``node_id`` is the real identity, and it never reaches the prompt (see
    :func:`_context_block`). :func:`resolve_citation` still accepts one,
    because a provider or a model that echoes an id costs nothing to
    understand and the check against the retrieval is identical either way.
    """

    marker: int
    node_id: str
    title: str | None
    space_id: str | None
    text: str


class Citation(BaseModel):
    """One citation that survived validation: a node the caller can go and read."""

    marker: int
    node_id: str
    title: str | None
    space_id: str | None


class AskOut(BaseModel):
    """One answer, or one honest refusal to answer (E1/E2).

    ``answered`` is computed from :attr:`citations`, never taken from the model.
    When it is false, :attr:`answer` is ``None``: an answer whose citations do
    not resolve is not a shorter answer, it is no answer, and returning the text
    beside ``answered: false`` would put a sentence on a screen that the
    interface has just said it cannot stand behind.

    Three lists say what this answer was built from, and they are three
    different facts. ``considered`` is what reached the model. ``dropped`` is
    what the retrieval found and the context window could not carry — reported
    because a note that was found and never read is the one thing a human would
    otherwise have no way to notice. ``unresolved`` is what the model named that
    this retrieval did not return: the honest measure of how far it drifted, and
    on a weak model the field a reader learns the most from.
    """

    question: str
    k: int
    answered: bool
    answer: str | None
    citations: list[Citation]
    considered: list[str]
    dropped: list[str]
    unresolved: list[str]
    refusal: str | None
    used: agent.LLMReport


class SummaryOut(BaseModel):
    """One summary of a node's neighbourhood, or an honest refusal.

    ``summarized`` follows :attr:`AskOut.answered`'s rule, and it is worth being
    precise about what it does and does not prove. The context here definitely
    contains the material — it is the subgraph that was asked for — so a
    resolvable citation does not prove the summary is faithful. What it proves
    is that the model addressed the notes it was given rather than composing
    from nothing, which is the failure a schema-valid confabulation looks like.

    ``truncated`` and ``dropped`` are two different partial reads and are kept
    apart: the first is the subgraph *walk* stopping at its cap, the second is
    the context window refusing notes the walk did return.
    """

    node_id: str
    depth: int
    summarized: bool
    summary: str | None
    citations: list[Citation]
    considered: list[str]
    dropped: list[str]
    unresolved: list[str]
    truncated: bool
    refusal: str | None
    used: agent.LLMReport


class QueryRewrite(BaseModel):
    """What was asked on the human's behalf, and whether it was asked at all.

    Returned beside the results so the rewrite is visible rather than implied:
    a human who gets a surprising result list can see the words the search
    actually ran with, and re-run with their own.
    """

    requested: bool
    applied: bool
    terms: list[str]
    original: str
    refusal: str | None
    used: agent.LLMReport | None


class NaturalSearchOut(BaseModel):
    """A :class:`~nodum.models.SearchResult` plus the rewrite that produced it.

    The same three fields ``SearchResult`` carries, so a client parses one
    shape whether or not it asked for the rewrite, plus ``rewrite``. ``query``
    is what was **searched**, which is the rewrite when one applied and the
    human's own words when it did not.
    """

    query: str
    k: int
    hits: list[SearchHit]
    rewrite: QueryRewrite


class ProviderStatus(BaseModel):
    """Whether a provider is configured, and — separately — whether it answers.

    The two are deliberately different facts, and :mod:`nodum.llm` keeps them
    apart on purpose: resolution reads configuration and makes no network call,
    because a server that is down at 03:00 and up at 03:05 is not a
    configuration change. So :attr:`reachable` is a *tri-state*. ``None`` means
    the question was not asked — either nothing is configured to ask, or the
    caller declined the probe — and reporting that as ``false`` would say a
    server is down when nobody ever knocked.
    """

    configured: bool
    provider: str | None
    model: str | None
    context_tokens: int | None
    reachable: bool | None
    detail: str | None
    probe_ms: int | None
    budget_tokens: int
    budget_seconds: float
    max_output_tokens: int
    call_timeout: float


def resolve_citation(raw: Any, offered: list[Offered]) -> Offered | None:
    """Resolve one thing the model called a citation, or ``None`` (E2).

    Parsing is deliberately generous and matching is deliberately strict. The
    generosity is measured — the model wraps its answer in brackets, quotes it,
    and echoes the prompt's ``id=`` label — and it costs nothing, because
    whatever comes out of the parse still has to *be* something this request
    retrieved. The strictness is the whole defence: a citation that names a node
    the search never returned is not a weaker citation, it is a made-up one.

    It stays generous even though :data:`_CITATION_PATTERN` now makes most of
    that noise unrepresentable, because the pattern is enforced by *the
    provider*: a server that ignores ``response_format``, an operator who points
    at one that does not support it, and every future provider are all cases
    where this function is the only thing left standing between the model's
    prose and a node id.

    Args:
        raw: Whatever the model put in its ``cited`` list.
        offered: What this request actually put in front of it.

    Returns:
        The item cited, or ``None`` when nothing offered answers to it.
    """
    text = str(raw).strip().strip(_CITATION_TRIM)
    lowered = text.casefold()
    for prefix in _CITATION_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip(_CITATION_TRIM)
            break
    if not text:
        return None
    folded = text.casefold()
    for item in offered:
        if folded == str(item.marker) or folded == item.node_id.casefold():
            return item
    return None


def _validate_citations(
    raw_citations: Any, offered: list[Offered]
) -> tuple[list[Citation], list[str]]:
    """Split what the model cited into what resolves and what does not.

    Order is the model's and duplicates are collapsed, so a model that cites the
    same note three times does not turn one source into three.
    """
    if not isinstance(raw_citations, list):
        return [], []
    resolved: list[Citation] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for raw in raw_citations:
        item = resolve_citation(raw, offered)
        if item is None:
            unresolved.append(str(raw))
            continue
        if item.node_id in seen:
            continue
        seen.add(item.node_id)
        resolved.append(
            Citation(
                marker=item.marker,
                node_id=item.node_id,
                title=item.title,
                space_id=item.space_id,
            )
        )
    return resolved, unresolved


def _excerpt(text: str, limit: int) -> str:
    """One node's text, bounded and marked when it was cut."""
    collapsed = text.strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + " …[truncated]"


def _context_block(offered: list[Offered], limit: int) -> str:
    """Render what was retrieved as the numbered notes the prompt refers to.

    **A note is identified by a small integer and by nothing else**, and both
    halves of that are measured.

    The marker is there because a 1B model cannot reliably copy a 32-hex node
    id, and a citation it cannot reproduce is a citation that never resolves.
    The *id* is absent because putting it beside the marker made things worse in
    a way nothing predicted: asked for a note number with
    ``11660be27af84685afe404f31e02253a`` in front of it, the model cited
    ``"116"`` and ``"749"`` — mining the id for digits. Every extra number in a
    prompt is another thing that can come back as a citation, so the prompt
    carries exactly one number per note and the caller owns the mapping back to
    the graph.
    """
    return "\n\n".join(
        f"[{item.marker}] {item.title or '(untitled)'}\n{_excerpt(item.text, limit) or '(no text)'}"
        for item in offered
    )


def _fit_prompt(
    active: agent.AgentRun, template: str, offered: list[Offered], **fields: str
) -> tuple[list[Offered], agent.Message | None]:
    """Assemble the largest prompt that fits the model's window (E1).

    The measured failure this exists to prevent: a 51 KB prompt and a 207 KB
    prompt both report **4 096 prompt tokens and the same wall time** on the
    local model — the window filled, everything past it was dropped, and
    nothing in the response says so. A retrieval-backed ``/ask`` that serialises
    a subgraph and posts it is, by default, an endpoint that answers from a
    prefix. :mod:`nodum.llm` refuses such a prompt rather than sending it, which
    turns the silent failure into a visible one; this is what stops the visible
    one being the *ordinary* outcome.

    Two levers, in order. **Narrow the excerpts first** — every note stays in
    the prompt and each says less — because a shorter excerpt of the
    top-ranked note is worth more than the absence of it. Only when the
    excerpts are already at :data:`MIN_CONTEXT_CHARS`, below which an excerpt
    says nothing at all, does it **drop the worst-ranked note**. The estimate is
    the provider's own, so what is fitted is exactly what would be refused.

    Args:
        active: The run, for the provider's estimate and the output reservation.
        template: The prompt template, with a ``{context}`` field.
        offered: What was retrieved, best first.
        fields: The template's other fields.

    Returns:
        The notes that fit and the message carrying them, or ``([], None)`` when
        not even one note at the narrowest excerpt fits — which means the
        template plus the question already fill the window.
    """
    provider = active.provider
    if provider is None:
        return [], None
    ceiling = provider.context_tokens - active.max_output_tokens
    items = list(offered)
    limit = MAX_CONTEXT_CHARS
    while items:
        message = agent.Message(
            role="user", content=template.format(context=_context_block(items, limit), **fields)
        )
        if provider.estimate_prompt_tokens([message]) <= ceiling:
            return items, message
        if limit > MIN_CONTEXT_CHARS:
            limit = max(MIN_CONTEXT_CHARS, limit // 2)
            continue
        items.pop()
        limit = MAX_CONTEXT_CHARS
    return [], None


def _decode(text: str) -> dict[str, Any] | None:
    """Read the model's reply as the object the schema asked for, or ``None``.

    A schema makes this reliable and not certain: a provider that ignores
    ``response_format``, or a model asked without one, answers with prose. That
    is a failure with a name here rather than a ``KeyError`` two lines later.
    """
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _refusal_for(exc: Exception) -> str:
    """One sentence saying what stopped this call, in the caller's language.

    Every provider failure already carries a message written for a human — the
    runtime's budget refusals name the numbers, the provider's name the endpoint
    — so this adds the fact those messages cannot know: that the request is
    coming back unanswered rather than being retried.
    """
    return str(exc)


def _no_answer(reason: str) -> str:
    return reason


def _node_text(hit: SearchHit, *, principal: Principal, path: str | Path | None) -> str:
    """The node's own text for the prompt, falling back to the search snippet.

    A snippet is 200 characters with match markers in it — enough to *rank* a
    node and not enough to answer from, so the answer is built from the node
    rather than from the index's summary of it. The extra read per hit is one
    bounded, grant-scoped ``get_node``: the hit came from this principal's own
    search, so it is visible, and a node deleted in the moment between the two
    reads falls back to the snippet rather than failing the request.
    """
    try:
        node = service.get_node(hit.node_id, principal=principal, path=path)
    except service.RecordNotFound:
        return hit.snippet
    return node.content.strip() or hit.snippet


def _run_for(purpose: str, principal: Principal, run: agent.AgentRun | None) -> agent.AgentRun:
    """This request's runtime — the caller's, or one built for it.

    Callers pass their own in tests and wherever a budget has already been
    decided; the surfaces let this build one, which reads the request budget
    from the environment. Either way it is the *only* way to a provider call.
    """
    return run if run is not None else agent.for_request(purpose=purpose, principal=principal)


def ask(
    question: str,
    *,
    k: int = DEFAULT_ASK_K,
    space: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
    run: agent.AgentRun | None = None,
) -> AskOut:
    """Answer a question from the graph, with citations, or say it could not.

    One retrieval and one model call, in that order, and the retrieval is what
    bounds the call: the model sees the ``k`` hits and nothing else, so the
    prompt's size is a function of ``k`` rather than of the graph.

    **It writes nothing** (E1), and it never answers from a provider that is not
    there: with none configured the refusal names the variable to set rather
    than being a 500, a traceback, or an empty string.

    Args:
        question: The human's question, as they typed it.
        k: Hits to retrieve, clamped to :data:`MAX_ASK_K`.
        space: Optional space to narrow the retrieval to. It composes with the
            principal's scope like every other space filter — it narrows and
            never widens.
        principal: Who is asking. Every read is theirs, so a node they cannot
            read is a node the answer cannot come from.
        path: Explicit database path.
        run: An explicit runtime, overriding the per-request default.

    Returns:
        The answer with its validated citations, or ``answered: false`` with a
        ``refusal`` saying why. Both shapes carry ``used``.

    Raises:
        ValueError: If the question is empty, if ``k`` is below 1, or if the
            space does not resolve. These are the caller's errors, which is why
            they raise rather than becoming a refusal: a refusal says the
            *model* could not answer, and saying that about a malformed request
            would hide a bug in the client.
    """
    question = question.strip()
    if not question:
        raise ValueError("question must not be empty")
    service.require_positive_limit(k, "k")
    k = min(k, MAX_ASK_K)
    active = _run_for("ask", principal, run)
    dropped: list[str] = []

    if not active.available:
        return AskOut(
            question=question,
            k=k,
            answered=False,
            answer=None,
            citations=[],
            considered=[],
            dropped=dropped,
            unresolved=[],
            refusal=active.unavailable_reason,
            used=active.report(),
        )

    result = search_module.search(question, k=k, space=space, principal=principal, path=path)
    retrieved = [
        Offered(
            marker=index,
            node_id=hit.node_id,
            title=hit.title,
            space_id=hit.space_id,
            text=_node_text(hit, principal=principal, path=path),
        )
        for index, hit in enumerate(result.hits, start=1)
    ]
    if not retrieved:
        return AskOut(
            question=question,
            k=k,
            answered=False,
            answer=None,
            citations=[],
            considered=[],
            dropped=dropped,
            unresolved=[],
            refusal=(
                "the search found nothing to answer from, so no model call was made. "
                "Try different words, or widen the space filter"
            ),
            used=active.report(),
        )

    offered, message = _fit_prompt(active, ASK_TEMPLATE, retrieved, question=question)
    considered = [item.node_id for item in offered]
    dropped = [item.node_id for item in retrieved[len(offered) :]]
    if message is None:
        return AskOut(
            question=question,
            k=k,
            answered=False,
            answer=None,
            citations=[],
            considered=[],
            dropped=[item.node_id for item in retrieved],
            unresolved=[],
            refusal=(
                "the question alone fills this model's context window, so no note could be sent "
                "with it. Ask a shorter question, or configure a model with a wider window"
            ),
            used=active.report(),
        )
    try:
        generation = active.chat([message], prompt_version=ASK_PROMPT_VERSION, schema=ASK_SCHEMA)
    except (agent.BudgetExhausted, agent.LLMError) as exc:
        return AskOut(
            question=question,
            k=k,
            answered=False,
            answer=None,
            citations=[],
            considered=considered,
            dropped=dropped,
            unresolved=[],
            refusal=_refusal_for(exc),
            used=active.report(),
        )

    decoded = _decode(generation.text)
    if decoded is None:
        return AskOut(
            question=question,
            k=k,
            answered=False,
            answer=None,
            citations=[],
            considered=considered,
            dropped=dropped,
            unresolved=[],
            refusal=(
                "the model's reply was not the JSON object the schema asked for, so there is "
                "nothing to validate"
            ),
            used=active.report(),
        )

    answer = decoded.get("answer")
    answer = answer.strip() if isinstance(answer, str) else ""
    citations, unresolved = _validate_citations(decoded.get("cited"), offered)
    answered = bool(citations) and bool(answer)
    refusal = None
    if not citations:
        refusal = _no_answer(
            "the model cited nothing this search returned, so the answer is not returned. "
            "A citation that does not resolve to a node you can read is not a citation"
        )
    elif not answer:
        refusal = _no_answer("the model returned citations and no answer text")
    return AskOut(
        question=question,
        k=k,
        answered=answered,
        answer=answer if answered else None,
        citations=citations,
        considered=considered,
        dropped=dropped,
        unresolved=unresolved,
        refusal=refusal,
        used=active.report(),
    )


def summarize(
    node_id: str,
    *,
    depth: int = DEFAULT_SUMMARY_DEPTH,
    principal: Principal,
    path: str | Path | None = None,
    run: agent.AgentRun | None = None,
) -> SummaryOut:
    """Summarise a node and its neighbourhood, reading only (E1).

    The subgraph is the bound: :func:`nodum.service.subgraph` is already capped
    twice and reports ``truncated``, so the prompt cannot grow with the graph
    and the result says when the walk stopped early.

    **Nothing writes.** Design E1 offers an opt-in ``propose=true`` that files
    the summary as a reviewable ``proposed`` version; it is deliberately not in
    this wave, because 5b-i is cut exactly at the point where a model call
    causes a write and the whole argument for that cut is that the read-only
    half ships and is judged first.

    Args:
        node_id: The node at the centre.
        depth: Hops to walk, clamped to :data:`MAX_SUMMARY_DEPTH`.
        principal: Who is asking.
        path: Explicit database path.
        run: An explicit runtime, overriding the per-request default.

    Returns:
        The summary with its validated citations, or ``summarized: false`` with
        a ``refusal``.

    Raises:
        RecordNotFound: If the node does not resolve, or sits in a space this
            principal cannot read — the service's rule, unchanged.
        ValueError: If ``depth`` is negative.
    """
    if depth < 0:
        raise ValueError("depth must not be negative")
    depth = min(depth, MAX_SUMMARY_DEPTH)
    active = _run_for("summarize", principal, run)
    dropped: list[str] = []

    # The read happens first even when there is no provider, because it is what
    # says whether this node exists at all: refusing an unreadable id with "no
    # LLM provider configured" would answer the wrong question.
    region = service.subgraph(
        node_id, depth=depth, principal=principal, limit=MAX_SUMMARY_NODES, path=path
    )
    retrieved = [
        Offered(
            marker=index,
            node_id=node.id,
            title=node.title,
            space_id=node.space_id,
            text=node.content,
        )
        for index, node in enumerate(region.nodes, start=1)
    ]

    if not active.available:
        return SummaryOut(
            node_id=node_id,
            depth=depth,
            summarized=False,
            summary=None,
            citations=[],
            considered=[item.node_id for item in retrieved],
            dropped=dropped,
            unresolved=[],
            truncated=region.truncated,
            refusal=active.unavailable_reason,
            used=active.report(),
        )

    offered, message = _fit_prompt(active, SUMMARIZE_TEMPLATE, retrieved)
    considered = [item.node_id for item in offered]
    dropped = [item.node_id for item in retrieved[len(offered) :]]
    if message is None:
        return SummaryOut(
            node_id=node_id,
            depth=depth,
            summarized=False,
            summary=None,
            citations=[],
            considered=[],
            dropped=[item.node_id for item in retrieved],
            unresolved=[],
            truncated=region.truncated,
            refusal=(
                "not one of these notes fits this model's context window at its narrowest "
                "excerpt. Configure a model with a wider window"
            ),
            used=active.report(),
        )
    try:
        generation = active.chat(
            [message], prompt_version=SUMMARIZE_PROMPT_VERSION, schema=SUMMARIZE_SCHEMA
        )
    except (agent.BudgetExhausted, agent.LLMError) as exc:
        return SummaryOut(
            node_id=node_id,
            depth=depth,
            summarized=False,
            summary=None,
            citations=[],
            considered=considered,
            dropped=dropped,
            unresolved=[],
            truncated=region.truncated,
            refusal=_refusal_for(exc),
            used=active.report(),
        )

    decoded = _decode(generation.text)
    if decoded is None:
        return SummaryOut(
            node_id=node_id,
            depth=depth,
            summarized=False,
            summary=None,
            citations=[],
            considered=considered,
            dropped=dropped,
            unresolved=[],
            truncated=region.truncated,
            refusal=(
                "the model's reply was not the JSON object the schema asked for, so there is "
                "nothing to validate"
            ),
            used=active.report(),
        )

    summary = decoded.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""
    citations, unresolved = _validate_citations(decoded.get("cited"), offered)
    summarized = bool(citations) and bool(summary)
    refusal = None
    if not citations:
        refusal = _no_answer(
            "the model cited none of the notes it was given, so the summary is not returned"
        )
    elif not summary:
        refusal = _no_answer("the model returned citations and no summary text")
    return SummaryOut(
        node_id=node_id,
        depth=depth,
        summarized=summarized,
        summary=summary if summarized else None,
        citations=citations,
        considered=considered,
        dropped=dropped,
        unresolved=unresolved,
        truncated=region.truncated,
        refusal=refusal,
        used=active.report(),
    )


def _rewrite_terms(
    question: str, active: agent.AgentRun
) -> tuple[list[str], str | None, agent.LLMReport]:
    """Ask the model for search terms. Returns the terms, a refusal, and the cost."""
    messages = [
        agent.Message(
            role="user",
            content=REWRITE_TEMPLATE.format(question=question, max_terms=MAX_REWRITE_TERMS),
        )
    ]
    try:
        # No per-call output ceiling. A rewrite needs about fifty tokens, and an
        # earlier version said so with `max_output_tokens=200` — which
        # **failed every single call** against `qwen3:8b`, because a reasoning
        # model spends its thinking tokens out of the same allowance and hits
        # the ceiling before it writes any JSON. B3 then does exactly what it
        # should (a `length` finish is a failed call, the body discarded) and
        # the feature is simply off on that model. A tight ceiling is not a
        # saving, it is a model-compatibility setting in disguise; the run's own
        # `NODUM_LLM_MAX_OUTPUT_TOKENS` is the one knob, and it is a human's.
        generation = active.chat(
            messages, prompt_version=REWRITE_PROMPT_VERSION, schema=REWRITE_SCHEMA
        )
    except (agent.BudgetExhausted, agent.LLMError) as exc:
        return [], _refusal_for(exc), active.report()

    decoded = _decode(generation.text)
    if decoded is None:
        return (
            [],
            "the model's reply was not the JSON object the schema asked for",
            active.report(),
        )
    raw_terms = decoded.get("terms")
    if not isinstance(raw_terms, list):
        return [], "the model returned no terms", active.report()

    terms: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        term = str(raw).strip() if raw is not None else ""
        # A term is split on whitespace by the matcher anyway, so a phrase is
        # already several terms; what has to be dropped here is the empty
        # string, which would be one more nothing in the query.
        if not term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        terms.append(term)
        if len(terms) == MAX_REWRITE_TERMS:
            break
    return terms, None, active.report()


def natural_search(
    query: str,
    *,
    k: int = 10,
    state: str | None = "active",
    type: str | None = None,
    created_by: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    include_meta: bool = False,
    space: str | None = None,
    expand: bool = False,
    principal: Principal,
    path: str | Path | None = None,
    run: agent.AgentRun | None = None,
) -> NaturalSearchOut:
    """Search with a model-written query, layered on the ordinary matcher (E3).

    **A rewrite, not a retrieval.** The model contributes search *terms*; every
    ranked signal, every filter and every cap is
    :func:`nodum.search.search`'s, unchanged. So the rewrite cannot make search
    better or worse at anything except choosing words, and with no provider it
    is a no-op that says so — search must work without one.

    **It layers on the quorum rather than replacing it, and the layering is
    what makes it safe.** The measurement that shaped this: asked to rewrite
    *"What did I write about how exactly-once semantics work in Kafka?"*, the
    local model returned ``["semantics", "once-once semantics", "Kafka", "data
    processing"]`` — inventing a term, and dropping the one that discriminates.
    Under the conjunctive matcher that rewrite replaced, ``"once-once
    semantics"`` alone matched nothing and zeroed the result set; under the
    quorum a term the index has never seen carries ``df = 0`` and is dropped
    before the quorum is computed, so one hallucination costs nothing. The
    rewrite is a prerequisite's dependent, not its substitute.

    **The model's terms replace the human's rather than joining them**, which is
    E3 as designed. The alternative — searching the union — was considered and
    not taken: the quorum's bar is a fraction of the query's *total*
    discriminating weight, so adding real-but-different rare terms raises the
    bar that the human's own terms then have to clear, and a rewrite that made
    a good query harder to satisfy would be a worse failure than one that
    changes the words visibly. What makes replacement safe to ship is that it is
    visible: :attr:`QueryRewrite.original` and :attr:`QueryRewrite.terms` are
    both returned, so a human sees what was asked on their behalf.

    Args:
        query: The human's question, as they typed it.
        k: Maximum hits, as :func:`nodum.search.search` means it.
        state: Node-state filter, as :func:`nodum.search.search` means it.
        type: Node-type filter.
        created_by: Writer filter.
        created_after: Lower time bound.
        created_before: Upper time bound.
        include_meta: Include meta-space nodes in an unnarrowed search.
        space: Narrow to one space.
        expand: Append one-hop neighbours of the fused hits.
        principal: Who is asking.
        path: Explicit database path.
        run: An explicit runtime, overriding the per-request default.

    Returns:
        The ordinary result plus the rewrite that produced it. ``query`` is what
        was searched.

    Raises:
        ValueError: Everything :func:`nodum.search.search` raises, unchanged.
    """
    original = query.strip()
    if not original:
        raise ValueError("query must contain at least one term")
    active = _run_for("search-rewrite", principal, run)

    if not active.available:
        rewrite = QueryRewrite(
            requested=True,
            applied=False,
            terms=[],
            original=original,
            refusal=active.unavailable_reason,
            used=active.report(),
        )
    else:
        terms, refusal, used = _rewrite_terms(original, active)
        rewrite = QueryRewrite(
            requested=True,
            applied=bool(terms),
            terms=terms,
            original=original,
            refusal=refusal
            or (None if terms else "the model produced no usable terms, so your words were used"),
            used=used,
        )

    searched = " ".join(rewrite.terms) if rewrite.applied else original
    result = search_module.search(
        searched,
        k=k,
        state=state,
        type=type,
        created_by=created_by,
        created_after=created_after,
        created_before=created_before,
        include_meta=include_meta,
        space=space,
        expand=expand,
        principal=principal,
        path=path,
    )
    return NaturalSearchOut(query=result.query, k=result.k, hits=result.hits, rewrite=rewrite)


def provider_status(*, principal: Principal, probe: bool = True) -> ProviderStatus:
    """Report whether a provider is configured and whether it answers.

    Two questions, asked separately on purpose. :func:`nodum.llm.get_provider`
    resolves from configuration and makes no network call, so "configured" is
    free and permanent while "reachable" is a fact about this instant — and
    conflating them is how an install reports itself broken because a server was
    restarting.

    **The probe is one real model call**, because that is the only thing that
    answers the question, and it is cheap in exactly the case that matters:
    nothing listening is a refused connection in well under a millisecond, and a
    model the server does not have is an HTTP 404 in about one. A live server
    costs a handful of tokens.

    It goes through :class:`nodum.agent.AgentRun` like every other provider call
    (P3), which is why this takes a principal: a status command that spent a
    model call with nobody named would be the one place in this system where
    something is spent unattributed.

    Args:
        principal: Who asked. The probe is metered against their request budget.
        probe: Whether to make the call at all. ``False`` reports the
            configuration and leaves ``reachable`` unasked.

    Returns:
        The configuration, the probe's verdict, and the ceilings a request would
        spend under.
    """
    active = agent.for_request(purpose="llm-status", principal=principal)
    provider = active.provider
    status = ProviderStatus(
        configured=active.available,
        provider=active.provider_id,
        model=active.model_id,
        context_tokens=provider.context_tokens if provider is not None else None,
        reachable=None,
        detail=active.unavailable_reason,
        probe_ms=None,
        budget_tokens=active.budget.tokens,
        budget_seconds=active.budget.seconds,
        max_output_tokens=active.max_output_tokens,
        call_timeout=active.call_timeout,
    )
    if not status.configured or not probe:
        if status.configured and not probe:
            status.detail = "not probed"
        return status

    started = time.monotonic()
    try:
        active.chat(
            [agent.Message(role="user", content="ping")],
            prompt_version=ASK_PROMPT_VERSION,
            max_output_tokens=PROBE_OUTPUT_TOKENS,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        status.reachable = True
        status.detail = None
    except (agent.OutputTruncated, agent.ContextOverflow) as exc:
        # The server answered. Both of these are failures of the *answer* — the
        # body is unusable — and the probe is not asking for a usable answer,
        # it is asking whether anything is listening. Reading a ceiling this
        # probe itself set as "unreachable" would report every healthy server
        # as down, since a handful of output tokens is a `length` finish on
        # almost any reply.
        status.reachable = True
        status.detail = f"reachable; the probe's own ceiling bit ({exc.__class__.__name__})"
    except agent.ProviderUnavailable as exc:
        status.reachable = False
        status.detail = _refusal_for(exc)
    except (agent.PromptTooLong, agent.BudgetExhausted) as exc:
        # Neither reached the wire, so neither says anything about the server.
        # `reachable` stays None rather than becoming False: this is a local
        # configuration refusing before it knocked.
        status.detail = _refusal_for(exc)
    status.probe_ms = int((time.monotonic() - started) * 1000)
    return status
