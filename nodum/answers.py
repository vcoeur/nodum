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
3. **One surviving citation beside a citation that names nothing is not an
   answer either** (:func:`_ungrounded`). Live: an answer of ``AWS``, citing a
   Kafka textbook that contains no occurrence of AWS, cloud, Kubernetes or
   provider — and citing marker ``2`` as well, when exactly one note had been
   offered. The endpoint had the proof that the model was not reading the
   context, put it in ``unresolved``, and stood behind the other citation.
4. **A number the sent text does not contain is not an answer**
   (:func:`_unsupported_numbers`). Live: a source saying an escalation deadline
   is *fourteen minutes*, of which the model was shown a 1 213-character prefix
   that stopped well before that sentence, answered "…is 24 hours".

Two further measurements shape the parsing. The model cited ``n2`` (the wrong
node) for an answer that was in ``n3``, and it returned ``["id=n0", …]`` —
echoing the prompt's literal ``id=`` prefix rather than the id behind it. So a
citation is read defensively (brackets, quotes, an ``id=`` prefix) and then
validated, and a citation that survives parsing but names nothing retrieved is
worth exactly as much as one that does not parse.

The schema deliberately has **no ``answered`` field at all**. A field nobody
may read is a field that should not be in the contract, because the next reader
will wire it up.

## What ``answered: true`` does not mean

**Citation resolvability is not groundedness.** Rule 1 defends against an
invented *id*; it says nothing about invented *content citing a real id*, and
that is the failure mode a model actually has. Rules 3 and 4 are two cheap,
deterministic narrowings of the gap, and they are narrow on purpose — numbers
are checked and language is not, because deciding whether a sentence is
supported by a paragraph is a judgement and a judgement is a second model call.
An answer can pass every check here and be false. The envelope is built so a
reader can see that for themselves rather than being asked to trust a boolean:
``citations`` carries ``truncated`` per note, ``truncated_notes`` and
``dropped`` say what the model saw partly and not at all, and ``considered`` is
empty whenever no call was made.

## What the model is shown, and what the envelope says about it

**A bound that is not reported is a lie the caller cannot detect.** Each note
is excerpted to :data:`MAX_CONTEXT_CHARS` and the whole context is then fitted
to the window, so the ordinary case is that some note reached the model in
part. The prompt has always said ``…[truncated]`` to the *model*; the envelope
now says it to the caller, per offered note and per citation.

**Every ``[n]`` at the start of a line in the prompt is a note boundary, and a
node used to be able to write one** (:func:`_neutralise_markers`). Measured on
the shape ingestion produces: a ``source`` node whose text opened with ``[1]
Retention window`` moved its own sentence inside note 1's body, both local
models answered from it, and the 8B cited only the honest notes — so the
citations pointed a human at a note that says the opposite of the answer.

The rule is about **the prompt**, not about the graph, so it holds at both ends
of the template. ``ASK_TEMPLATE`` prints the question *under* the notes, and a
question carrying a line ``[3] …`` therefore opened one more note than the
retrieval offered: measured, ``llama3.2:1b`` came back citing ``2`` and ``3``
on a one-note graph. That is the caller's own text and no grant boundary is
crossed — what is crossed is the invariant :attr:`AskOut.citations` rests on,
that a note boundary is something this module wrote. Both the notes and the
question are defused before they are fitted; ``question`` in the envelope is
still what was typed.

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
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nodum import agent, service
from nodum import search as search_module
from nodum.models import NodeOut, SearchHit
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

#: Node states whose text may be put in front of the model, and the meta space
#: is excluded beside them. These are ``nodum.search.search``'s own defaults
#: (``state="active"``, ``include_meta=False``), restated here because
#: :func:`summarize` reaches its notes through :func:`nodum.service.subgraph`,
#: which filters *edges* by state and never filters nodes at all.
#:
#: The two endpoints disagreeing about this was a real defect: an archived node
#: was in the set ``/summarize`` would have sent to the provider while ``/ask``
#: could not reach it at any ``k``. Neither is a grant violation — the caller is
#: a human who can read every one of these rows — but a human archives a note to
#: take it out of circulation, and "circulation" has to include the one path
#: that puts its text on somebody else's machine.
SENDABLE_STATES = ("active",)

#: Terms a query rewrite may contribute. One call, bounded output, bounded
#: query — a rewrite that could return fifty terms would be a rewrite that can
#: turn one search into a scan.
MAX_REWRITE_TERMS = 8

#: Output tokens the query rewrite reserves, when the window has the room for
#: it (:func:`_rewrite_ceiling` never takes more than half) and when the human's
#: own ceiling is not already higher.
#:
#: **This is a floor, not a cap, and it is a model-compatibility setting.** A
#: rewrite needs about fifty tokens of JSON, and the shipped default of 512
#: output tokens looks like ten times enough — except that a reasoning model
#: spends its thinking out of the same allowance (``ollama`` charges ``<think>``
#: to ``completion_tokens`` and strips it from ``content``), so ``qwen3:8b``
#: answers every rewrite with an empty body at ``finish_reason: "length"``. B3
#: then correctly discards it and the feature is simply **off out of the box**
#: on that model, behind a message about a ceiling nobody chose. 2 048 is the
#: number measured to cure it.
#:
#: It is set here rather than in :mod:`nodum.agent`'s default because the
#: rewrite is the one call on this surface whose *prompt* is tiny: reserving
#: half the window costs it nothing, while doing the same to ``/ask`` would
#: halve the context every answer is built from.
REWRITE_OUTPUT_TOKENS = 2048

#: Output tokens the reachability probe asks for. Sized to be cheap rather than
#: useful: the probe's question is whether anything answers, and a live server
#: hitting this ceiling answers it just as well as one that stops on its own.
PROBE_OUTPUT_TOKENS = 8


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

#: A note boundary as :func:`_context_block` writes one: ``[`` digits ``]`` at
#: the start of a line. Matched against *graph text* so that a node whose
#: content carries one can be stopped from opening a second note inside note
#: *n*'s body — see :func:`_neutralise_markers`.
_LINE_MARKER = re.compile(r"^([ \t]*)\[([0-9]+)\]", re.MULTILINE)

#: A run of digits, which is the one kind of claim this module can check
#: deterministically against the text it sent (:func:`_unsupported_numbers`).
_NUMBER = re.compile(r"[0-9]+")


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

    ``text`` is the node's whole text and ``excerpt`` is **what was actually
    sent** — the two differ whenever :data:`MAX_CONTEXT_CHARS` or the fitting
    in :func:`_fit_prompt` cut the note short, and ``truncated`` says so. They
    are separate fields because every check downstream has to be made against
    the second one: a groundedness test run against text the model never saw
    would corroborate an answer out of material that was not in the prompt.
    """

    marker: int
    node_id: str
    title: str | None
    space_id: str | None
    state: str | None = None
    text: str
    excerpt: str = ""
    truncated: bool = False


class Citation(BaseModel):
    """One citation that survived validation: a node the caller can go and read.

    ``truncated`` is the load-bearing field. Without it a citation says "the
    answer came from this note" and means "the answer came from *some prefix
    of* this note", and a human who opens the node and finds the sentence there
    has confirmed nothing — the model may never have been shown that line.
    ``state`` is here for the same reason at one remove: what a note *is* is
    part of reading a citation honestly.
    """

    marker: int
    node_id: str
    title: str | None
    space_id: str | None
    state: str | None = None
    truncated: bool = False


class AskOut(BaseModel):
    """One answer, or one honest refusal to answer (E1/E2).

    **What ``answered: true`` claims, exactly.** Four deterministic things held
    (:func:`ask`): the model cited at least one note *this* retrieval returned;
    it did not name a note that does not exist while offering only one that
    does; every number in the answer text appears in the text that was actually
    sent, or in the question; and there is answer text.

    **What it does not claim.** It does not say the answer is true, and it does
    not say the cited note contains it. Nothing here can: a citation is
    resolvable when the *id* is real, and a model that invents content while
    naming a real id passes every check above. Live example — a question about
    which cloud hosts a Kubernetes cluster answered ``AWS``, citing a 28 100-
    character Kafka textbook containing no occurrence of AWS, cloud, Kubernetes
    or provider, on a graph that says elsewhere the cluster is on-prem k3s.
    Citation resolvability is not groundedness, and the only claim this
    envelope makes about groundedness is the narrow arithmetic one named above.
    Read :attr:`citations` — with :attr:`Citation.truncated` — rather than
    trusting :attr:`answered` alone.

    When :attr:`answered` is false, :attr:`answer` is ``None``: an answer the
    interface has just said it cannot stand behind is not a shorter answer, and
    putting the text on the screen beside the refusal would undo the refusal.

    Five lists say what this answer was built from, and each is a different
    fact. ``considered`` is what reached the model — nothing else, so it is
    empty on every path where no call was made, and ``used.calls`` is its
    corroboration. ``truncated_notes`` is what reached the model **in part**:
    the note was sent, and not all of it was, which is what makes a citation to
    it weaker than it looks. ``dropped`` is what the retrieval found and the
    context window could not carry at all. ``unresolved`` is what the model
    named that this retrieval did not return: the honest measure of how far it
    drifted, and on a weak model the field a reader learns the most from.
    ``unsupported_numbers`` is what the answer states in digits and the sent
    text does not contain.
    """

    question: str
    k: int
    answered: bool
    answer: str | None
    citations: list[Citation]
    considered: list[str]
    dropped: list[str]
    truncated_notes: list[str] = []
    unresolved: list[str]
    unsupported_numbers: list[str] = []
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

    **Four different partial reads, kept apart because they have four different
    causes.** ``truncated`` is the subgraph *walk* stopping at its cap.
    ``withheld`` is this module refusing to send a node the walk returned,
    because it is archived, proposed, or lives in the meta space
    (:data:`SENDABLE_STATES`). ``dropped`` is the context window refusing a note
    outright. ``truncated_notes`` is the context window taking a note in part —
    measured: a 28 100-character source narrowed to :data:`MIN_CONTEXT_CHARS`
    and reported ``truncated: false``, because the only ``truncated`` there was
    then belonged to the walk.
    """

    node_id: str
    depth: int
    summarized: bool
    summary: str | None
    citations: list[Citation]
    considered: list[str]
    dropped: list[str]
    truncated_notes: list[str] = []
    withheld: list[str] = []
    unresolved: list[str]
    unsupported_numbers: list[str] = []
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
    configuration change.

    So :attr:`reachable` is a *tri-state*, and ``None`` is **not established**
    rather than "not asked". Three ways to get it: nothing is configured to
    ask, the caller declined the probe, or the probe was asked and got no
    answer within :attr:`call_timeout`. That third one used to report ``false``,
    which collapsed a distinction :mod:`nodum.llm` makes on purpose — a
    :class:`~nodum.llm.ProviderTimeout` is a subclass of
    :class:`~nodum.llm.ProviderUnavailable`, but "a refused connection" is a
    server that is not running and "no answer yet" is very often a live server
    loading a model for the first time. Saying ``false`` about the second sends
    a human to fix an install that is working.

    :attr:`used` is the probe's own cost. It is one real model call — 34 tokens,
    measured — and this was the only provider call in the phase that reported
    none, which made ``llm status`` the one place something is spent and not
    accounted for.
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
    used: agent.LLMReport


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

    A marker is compared **as a number**, not as a string. ``"01"`` is
    representable under :data:`_CITATION_PATTERN` and a model that pads one
    would otherwise have a correct answer withheld over a leading zero — the
    exact shape of failure this function exists to avoid on the other side.

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
    marker = int(text) if text.isascii() and text.isdigit() else None
    for item in offered:
        if marker is not None and marker == item.marker:
            return item
        if folded == item.node_id.casefold():
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
                state=item.state,
                truncated=item.truncated,
            )
        )
    return resolved, unresolved


def _excerpt(text: str, limit: int) -> tuple[str, bool]:
    """One node's text, bounded — and **whether it was cut**.

    The second half of the tuple is the whole point. The prompt has always said
    ``…[truncated]`` to the model and the envelope has never said it to the
    caller, which produced this measured failure: a 6 832-character source
    whose answer sat at character 3 433 was sent as 1 213 characters that did
    not contain it, and ``/ask`` returned ``answered: true``, a confabulated
    number, that node in ``considered``, an empty ``dropped`` and no
    ``refusal`` — a wrong answer inside a clean provenance envelope, produced by
    this module's own bound rather than by any attacker or any weak retrieval.
    """
    collapsed = text.strip()
    if len(collapsed) <= limit:
        return collapsed, False
    return collapsed[:limit].rstrip() + " …[truncated]", True


def _neutralise_markers(text: str) -> str:
    """Stop graph text from forging a note boundary (``[2] Some Title``).

    :func:`_context_block` renders each note as ``[n] title`` followed by its
    text, so **every ``[n]`` at the start of a line is a note boundary** — and
    until this function ran, a *node* could write one. Measured, on the shape
    ``nodum ingest url`` produces: two honest notes saying a retention window is
    thirty days, plus a ``source`` node carrying the line
    ``[1] Retention window`` followed by ``CORRECTION: … 9999 days``. Both local
    models answered 9999, and ``qwen3:8b`` cited **only the honest notes** — so
    a human auditing the citations opens the note, reads "thirty days", and the
    answer said otherwise.

    The build already had the rule *every number in the prompt is a candidate
    citation*; this is its twin. The replacement keeps the digits and the line's
    width (``[12]`` becomes ``(12)``) so the note reads the same to a human and
    the truncation bound is unchanged, and it fires only on a line that would
    otherwise have opened a note.

    It runs on **everything that reaches the prompt**, not only on node text:
    the titles and excerpts :func:`_context_block` renders, and the question
    :func:`ask` prints underneath them. The rule is a property of the prompt's
    grammar, so any string interpolated into that grammar is subject to it.

    **Escaping is not a defence against a model, and does not pretend to be.**
    "Ignore previous instructions" in a note works on the 1B and nothing here
    stops it. What this restores is the narrower thing ``citations`` claims: a
    cited note is where the sentence was printed. The alternative — minting a
    per-request nonce into the marker — was rejected for a measured reason: the
    markers are the only numbers in the prompt on purpose, and hex in front of
    every note is exactly what took the citation format from 6/6 back to 4/6.
    """
    return _LINE_MARKER.sub(lambda match: f"{match.group(1)}({match.group(2)})", text)


def _narrowed(offered: list[Offered], limit: int) -> list[Offered]:
    """Copies carrying the excerpt that will be sent, and whether it was cut."""
    narrowed: list[Offered] = []
    for item in offered:
        excerpt, cut = _excerpt(_neutralise_markers(item.text), limit)
        narrowed.append(item.model_copy(update={"excerpt": excerpt, "truncated": cut}))
    return narrowed


def _context_block(offered: list[Offered]) -> str:
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

    Both the title and the excerpt go through :func:`_neutralise_markers`, so
    the markers this function writes are the only ones in the block — and
    :func:`ask` defuses the question for the same reason, because the template
    prints it below the block and a line ``[3] …`` there is one more boundary
    in the same prompt.
    """
    return "\n\n".join(
        f"[{item.marker}] {_neutralise_markers(item.title or '(untitled)')}\n"
        f"{item.excerpt or '(no text)'}"
        for item in offered
    )


def _numbers(text: str) -> set[str]:
    """Digit runs in ``text``, normalised so ``007`` and ``7`` are one number.

    Runs rather than substrings: ``2024`` does not contain the number ``24``,
    and a substring test would read a year as corroboration for a duration.
    """
    return {str(int(run)) for run in _NUMBER.findall(text)}


def _unsupported_numbers(answer: str, offered: list[Offered], *asked: str) -> list[str]:
    """Numbers the answer states that the text actually sent does not carry.

    The one groundedness check available here that is deterministic, free, and
    honest about its own reach. It compares the answer's digit runs against the
    **excerpts that were sent** plus their titles plus whatever the caller
    asked, and reports what is in neither.

    It checks **numbers and nothing else.** ``AWS`` in an answer whose context
    never mentions a cloud provider is exactly as ungrounded and is not caught
    here; catching it needs a judgement about language, which is a second model
    call and a different phase. What numbers buy is that most of what a personal
    knowledge graph is asked — a deadline, a retention window, a version, a
    count — turns on one, and a number the sent text does not contain was
    supplied by the model. Measured: a source saying an escalation deadline is
    *fourteen minutes*, sent as a 1 213-character prefix that did not reach that
    sentence, answered "…is 24 hours".

    The prompt's own markers are deliberately **not** in the supporting text —
    they are this module's numbers, not the graph's, and counting them would
    make every single-digit claim self-supporting.

    A false positive is possible and is the trade taken on purpose: a model that
    renders *fourteen* as ``14`` is refused with the number named in the
    refusal. That is :mod:`nodum.llm`'s own rule for its token estimate — an
    over-refusal is visible and itemised, an under-refusal is an answer nobody
    can tell from a good one.
    """
    supported = _numbers(" ".join(asked))
    for item in offered:
        supported |= _numbers(f"{item.title or ''} {item.excerpt}")
    return sorted(_numbers(answer) - supported, key=int)


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
        The notes that fit — each carrying the excerpt that was put in the
        message and whether it was cut — and the message, or ``([], None)``
        when not even one note at the narrowest excerpt fits, which means the
        template plus the question already fill the window.
    """
    window = active.context_tokens
    if window is None:
        return [], None
    ceiling = window - active.max_output_tokens
    items = list(offered)
    limit = MAX_CONTEXT_CHARS
    while items:
        narrowed = _narrowed(items, limit)
        message = agent.Message(
            role="user", content=template.format(context=_context_block(narrowed), **fields)
        )
        if active.estimate_prompt_tokens([message]) <= ceiling:
            return narrowed, message
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


def _charged_calls(active: agent.AgentRun) -> int:
    """Provider calls this run has been billed for.

    What ``considered`` is answerable against: the field says *what reached the
    model*, and the meter is the only thing that knows whether anything did. A
    refusal raised before the wire (a budget ceiling, a prompt that will not
    fit) leaves this unchanged; a call that reached the server and came back
    unusable (a filled context, an output ceiling) increments it, because the
    model really did read the prompt.
    """
    return active.budget.calls


def _ungrounded(
    citations: list[Citation], unresolved: list[str], unsupported: list[str], *, noun: str
) -> str | None:
    """Why a resolving citation is still not enough here, or ``None`` (E2).

    Two rules, both deterministic, both cheap, and both drawn from a live
    failure rather than from reasoning about one.

    **A citation that resolves beside one that does not, on its own, is not an
    answer.** Live: ``nodum ask "Which cloud provider hosts the production
    Kubernetes cluster?"`` came back ``answered: true`` and ``AWS``, citing a
    28 100-character Kafka textbook with no occurrence of AWS, cloud,
    Kubernetes, k3s, Azure, GCP or provider in it. The endpoint had the signal
    and threw it away: the model *also* cited marker ``2`` when exactly one note
    was offered, which is proof it was not reading the context, and the response
    put that in ``unresolved`` while standing behind the other citation.

    **A number the sent text does not contain is not an answer.** See
    :func:`_unsupported_numbers`.

    The first rule is deliberately not widened to "any unresolved citation
    voids the answer". Two surviving citations beside one spurious marker is a
    different picture — a model that placed two real notes has demonstrably
    read them — and voiding it would refuse answers this graph really contains.
    """
    if unresolved and len(citations) == 1:
        return _no_answer(
            f"the model cited {', '.join(unresolved)}, which this request never offered, beside "
            f"the one note that does resolve. A model naming a note that does not exist was not "
            f"reading the notes it was given, so the surviving citation is a coincidence rather "
            f"than corroboration and the {noun} is not returned"
        )
    if unsupported:
        return _no_answer(
            f"the {noun} states {', '.join(unsupported)}, and none of that appears in the text "
            f"this request actually sent. Numbers are the one claim checked here, and they are "
            f"checked against the excerpts as sent — a note cut short is a note whose rest the "
            f"model never saw"
        )
    return None


def _offered_hit(
    marker: int, hit: SearchHit, *, principal: Principal, path: str | Path | None
) -> Offered:
    """One search hit as a note to offer: the node's own text, and its state.

    A snippet is 200 characters with match markers in it — enough to *rank* a
    node and not enough to answer from, so the answer is built from the node
    rather than from the index's summary of it. The extra read per hit is one
    bounded, grant-scoped ``get_node``: the hit came from this principal's own
    search, so it is visible, and a node deleted in the moment between the two
    reads falls back to the snippet rather than failing the request.

    That read is also where ``state`` comes from. Every hit is ``active`` —
    :func:`nodum.search.search` filters on it — but a field that says so is
    what lets a caller read one envelope for both endpoints, and ``None`` on
    the vanished-node path is the honest answer rather than a guess.
    """
    try:
        node: NodeOut | None = service.get_node(hit.node_id, principal=principal, path=path)
    except service.RecordNotFound:
        node = None
    return Offered(
        marker=marker,
        node_id=hit.node_id,
        title=hit.title,
        space_id=hit.space_id,
        state=node.state if node is not None else None,
        text=(node.content.strip() if node is not None else "") or hit.snippet,
    )


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

    **``answered: true`` is four deterministic checks and no more than that**
    (see :class:`AskOut` for what each one is worth):

    1. at least one citation resolves to a note this retrieval returned (E2);
    2. the model did not also name a note that does not exist while offering
       only one that does — a model that cites marker ``2`` when one note was
       sent demonstrably was not reading the context, and the one citation that
       happens to resolve is then not corroboration but a coincidence;
    3. every number in the answer text appears in the text that was actually
       sent, or in the question (:func:`_unsupported_numbers`);
    4. there is answer text.

    None of the four establishes that the answer is *true*, and the docstrings
    here say so rather than implying otherwise: a model that invents content
    while citing a real id passes all four, and no deterministic check
    available to this module catches that.

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
        _offered_hit(index, hit, principal=principal, path=path)
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

    # The question is the last thing in the template, *under* the notes, so a
    # line `[3] …` in it opens one more note than the retrieval offered. It goes
    # through the same defusing the excerpts do; `question` in the envelope
    # stays the human's own words.
    offered, message = _fit_prompt(
        active, ASK_TEMPLATE, retrieved, question=_neutralise_markers(question)
    )
    considered = [item.node_id for item in offered]
    truncated_notes = [item.node_id for item in offered if item.truncated]
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
    calls_before = _charged_calls(active)
    try:
        generation = active.chat([message], prompt_version=ASK_PROMPT_VERSION, schema=ASK_SCHEMA)
    except (agent.BudgetExhausted, agent.LLMError) as exc:
        # `considered` and `truncated_notes` are claims about what the model
        # saw. A ceiling that refused before the wire and a provider nothing
        # answered on both leave them empty — listing node ids beside
        # `used.calls: 0` said notes reached a model that was never called.
        reached = _charged_calls(active) > calls_before
        return AskOut(
            question=question,
            k=k,
            answered=False,
            answer=None,
            citations=[],
            considered=considered if reached else [],
            dropped=dropped,
            truncated_notes=truncated_notes if reached else [],
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
            truncated_notes=truncated_notes,
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
    unsupported = _unsupported_numbers(answer, offered, question)
    answered = bool(citations) and bool(answer)
    refusal = None
    if not citations:
        refusal = _no_answer(
            "the model cited nothing this search returned, so the answer is not returned. "
            "A citation that does not resolve to a node you can read is not a citation"
        )
    elif not answer:
        refusal = _no_answer("the model returned citations and no answer text")
    else:
        refusal = _ungrounded(citations, unresolved, unsupported, noun="answer")
        answered = refusal is None
    return AskOut(
        question=question,
        k=k,
        answered=answered,
        answer=answer if answered else None,
        citations=citations,
        considered=considered,
        dropped=dropped,
        truncated_notes=truncated_notes,
        unresolved=unresolved,
        unsupported_numbers=unsupported,
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

    **What may be sent is narrower than what may be read** (:data:`SENDABLE_
    STATES`). ``subgraph`` filters *edges* by state and never filters nodes at
    all, so the walk returns archived, proposed and meta-space nodes — and this
    endpoint used to put every one of them in front of the provider while
    ``/ask``, which searches ``state="active", include_meta=False``, could not
    reach any of them at any ``k``. Neither is a grant violation, since the
    caller is a human who may read all of it; what was wrong is that the two
    endpoints disagreed about what leaves the machine, and only one of them
    agreed with what a human means by archiving a note. They are named in
    ``withheld`` rather than silently absent.

    ``summarized`` follows :func:`ask`'s rules exactly, including the two under
    :func:`_ungrounded` — the material is by construction in the context here,
    which makes a number the context does not contain *more* clearly the
    model's own invention, not less.

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
    retrieved: list[Offered] = []
    withheld: list[str] = []
    for node in region.nodes:
        if node.state not in SENDABLE_STATES or node.space_id == service.META_SPACE_ID:
            withheld.append(node.id)
            continue
        retrieved.append(
            Offered(
                marker=len(retrieved) + 1,
                node_id=node.id,
                title=node.title,
                space_id=node.space_id,
                state=node.state,
                text=node.content,
            )
        )

    if not active.available:
        return SummaryOut(
            node_id=node_id,
            depth=depth,
            summarized=False,
            summary=None,
            citations=[],
            considered=[],
            dropped=dropped,
            withheld=withheld,
            unresolved=[],
            truncated=region.truncated,
            refusal=active.unavailable_reason,
            used=active.report(),
        )
    if not retrieved:
        return SummaryOut(
            node_id=node_id,
            depth=depth,
            summarized=False,
            summary=None,
            citations=[],
            considered=[],
            dropped=dropped,
            withheld=withheld,
            unresolved=[],
            truncated=region.truncated,
            refusal=(
                "every node in this region is archived, proposed, or lives in the meta space, so "
                "there was nothing this endpoint may send and no model call was made"
            ),
            used=active.report(),
        )

    offered, message = _fit_prompt(active, SUMMARIZE_TEMPLATE, retrieved)
    considered = [item.node_id for item in offered]
    truncated_notes = [item.node_id for item in offered if item.truncated]
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
            withheld=withheld,
            unresolved=[],
            truncated=region.truncated,
            refusal=(
                "not one of these notes fits this model's context window at its narrowest "
                "excerpt. Configure a model with a wider window"
            ),
            used=active.report(),
        )
    calls_before = _charged_calls(active)
    try:
        generation = active.chat(
            [message], prompt_version=SUMMARIZE_PROMPT_VERSION, schema=SUMMARIZE_SCHEMA
        )
    except (agent.BudgetExhausted, agent.LLMError) as exc:
        reached = _charged_calls(active) > calls_before
        return SummaryOut(
            node_id=node_id,
            depth=depth,
            summarized=False,
            summary=None,
            citations=[],
            considered=considered if reached else [],
            dropped=dropped,
            truncated_notes=truncated_notes if reached else [],
            withheld=withheld,
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
            truncated_notes=truncated_notes,
            withheld=withheld,
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
    unsupported = _unsupported_numbers(summary, offered)
    summarized = bool(citations) and bool(summary)
    refusal = None
    if not citations:
        refusal = _no_answer(
            "the model cited none of the notes it was given, so the summary is not returned"
        )
    elif not summary:
        refusal = _no_answer("the model returned citations and no summary text")
    else:
        refusal = _ungrounded(citations, unresolved, unsupported, noun="summary")
        summarized = refusal is None
    return SummaryOut(
        node_id=node_id,
        depth=depth,
        summarized=summarized,
        summary=summary if summarized else None,
        citations=citations,
        considered=considered,
        dropped=dropped,
        truncated_notes=truncated_notes,
        withheld=withheld,
        unresolved=unresolved,
        unsupported_numbers=unsupported,
        truncated=region.truncated,
        refusal=refusal,
        used=active.report(),
    )


def _rewrite_ceiling(active: agent.AgentRun) -> int:
    """Output tokens the rewrite reserves: the run's ceiling, raised if it fits.

    **It only ever raises.** The human's ``NODUM_LLM_MAX_OUTPUT_TOKENS`` is the
    knob and a per-call number below it would be the model-compatibility
    setting in disguise this call site already refused once. What this adds is
    a floor of :data:`REWRITE_OUTPUT_TOKENS`, because the shipped default of
    512 turns the feature **off out of the box** on a reasoning model: the
    thinking tokens come out of the same allowance, ``qwen3:8b`` therefore
    answers with an empty body at ``finish_reason: "length"``, B3 correctly
    charges it and discards it, and the human is told about a ceiling they
    never chose.

    The floor is capped at half the window, so raising it can never be what
    leaves a prompt no room — the rewrite's prompt is one template and one
    question, which is the reason this is safe here and would not be on
    ``/ask``.
    """
    window = active.context_tokens
    floor = REWRITE_OUTPUT_TOKENS if window is None else min(REWRITE_OUTPUT_TOKENS, window // 2)
    return max(active.max_output_tokens, floor)


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
        generation = active.chat(
            messages,
            prompt_version=REWRITE_PROMPT_VERSION,
            schema=REWRITE_SCHEMA,
            max_output_tokens=_rewrite_ceiling(active),
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
    something is spent unattributed. **And what it spends is reported**, in
    ``used``, for the same reason: 34 tokens a probe, measured, is not nothing,
    and this was the only provider call in the phase whose cost the caller
    could not see.

    **The probe waits exactly as long as the envelope says it will.** It used
    to hold its own 30-second constant, which ``NODUM_LLM_CALL_TIMEOUT`` did
    not reach — so a slow install printed ``did not answer within 30s`` three
    lines under ``"call_timeout": 600.0``, and raising the knob changed
    nothing. There is one per-call ceiling and it is the run's.

    Args:
        principal: Who asked. The probe is metered against their request budget.
        probe: Whether to make the call at all. ``False`` reports the
            configuration and leaves ``reachable`` unasked.

    Returns:
        The configuration, the probe's verdict, the ceilings a request would
        spend under, and what the probe itself cost.
    """
    active = agent.for_request(purpose="llm-status", principal=principal)
    status = ProviderStatus(
        configured=active.available,
        provider=active.provider_id,
        model=active.model_id,
        context_tokens=active.context_tokens,
        reachable=None,
        detail=active.unavailable_reason,
        probe_ms=None,
        budget_tokens=active.budget.tokens,
        budget_seconds=active.budget.seconds,
        max_output_tokens=active.max_output_tokens,
        call_timeout=active.call_timeout,
        used=active.report(),
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
    except agent.ProviderTimeout as exc:
        # Caught **above** `ProviderUnavailable`, which it subclasses. A refused
        # connection is a server that is not running; no answer inside the
        # ceiling is very often a live server loading a model for the first
        # time, and reporting that as `reachable: false` sends a human to fix an
        # install that works. Neither is established, so neither is claimed.
        status.detail = (
            f"{_refusal_for(exc)} — which is not the same as down: nothing was established "
            f"either way. A first call often loads the model; raise "
            f"{agent.ENV_CALL_TIMEOUT} and ask again"
        )
    except agent.ProviderUnavailable as exc:
        status.reachable = False
        status.detail = _refusal_for(exc)
    except (agent.PromptTooLong, agent.BudgetExhausted) as exc:
        # Neither reached the wire, so neither says anything about the server.
        # `reachable` stays None rather than becoming False: this is a local
        # configuration refusing before it knocked.
        status.detail = _refusal_for(exc)
    status.probe_ms = int((time.monotonic() - started) * 1000)
    status.used = active.report()
    return status
