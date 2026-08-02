"""The LLM provider seam (Phase 5b, design decision P1) — one call, nothing else.

Shaped deliberately like :mod:`nodum.embeddings`: a ``Protocol``, a
module-level cached resolution (:func:`get_provider` → a provider or ``None``),
an :func:`unavailable_reason`, and a :func:`set_provider` test seam. That
parallel is not aesthetic — it is the seam every adapter in this package
already knows how to be absent through (``projector status`` reports the
embedding provider's absence, ``consolidate`` degrades on it, ``search`` skips
a signal for it), and a second seam with a different shape would mean two ways
to be offline.

**One implementation covers the local and the remote half.** ``ollama`` serves
an OpenAI-compatible ``/v1/chat/completions`` that honours
``response_format: {type: json_schema}`` and returns
``usage: {prompt_tokens, completion_tokens, total_tokens}`` plus
``finish_reason`` — verified by driving it, not assumed (see the measurements
below). So :class:`OpenAICompatProvider` is both halves: the local default is
``http://localhost:11434/v1`` with no key, a remote provider is the same class
with a key and a different base URL. ollama's *native* ``/api/chat`` is richer
(it exposes ``prompt_eval_duration``, and its ``format`` field takes a bare
schema) and was rejected for exactly that reason: it is a second code path that
only the local half exercises, on a machine where the remote half cannot be
tested at all. One path that both halves share, with the *less* capable surface
as the contract, is the one where a bug found locally is a bug fixed remotely.

**What it refuses to abstract over**, each refusal a decision rather than an
omission (P1):

- **Streaming.** Nothing here consumes a token stream: the two consumers are a
  cycle job (nobody is watching) and an HTTP endpoint that must answer or fail.
- **Tool calling.** The internal agent already *has* the graph — it is a peer
  client with a grant set and every read it needs is a Python call to
  :mod:`nodum.service`. Letting the model choose reads would put a model inside
  the loop that decides what to read, which is where a token budget stops being
  bounded.
- **Embeddings.** :mod:`nodum.embeddings` has its own ``model_id`` contract and
  its own D6 lifecycle; a provider that did both would make a model change mean
  two different things.
- **Retries and backoff.** A caller that needs a retry has a budget to spend it
  against (B2), so the policy belongs where the budget is — :mod:`nodum.agent`.
- **Prompt templates.** Prompts are job-specific and versioned (A2); a provider
  that owned them would make a prompt change a provider change.
- **Sampling.** :data:`TEMPERATURE` is fixed at 0. Determinism at temperature 0
  was measured on *this* backend and is not a property of the interface, so
  nothing may depend on it — but the least-random setting is still the right one
  for text a human will review, and a knob nobody has a reason to turn is a knob
  that will be turned by accident.

**There is no module-level ``chat`` function on purpose.** P3 says every
provider call in the system goes through the one :mod:`nodum.agent` entry
point, so that accounting, budgets and the kill switch happen in one place. A
second door here would be a second place where a model call happens, with its
own accounting and its own way of being absent.

**The peer-client shape holds here too** (P2). This module opens no database
connection, reads no row, imports nothing from :mod:`nodum`, and binds no
principal. It converts messages to text. The job holds the principal, the job
holds the budget, the job makes the write — and ``tests/test_llm.py`` asserts
over the ASTs of :mod:`nodum.service`, :mod:`nodum.projectors`,
:mod:`nodum.store` and :mod:`nodum.migrations` that none of them can reach this
module, which is design Constraint 4 stated in the form it is actually violated
in: not "don't call an LLM in validation" but "validation cannot reach the
module that could".

Measured against ``llama3.2:1b`` (Q8_0) served by ollama on this machine —
re-verified in this wave, not carried from the design note:

- **Silent context truncation.** 16 000 characters report 2 932 prompt tokens;
  64 000 report **4 096**, and 70 000 report **4 096** as well, with
  ``finish_reason: "stop"`` on both. The context window filled, everything past
  it was dropped, and *nothing in the response says so* — ``finish_reason``
  flags a truncated **output**, never a truncated **input**. The only legible
  signal is in ``usage``, and it arrives after the call has been paid for. That
  is why :meth:`OpenAICompatProvider.chat` refuses an over-long prompt
  **before** sending it (:class:`PromptTooLong`).
- **The window that binds is the *server's*, not the model's**, and that is
  what makes the refusal's own number worth being careful about. ollama serves
  every model at ``num_ctx`` — 4 096 unless ``OLLAMA_CONTEXT_LENGTH`` says
  otherwise — while ``llama3.2:1b`` really has a 128 k window, so "raise
  :data:`ENV_CONTEXT_TOKENS` for a model that has the room" produces the hole
  rather than avoiding it. Measured at ``NODUM_LLM_CONTEXT_TOKENS=32768``
  against that server: a 30 000-character prompt is **not** refused,
  ``prompt_tokens`` comes back 4 096, ``finish_reason`` is ``"stop"``, and a
  whole answer is returned from a prefix.
- **So there are two after-the-fact signals, and they see different failures.**
  :attr:`Completion.context_filled` compares the report against the *configured*
  window, which catches a server whose window really is that one and is
  structurally blind to the case above. :attr:`Completion.prompt_truncated`
  compares it against the prompt's own bytes — the server read fewer tokens than
  this many bytes can possibly cost — which is the one signal that does not
  depend on the operator having configured the right number.
- **A token ceiling gives unparseable JSON, not a short object.** Under a JSON
  schema with ``max_tokens=8``: ``finish_reason: "length"`` and the body
  ``'{\\n  "title": "Kafka'`` — cut mid-string. So a ``length`` finish is *no
  result*, not a partial one.
- **A JSON schema fixes the envelope and nothing else.** Asked a question its
  context could not answer, under a schema, the model returned
  ``{"answer": "n0", "cited_ids": ["n0"], "answered": false}`` — schema-valid,
  and an "answer" that is a context label. **Schema validity is never truth.**
  Validating what the model said against what was actually retrieved is the
  caller's job and this module does none of it.
- **Failure is fast and legible.** Nothing listening: ``URLError: Connection
  refused`` in 0.0003 s. An unknown model on a live server: HTTP 404,
  ``{"error":{"message":"model 'does-not-exist:1b' not found"…}}`` in 0.0013 s.

Configuration is environment-only (D10's "no key = smart features off",
generalised to "no provider = smart features off"):

``NODUM_LLM_MODEL``
    The model name. **Unset means no provider**, and therefore no smart
    features anywhere. There is no default, because a guessed model name is a
    404 on the first call rather than an honest absence at resolution time.
``NODUM_LLM_BASE_URL``
    OpenAI-compatible base URL; defaults to :data:`DEFAULT_BASE_URL` unless
    :func:`profile_for` recognises the model id as one a shipped profile's
    endpoint serves. Setting it explicitly takes the *whole* decision: a profile
    then applies only where it is that same host, so no model name can move a
    call off the endpoint the operator named — and where that host *is* the
    profiled one, the profile's window and modes still apply, because the
    profile is a fact about the endpoint rather than a consolation for not
    having named it.

    **It must be a URL urllib can POST to, and a spelling that is not is no
    provider with a reason** (:func:`base_url_problem`) — the same posture as an
    unparseable ``NODUM_LLM_CONTEXT_TOKENS``. A scheme-less
    ``api.deepseek.com/v1`` is not repaired into one, because choosing ``http``
    or ``https`` on the operator's behalf decides whether ``NODUM_LLM_API_KEY``
    crosses the network in clear text.
``NODUM_LLM_API_KEY``
    Bearer token. Optional — the local default needs none.

    **It is sent only to an endpoint somebody named**: ``NODUM_LLM_BASE_URL``,
    or a model id that is exactly one a shipped profile serves. A model id
    nobody profiled falls back to :data:`DEFAULT_BASE_URL`, which is a host
    *this module* chose — so the key is dropped at resolution time rather than
    posted to it, and :func:`key_withheld_reason` says so in
    ``nodum llm status``. A local gateway that requires a key keeps it by naming
    itself in ``NODUM_LLM_BASE_URL``.
``NODUM_LLM_THINKING``
    The reasoning level, one of :data:`THINKING_LEVELS`, defaulting to
    :data:`DEFAULT_THINKING`. A value outside the set is **no provider with a
    reason** rather than a fallback: a level is a name the API validates, not a
    number whose worst case is less work. Whether it reaches the endpoint at all
    is a second fact — :attr:`OpenAICompatProvider.thinking_applied`, reported
    beside it, because ollama accepts only ``none``. **Nothing may size an
    output ceiling from it** (see :data:`DEFAULT_THINKING`).
``NODUM_LLM_CONTEXT_TOKENS``
    **The window the endpoint will actually serve**, defaulting to
    :data:`DEFAULT_CONTEXT_TOKENS`. This is not the model card's number: with
    ollama the binding limit is the server's ``num_ctx``
    (``OLLAMA_CONTEXT_LENGTH``, 4 096 unless raised), applied to every model it
    serves — ``llama3.2:1b`` has a 128 k window and is served 4 096 of it.
    Setting this above the *serving* window re-opens the silent-truncation hole,
    since the refusal is computed against this number: the prompt passes the
    check, the server drops whatever did not fit, and ``prompt_tokens`` comes
    back **below** the configured ceiling, where
    :attr:`Completion.context_filled` cannot see it (measured — see above).
    :attr:`Completion.prompt_truncated` is what catches that case. Raise this
    only together with the serving window.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from pydantic import BaseModel

#: The one thing in this module that is neither a refusal nor a number a caller
#: reads back. A provider whose own ``usage`` block contradicts itself is not a
#: failure — nodum's arithmetic is authoritative and the call is still billed
#: honestly — but silence is how a wire that has started reporting nonsense goes
#: unnoticed for a release. See :meth:`OpenAICompatProvider._completion`.
_log = logging.getLogger(__name__)

#: Local default: what ``ollama serve`` exposes. Both halves of the provider
#: talk this surface — see the module docstring for why there is only one class.
DEFAULT_BASE_URL = "http://localhost:11434/v1"

#: The context window assumed when :data:`ENV_CONTEXT_TOKENS` is unset.
#: 4 096 is the measured window of the local default model, and the number the
#: silent-truncation refusal is computed against.
DEFAULT_CONTEXT_TOKENS = 4096

#: Sampling is not configurable (see the module docstring). 0 is the
#: least-random setting; nothing may *depend* on the determinism it produced on
#: the local backend, because a remote provider makes no such promise.
TEMPERATURE = 0.0

#: ``finish_reason`` when the output hit its ceiling. Load-bearing: the body at
#: that point is unparseable, not short — measured.
FINISH_LENGTH = "length"

#: Fixed tokens a chat template spends before any message content. Measured at
#: 25 for one empty user message on the local default, 35 for four; 32 plus
#: :data:`MESSAGE_OVERHEAD_TOKENS` each is comfortably above both, and the
#: estimate is deliberately an over-count throughout (see :func:`estimate_tokens`).
TEMPLATE_OVERHEAD_TOKENS = 32

#: Tokens charged per message on top of its content, for the role markers a
#: chat template wraps it in.
MESSAGE_OVERHEAD_TOKENS = 16

#: The loosest bytes-per-token ratio a prompt may have before
#: :attr:`Completion.prompt_truncated` calls the server's report impossible.
#:
#: :func:`estimate_tokens` is an *upper* bound, so "``prompt_tokens`` came back
#: below the estimate" is true of nearly every honest call and detects nothing.
#: The checkable statement is the other bound: *this many bytes cannot cost
#: fewer than this many tokens*. How few depends entirely on the script, and the
#: measured means on the local default model are 1.18 (32-hex ids) to **4.55**
#: (Arabic), with English at 4.49 — the ten samples in
#: ``tests/test_llm.py::MEASURED_TOKEN_COSTS``, which is what pins this constant
#: from below. 6 leaves 32 % headroom over the loosest of them.
#:
#: What that buys and what it does not: at 6 the check fires when the server
#: dropped roughly a quarter or more of an English prompt, and a *narrower*
#: truncation is invisible — there is no per-call signal for it, because the
#: estimate cannot tell "the tokeniser was efficient" from "the server read
#: less". The error is one-sided by choice, the same way the estimate itself is:
#: a false alarm is a visible, itemised refusal, and a miss is an answer from a
#: prefix that nobody can tell from a good one.
MAX_BYTES_PER_TOKEN = 6

#: Structured output enforced by the server's constrained decoding: a string the
#: schema forbids is *unrepresentable*, not merely discouraged. What
#: :mod:`nodum.answers` relies on when it puts a ``pattern`` on a citation.
STRUCTURED_JSON_SCHEMA = "json_schema"

#: Structured output as "this will be a JSON object", with the schema demoted to
#: a sentence in the prompt. The envelope is still enforced — the body parses —
#: but every *constraint inside* the schema is now advice the model may ignore.
#: **This is a real reduction in what a caller may assume**, which is why it is
#: named on the completion, on the run and in ``nodum llm status`` rather than
#: happening quietly.
STRUCTURED_JSON_OBJECT = "json_object"

#: No ``response_format`` at all — what a call with no schema sends.
STRUCTURED_NONE = "none"

#: The reasoning level that turns thinking off. Distinguished from the others
#: because it is the only one every endpoint measured accepts (ollama 400s on
#: every graded level, including for a thinking-capable model) and the only one
#: whose cost is predictable (measured 0, on nine independent calls).
THINKING_NONE = "none"

#: The reasoning levels nodum accepts, weakest first. A value outside this set
#: is refused at resolution time with a message naming the set — never passed
#: through to the provider.
THINKING_LEVELS = (THINKING_NONE, "low", "medium", "high")

#: The shipped reasoning level.
#:
#: **The level names are not ordered by what they cost, and ``high`` is the
#: cheapest of the graded three.** Measured on ``deepseek-v4-flash`` over two
#: five-note synthesis fixtures, two samples each: ``low`` spent 743/905 and
#: 2 177/797 reasoning tokens where ``high`` spent 349/101 and 60/110 — eight
#: points, both fixtures, the same direction. On a one-word prompt the same gap
#: appears (``low`` 639, ``high`` 47). ``high`` appears to reason *efficiently*
#: rather than *more*, and it is also the fastest of the three (5.4 s against
#: ``low``'s 26.9 s on the same prompt).
#:
#: So there is no axis on which a lower label wins, and the shipped level is the
#: top of the accepted range. The corollary matters more than the default:
#: **nothing may size an output ceiling from the configured level**, because the
#: label does not bound the appetite — ``low`` reached 2 177 thinking tokens on a
#: prompt ``high`` answered in 60. Only :data:`THINKING_NONE` is predictable,
#: measured at exactly 0 on every call.
#:
#: **The field is always sent when the endpoint takes it**, because leaving it
#: unset is not neutral: unset measured 1 492 reasoning tokens on a fixture where
#: ``none`` measured 0.
#:
#: The quality evidence behind preferring a graded level at all is **thin and
#: recorded as thin**: of two hard fixtures only one discriminated, and on that
#: one ``high`` named the unifying idea ("Designing for Human Fallibility") where
#: ``none`` named a subset of it. The other fixture was answered well at every
#: level including ``none``.
DEFAULT_THINKING = "high"

#: The largest share of the context window the output reservation may take.
#:
#: The window holds the prompt *and* the answer on a server that shares one KV
#: cache for both — ollama, where ``num_ctx`` binds — so the reservation is
#: load-bearing there and a flat subtraction was right for a 4 096-token window.
#: It stops being right the moment the output ceiling is sized for a reasoning
#: model: 4 096 reserved out of 4 096 leaves the prompt nothing at all, and the
#: same 4 096 out of a 1 000 000-token window is a rounding error pretending to
#: be a guard.
#:
#: So the reservation is a *share*: at most half the window, whatever the
#: configured ceiling says, and the clamped number is what is sent as
#: ``max_tokens`` — clamping the reservation without clamping the request would
#: let the server generate into space the prompt was allowed to occupy, which is
#: the truncation this whole module exists to prevent.
#:
#: Half is not arbitrary: ``answers._rewrite_ceiling`` already caps its own
#: per-call ceiling at ``window // 2`` for the same reason, and one rule is
#: better than two.
OUTPUT_RESERVATION_FRACTION = 0.5

#: The system message that carries a schema into the prompt when
#: :data:`STRUCTURED_JSON_OBJECT` is in force.
#:
#: **The word "json" in it is load-bearing, not phrasing.** Measured: the
#: endpoint refuses ``response_format: {"type": "json_object"}`` with
#: ``HTTP 400 "Prompt must contain the word 'json' in some form to use
#: 'response_format' of type 'json_object'."`` unless the prompt says it. A
#: rewrite that drops the word turns every structured call into a 400.
JSON_OBJECT_INSTRUCTION = (
    "Return one JSON object and nothing else — no prose, no code fence.\n"
    "The object must conform to this JSON schema:\n{schema}"
)

#: What an endpoint is believed to accept in ``reasoning_effort``, strongest
#: first. Three states rather than a boolean, because there are three endpoints
#: in the world and two of them were measured:
#:
#: ``graded``
#:     Any level. ``deepseek-v4-flash``.
#: ``off-only``
#:     ``none`` and nothing else. ollama, which answers 400 to ``low``,
#:     ``medium`` and ``high`` — including on ``qwen3:8b``, which does think.
#: ``absent``
#:     The field is not understood at all. Not measured on either server, but
#:     reachable: an OpenAI-compatible endpoint that rejects unknown fields
#:     refuses ``reasoning_effort: "none"`` along with the rest, and a ladder
#:     that stopped at ``off-only`` would send the field forever and never
#:     recover. Found by making the status probe drive a real 400 — the two-state
#:     version left that server permanently unusable.
_WIRE_GRADED = "graded"
_WIRE_OFF_ONLY = "off-only"
_WIRE_ABSENT = "absent"

#: How many beliefs :meth:`OpenAICompatProvider._negotiate` may drop in one
#: call: the structured form once, and the reasoning field twice down the ladder
#: above. Bounded so a pathological server cannot turn one call into a loop.
_MAX_NEGOTIATIONS = 3

#: The floor on what is left of a per-call timeout for a re-send. A negotiation
#: that arrived with the ceiling already spent still has to make its one request
#: rather than passing 0 to ``urlopen``.
#:
#: 0 does **not** block forever — that is ``None``. Measured against a live
#: local server: a POST at ``timeout=0`` raises ``URLError(BlockingIOError(115,
#: 'Operation now in progress'))`` in under 10 ms, because the socket is put in
#: non-blocking mode and the connect returns ``EINPROGRESS``; the same POST at
#: ``0.001`` raises ``TimeoutError('timed out')``. That is worse than a timeout rather
#: than better: :meth:`OpenAICompatProvider._post` reads a non-``TimeoutError``
#: ``URLError`` as :class:`ProviderUnavailable` "is not reachable", so a
#: negotiation that ran out of clock would report a healthy server as down
#: instead of reporting the deadline that actually bit. A millisecond keeps the
#: re-send on the blocking path, where an expired ceiling surfaces as
#: :class:`ProviderTimeout` and names the ceiling.
_MIN_TIMEOUT = 0.001

#: Substrings that identify a 400 as "this endpoint does not serve that
#: ``response_format``". Measured on ``deepseek-v4-flash``: ``This
#: response_format type is unavailable now``.
_STRUCTURED_REJECTIONS = ("response_format",)

#: What separates the field's *name* from a path **into** it. A 400 reading
#: ``Invalid schema for response_format.schema.properties[0]`` names the same
#: substring and means the opposite thing: the server parsed ``response_format``
#: and is validating what is inside it, which is proof that it serves the field
#: and that the fault is nodum's own schema.
#:
#: Downgrading on that would trade a loud, fixable "your schema is wrong" for an
#: envelope quietly weakened for the life of the process — the exact harm
#: ``test_a_non_capability_400_is_not_negotiated`` exists to prevent, reached
#: through a message that happens to contain the marker. It is the one
#: *sharpening* of a matcher this module keeps deliberately blunt.
#:
#: **It is a real behaviour change, not a preservation.** Driven on both
#: revisions: ``response_format.type is unavailable`` and
#: ``response_format[type] not supported`` were negotiated before this guard and
#: are not now. No endpoint anyone has measured says either — the sentence
#: DeepSeek really returns carries no path — but an OpenAI-compatible server
#: wording its genuine capability rejection with a dotted or bracketed path will
#: now never downgrade, and structured output fails against it permanently
#: instead of once. The separator is only a proxy for "the server dereferenced
#: the field"; the honest signal is the reason words (``Invalid schema for``
#: against ``is unavailable``), not the punctuation. Kept because the failure it
#: prevents is by far the commoner one, and re-matching on reason words is
#: follow-up work rather than something to do on this branch.
_FIELD_PATH_SEPARATORS = (".", "[")

#: Substrings that identify a 400 as "this endpoint does not take a graded
#: reasoning level". Measured on ollama: ``"llama3.2:1b" does not support
#: thinking`` for a model without thinking, and ``think value "low" is not
#: supported for this model`` for ``qwen3:8b``, which has it — two different
#: sentences from one server, which is why this is a list rather than a string.
#:
#: Matched as plain substrings, deliberately unlike :data:`_STRUCTURED_REJECTIONS`:
#: two of the three are *sentences a server says* rather than field names, so
#: the path guard in :func:`_names_field` would stop ``"llama3.2:1b" does not
#: support thinking.`` matching over a full stop. ``reasoning_effort`` takes a
#: bare string, so there is no path into it for a server to name.
_THINKING_REJECTIONS = (
    "reasoning_effort",
    "does not support thinking",
    "think value",
)

#: Environment variables. Named as constants so a test asserts on the same
#: string the code reads.
ENV_MODEL = "NODUM_LLM_MODEL"
ENV_BASE_URL = "NODUM_LLM_BASE_URL"
ENV_API_KEY = "NODUM_LLM_API_KEY"
ENV_CONTEXT_TOKENS = "NODUM_LLM_CONTEXT_TOKENS"
ENV_THINKING = "NODUM_LLM_THINKING"


class LLMError(RuntimeError):
    """Anything that stopped a provider call from producing a usable answer."""


class PromptTooLong(LLMError):
    """The prompt would not fit, refused **before** the call was made.

    This is the whole point of the interface. The server does not tell you it
    dropped your input: a 64 000-character prompt and a 70 000-character one
    both report 4 096 prompt tokens, both with ``finish_reason: "stop"``, and
    the only difference is which half of the context the model never saw. Once
    the call is made, the truncation is legible in ``usage`` — and paid for.
    So the count happens here, on an estimate that never under-counts, and an
    over-long prompt is a refusal rather than a worse answer.
    """


class ContextOverflow(LLMError):
    """The server did not read the whole prompt — detected from ``usage`` after.

    The second line of defence, and it has two halves because the configured
    window can be wrong in either direction:
    :attr:`Completion.context_filled` (the report reached the configured
    ceiling) and :attr:`Completion.prompt_truncated` (the report is below what
    the prompt's bytes can possibly cost, which is the case a window configured
    *above* the serving one produces). Either way the completion is charged
    against the budget because it was really spent, and the body is discarded
    because part of the prompt was never read.
    """

    def __init__(self, message: str, completion: Completion) -> None:
        super().__init__(message)
        #: The call that was paid for. The caller bills it and drops the text.
        self.completion = completion


class OutputTruncated(LLMError):
    """The output hit its ceiling: ``finish_reason == "length"`` (B3).

    Measured: under a JSON schema, that body is cut mid-string and does not
    parse. It is a *failed call*, not a short result — the output is discarded
    unparsed and it still counts against the budget.
    """

    def __init__(self, message: str, completion: Completion) -> None:
        super().__init__(message)
        #: The call that was paid for. The caller bills it and drops the text.
        self.completion = completion


class ProviderUnavailable(LLMError):
    """The provider could not be reached, or answered something unusable.

    :attr:`status` is the HTTP status when there was one, and ``None`` for a
    failure that never got a response at all. It exists so capability
    negotiation can read a **400** — the client-error voice, which is where a
    server says "I do not serve that field" — without reading a 500 or a dropped
    connection the same way.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        #: The HTTP status, or ``None`` when no response arrived.
        self.status = status


class ProviderTimeout(ProviderUnavailable):
    """The per-call wall-clock ceiling bit (B2).

    Distinct from a dead server because they call for different responses: a
    timeout is a prompt that was too expensive for the ceiling it was given,
    and a refused connection is a configuration that is not running.
    """


class Message(BaseModel):
    """One chat message. ``role`` is ``system``, ``user`` or ``assistant``."""

    role: str
    content: str


class Completion(BaseModel):
    """One provider answer, with every field the wire already returns.

    Nothing here is computed by nodum except :attr:`latency_ms` (measured
    around the call), :attr:`context_tokens` (the configured window) and
    :attr:`prompt_estimate` (the pre-send over-count the refusal was computed
    against). The last two are carried so that :attr:`context_filled` and
    :attr:`prompt_truncated` are answerable without reaching back to the
    provider — and the estimate in particular is recorded **by the provider**,
    because the provider is the only thing that has it: it computes that number
    one line before sending, and a caller recomputing it afterwards would be a
    second estimator free to disagree with the one that actually decided.

    **A schema-valid body is not a true one.** The measurement that shaped this
    phase is a model returning ``{"answer": "n0", "cited_ids": ["n0"],
    "answered": false}`` for a question its context could not answer — well
    formed, and worthless. Validating the content against what was actually
    retrieved belongs to the caller, and this model does none of it.
    """

    text: str
    prompt_tokens: int
    output_tokens: int
    finish_reason: str
    model_id: str
    provider_id: str
    context_tokens: int
    latency_ms: int
    #: The pre-send over-count of the prompt, or ``0`` for a completion nobody
    #: measured (a replayed row, a hand-built fake). ``0`` means *unknown*, never
    #: *a prompt of nothing*, so :attr:`prompt_truncated` stays ``False`` on it
    #: rather than calling every such completion truncated.
    prompt_estimate: int = 0
    #: The same over-count with the chat-template overheads left out — the bytes
    #: really sent, and nothing else. :attr:`prompt_truncated` is computed
    #: against this rather than :attr:`prompt_estimate` because the overheads are
    #: a guess at somebody else's template: on a 33-byte prompt they were 60 % of
    #: the estimate and the check fired on a completion the server read whole.
    #: ``0`` falls back to :attr:`prompt_estimate`, so a completion built by hand
    #: behaves exactly as it did before this field existed.
    prompt_content_estimate: int = 0
    #: Tokens the model spent thinking, from
    #: ``usage.completion_tokens_details.reasoning_tokens``.
    #:
    #: **A share of :attr:`output_tokens`, never an addition to it** — measured
    #: on ``deepseek-v4-flash``, ``total_tokens`` is ``prompt + completion`` on
    #: every call and ``reasoning_tokens`` never exceeds ``completion_tokens``.
    #: So it is carried beside the cost rather than added to it: adding it would
    #: double-charge the one number budgets are denominated in.
    #:
    #: It is carried at all because it is where the output ceiling actually
    #: goes. A call that spent 1 420 of 1 520 output tokens thinking and 100
    #: writing is indistinguishable, in ``completion_tokens`` alone, from one
    #: that wrote 1 520 tokens of answer — and the first is one bad sample away
    #: from a ``length`` finish, which on this interface is *no result*.
    #: ``0`` means either "the model did not think" or "the wire did not say";
    #: the two are not distinguishable and nothing here pretends otherwise.
    reasoning_tokens: int = 0
    #: Prompt tokens served from the provider's prefix cache
    #: (``usage.prompt_cache_hit_tokens``). Priced ~50x cheaper than a miss on
    #: DeepSeek, so a cost report without it is a cost report that can be wrong
    #: by two orders of magnitude on a long stable prefix. ``0`` on a provider
    #: that reports no cache at all — ollama returns neither counter.
    cache_hit_tokens: int = 0
    #: Prompt tokens the cache did not serve (``usage.prompt_cache_miss_tokens``).
    #: Measured: hit + miss == ``prompt_tokens`` on every DeepSeek call.
    cache_miss_tokens: int = 0
    #: Which structured-output mode produced this body — one of
    #: :data:`STRUCTURED_JSON_SCHEMA`, :data:`STRUCTURED_JSON_OBJECT` or
    #: :data:`STRUCTURED_NONE`. Recorded on the completion because it is the
    #: only place a caller can see *what its schema was actually worth on this
    #: call*: under ``json_schema`` the server's constrained decoding makes a
    #: bad string unrepresentable, and under ``json_object`` the schema is
    #: nothing but a sentence in the prompt.
    structured_mode: str = STRUCTURED_NONE
    #: The ``max_tokens`` actually sent, which is not always what the caller
    #: asked for: the reservation is capped at a share of the window
    #: (:data:`OUTPUT_RESERVATION_FRACTION`). Recorded so a ``length`` finish
    #: can name the number that really bit.
    output_ceiling: int = 0
    #: The ``reasoning_effort`` actually sent, or ``None`` when the field was
    #: withheld because this endpoint does not take graded levels. ``None`` is
    #: **not** "no thinking" — it is the model's own default, which measured as
    #: the *most* expensive setting of all on ``deepseek-v4-flash``.
    thinking: str | None = None

    @property
    def total_tokens(self) -> int:
        """What the call cost, in the unit budgets are denominated in (B2).

        :attr:`reasoning_tokens` is deliberately **not** added: it is already
        inside :attr:`output_tokens`, and a meter that added it would report a
        reasoning model as costing up to twice what the bill says.
        """
        return self.prompt_tokens + self.output_tokens

    @property
    def content_tokens(self) -> int:
        """Output tokens that became text rather than thinking.

        The number the output ceiling has to leave room for. It is not on the
        wire — the wire reports the total and the thinking — and it is the
        difference between "the ceiling is generous" and "the ceiling is one
        excursion away from returning nothing at all".
        """
        return max(0, self.output_tokens - self.reasoning_tokens)

    @property
    def output_truncated(self) -> bool:
        """Did the output ceiling bite? Then this is a failed call (B3)."""
        return self.finish_reason == FINISH_LENGTH

    @property
    def context_filled(self) -> bool:
        """Did the report reach the **configured** window?

        ``finish_reason`` never says a prompt was truncated (measured:
        ``"stop"`` on a prompt cut from 70 000 characters to 4 096 tokens), so
        every signal there is lives in ``usage`` and arrives after the call is
        paid for.

        **What this one cannot see**, stated because both this docstring and
        ``AGENTS.md`` used to claim the opposite: it compares the server's report
        against :attr:`context_tokens`, which is the number *the operator
        configured*. Configure 32 768 against a server serving 4 096 — the
        ordinary ollama case, where ``num_ctx`` binds and the model card does not
        — and a truncated prompt comes back at 4 096, which is nowhere near the
        ceiling, so this is ``False`` on exactly the misconfiguration it was
        described as defending against. :attr:`prompt_truncated` is the check
        that sees that case; this one catches the server whose window really is
        the configured one.
        """
        return self.prompt_tokens >= self.context_tokens

    @property
    def prompt_truncated(self) -> bool:
        """Did the server read fewer tokens than the prompt can possibly cost?

        The signal that does not depend on the configured window being right:
        :attr:`prompt_estimate` bounds the prompt's bytes, and no byte-level BPE
        prompt averages more than :data:`MAX_BYTES_PER_TOKEN` bytes per token
        (measured ceiling 4.55, on Arabic). A report below that floor is a
        server that dropped part of the input — the truncation nothing in the
        response body admits to.

        It is deliberately one-sided and deliberately not sharp: see
        :data:`MAX_BYTES_PER_TOKEN` for what it catches (a truncation that lost
        about a quarter of an English prompt or more) and what it cannot. A
        completion carrying no estimate answers ``False``, because ``0`` there
        means nobody measured.
        """
        # The content estimate, not the refusal's estimate: the difference is the
        # chat-template overhead, which is nodum's guess at somebody else's
        # wrapping rather than bytes anybody sent. See
        # :func:`estimate_content_tokens` for the live false positive that
        # forced the distinction.
        estimate = self.prompt_content_estimate or self.prompt_estimate
        if estimate <= 0:
            return False
        return self.prompt_tokens * MAX_BYTES_PER_TOKEN < estimate


class LLMProvider(Protocol):
    """Anything that turns messages into one completion (design P1)."""

    @property
    def provider_id(self) -> str:
        """Stable identifier for *where* the text came from (A1 provenance)."""
        ...

    @property
    def model_id(self) -> str:
        """Stable identifier for *what* produced the text (A1 provenance)."""
        ...

    @property
    def context_tokens(self) -> int:
        """The window a prompt is refused against, output reservation included."""
        ...

    @property
    def structured_mode(self) -> str:
        """Which ``response_format`` a schema is currently sent under.

        On the interface because it changes **what a caller may assume about the
        body**, and a caller that cannot read it cannot know whether its schema
        is a constraint or a suggestion.
        """
        ...

    @property
    def thinking(self) -> str:
        """The configured reasoning level (one of :data:`THINKING_LEVELS`)."""
        ...

    @property
    def thinking_applied(self) -> bool:
        """Whether that level actually reaches the endpoint."""
        ...

    def estimate_prompt_tokens(
        self, messages: Sequence[Message], *, schema: dict[str, Any] | None = None
    ) -> int:
        """An over-count of what these messages will cost as a prompt."""
        ...

    def output_reservation(self, max_output_tokens: int) -> int:
        """What this call will really reserve, and really send as ``max_tokens``."""
        ...

    def chat(
        self,
        messages: Sequence[Message],
        *,
        schema: dict[str, Any] | None = None,
        max_output_tokens: int,
        timeout: float,
        thinking: str | None = None,
    ) -> Completion:
        """Send one prompt and return one completion, or raise :class:`LLMError`."""
        ...


def estimate_tokens(text: str) -> int:
    """An upper bound on the tokens ``text`` costs: its UTF-8 byte count.

    A byte-level BPE tokeniser — which is what every model on this surface uses
    — has tokens that decode to **at least one byte**, so the byte count can
    never be below the token count. That makes this a *bound* rather than a
    heuristic, which is the property the refusal needs: an estimate that
    sometimes under-counts is an estimate that sometimes ships the silent
    truncation it exists to prevent.

    It is deliberately loose, and how loose depends entirely on the script.
    Measured marginal bytes per token on the local default model: English prose
    4.49, Arabic 4.55, Cyrillic 4.16, CJK 4.05, JSON 2.92, accented Latin 1.94,
    emoji 1.33, and **32-hex ids 1.18** — which is the case this system
    actually generates, since every prompt it builds names nodes by id. A
    character-based estimate is not merely loose but *wrong* here: ``chars / 4``
    under-counts emoji by twelve times and a run of accented Latin by four, and
    an under-count is the one failure this may not have.

    The price is refusing some prompts that would have fit — about four times
    too eagerly on English prose against a small window. That is the trade the
    measurement forces: an over-refusal is a visible refusal a caller can
    itemise, and an under-refusal is an answer nobody can tell from a good one.
    """
    return len(text.encode("utf-8"))


def estimate_prompt_tokens(messages: Sequence[Message]) -> int:
    """Bound the whole prompt: every message's content, role, and its wrapping."""
    return TEMPLATE_OVERHEAD_TOKENS + sum(
        MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.role) + estimate_tokens(message.content)
        for message in messages
    )


class ProviderProfile(NamedTuple):
    """What this ships knowing about one endpoint, so it need not be configured.

    Every field is a **default**, overridden by the matching environment
    variable whenever one is set — a profile decides nothing an operator has
    already decided. It exists so that pointing nodum at a known endpoint is a
    model name and a key, rather than four variables the first of which
    (a wrong ``NODUM_LLM_CONTEXT_TOKENS``) silently re-opens the truncation hole
    this module is mostly about.

    Attributes:
        models: The **exact** model ids this endpoint serves, which is what
            selects the profile when no base URL is configured. Exact, never a
            prefix — see :func:`profile_for`.
        hosts: The hostnames that *are* this endpoint. Compared to a parsed
            hostname, never as a substring of a URL.
        base_url: The endpoint, when :data:`ENV_BASE_URL` is unset.
        context_tokens: The window the endpoint really serves.
        structured_mode: Which ``response_format`` to try first, so a known
            provider pays no failed round trip discovering it.
        graded_thinking: Whether ``reasoning_effort`` above ``none`` is accepted.
    """

    models: frozenset[str]
    hosts: frozenset[str]
    base_url: str
    context_tokens: int
    structured_mode: str
    graded_thinking: bool


#: The endpoint DeepSeek's own hosted model ids resolve to.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

#: Endpoints this ships knowing about, keyed by exact model id and by hostname.
#:
#: Deliberately tiny, and deliberately not a vendor registry: a profile earns
#: its place by being an endpoint whose *defaults are wrong* otherwise. DeepSeek
#: is the only row, because it is the only endpoint measured on which all four
#: fields differ from this module's defaults.
#:
#: **ollama is not a row and does not need one.** Three of its four values are
#: already the defaults here (4 096, ``json_schema``, its own base URL), and the
#: fourth — it answers 400 to every graded reasoning level, including on
#: ``qwen3:8b``, which thinks — is decided in :func:`_resolve_default` by
#: comparing the resolved base URL against :data:`DEFAULT_BASE_URL`. A profile
#: keyed on model ids could not have carried it anyway: ollama serves whatever
#: the operator pulled, so there is no list of exact ids to match.
#:
#: A provider that is *not* recognised keeps the optimistic beliefs and
#: negotiates them down on the first 400, so this table is an optimisation and
#: never a gate.
_PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        # The two ids `GET https://api.deepseek.com/models` really lists. A
        # model id is an exact string and this list is short, so there is no
        # reason to guess at one — and guessing is what made this dangerous;
        # see `profile_for`.
        models=frozenset({"deepseek-v4-flash", "deepseek-v4-pro"}),
        hosts=frozenset({"api.deepseek.com"}),
        base_url=DEEPSEEK_BASE_URL,
        # Measured on `deepseek-v4-flash`: a 1 000 000-token context and a
        # 384 000-token max output, which are separate limits rather than
        # one shared window — the reservation is harmless there and
        # load-bearing on ollama, so it is kept for both.
        context_tokens=1_000_000,
        # Measured: `json_schema` is HTTP 400 "This response_format type is
        # unavailable now"; `json_object` works.
        structured_mode=STRUCTURED_JSON_OBJECT,
        graded_thinking=True,
    ),
)


def _hostname(base_url: str) -> str:
    """The host a base URL names, lower-cased and without its port, or ``""``.

    A URL this cannot parse is ``""`` rather than an exception. Matching a
    profile is an optimisation and it runs *before* :func:`base_url_problem`
    refuses the same URL — measured, ``urlsplit`` raises
    ``ValueError("Invalid IPv6 URL")`` on ``http://[bad/v1``, and letting that
    out of here would turn a wrong URL into a traceback at *resolution* time,
    ahead of the sentence that says what is wrong with it.

    ``SplitResult.hostname`` lower-cases on its own (CPython's ``_hostinfo``),
    so ``https://API.DeepSeek.com/v1`` arrives here already folded and nothing
    folds it a second time —
    :func:`test_the_profiled_host_is_matched_however_the_url_is_spelled` is what
    holds that.
    """
    try:
        # A base URL with no scheme (`api.deepseek.com/v1`) parses as a bare
        # path, so it is given the authority marker before the host is read.
        # This is deliberately more forgiving than `base_url_problem`: a host is
        # readable out of a spelling nothing can POST to, and reading it is how
        # the refusal can be about *that endpoint* rather than about a string.
        split = urllib.parse.urlsplit(base_url if "//" in base_url else f"//{base_url}")
        return split.hostname or ""
    except ValueError:
        return ""


def _completions_url(base_url: str) -> str:
    """The chat-completions URL a base URL names. One spelling, in one place."""
    return f"{base_url.rstrip('/')}/chat/completions"


def base_url_problem(base_url: str) -> str | None:
    """Why ``urllib`` cannot POST to this base URL, or ``None`` when it can.

    **The check is the operation itself**, not a heuristic about it:
    ``urllib.request.Request`` parses the URL in its constructor and makes no
    network call, so asking it to build the request this provider will really
    send is exact — it accepts everything :meth:`OpenAICompatProvider._post`
    would accept and refuses everything it would refuse, and it cannot
    over-refuse a spelling that works.

    Two spellings reach it, both driven rather than imagined:
    ``api.deepseek.com/v1`` is ``ValueError: unknown url type`` (a base URL with
    no scheme) and ``http://[bad/v1`` is ``ValueError: Invalid IPv6 URL``.

    **A scheme-less base URL is refused here rather than repaired**, which is
    the decision worth stating because :func:`_hostname` deliberately reads a
    host out of one. Repairing it means choosing ``http`` or ``https`` on the
    operator's behalf, and that is not a spelling detail: it decides whether
    :data:`ENV_API_KEY` crosses the network in clear text. Guessing ``https``
    breaks a local gateway, guessing ``http`` publishes a bearer token, and
    neither is a guess this module is entitled to make from four characters that
    are not there. So it is refused with the sentence that fixes it, exactly as
    an unparseable :data:`ENV_CONTEXT_TOKENS` and an unrecognised
    :data:`ENV_THINKING` already are: unusable configuration is *no provider
    with a reason*, at resolution time, where ``nodum llm status`` prints it and
    exits 0.
    """
    try:
        urllib.request.Request(_completions_url(base_url), method="POST")
    except ValueError as failure:
        return str(failure)
    return None


def profile_for(*, model: str, base_url: str | None) -> ProviderProfile | None:
    """The shipped profile for this model/endpoint pair, or ``None``.

    **A configured base URL decides, and a model name only decides when there
    is none.** That asymmetry is the whole of this function, and it is here
    because the obvious alternative — match a ``deepseek-`` *prefix* on the
    model name — sends a local install's prompts to a third party:

    - ``deepseek-r1``, ``deepseek-coder``, ``deepseek-coder-v2``,
      ``deepseek-llm``, ``deepseek-v2``, ``deepseek-v3`` and ``deepseek-v3.1``
      are all **ollama library models**, run locally with no key. Under a prefix
      match ``NODUM_LLM_MODEL=deepseek-r1:8b`` with no :data:`ENV_BASE_URL`
      resolved to ``https://api.deepseek.com/v1`` — so a configuration that says
      "run locally" POSTed private graph text to a vendor if a key happened to
      be set, and 401'd against a host nobody configured if one did not.
    - An explicit ``NODUM_LLM_BASE_URL=http://localhost:11434/v1`` won the
      *URL* and still lost the *window*: :attr:`ProviderProfile.context_tokens`
      came from the profile, so a 1 000 000-token belief was carried against a
      server serving 4 096, which is exactly the silent-truncation hole the
      profile exists to close.

    So the match is on **exact** model ids (:attr:`ProviderProfile.models` — the
    ids the vendor's own ``/models`` really lists) and on a **parsed hostname**
    (:attr:`ProviderProfile.hosts`, never a substring: a proxy at
    ``deepseek-gw.lan`` is not DeepSeek). A model name nobody profiled keeps the
    local default and negotiates, which costs one 400; a wrong guess here costs
    egress, and only one of those two is recoverable.

    Args:
        model: :data:`ENV_MODEL`, already stripped — :func:`_resolve_default`
            is the one place that trims it, and trimming it a second time here
            was code no test could distinguish from its absence.
        base_url: :data:`ENV_BASE_URL` if it was set, else ``None``. **Not** the
            resolved default — a profile may not match against a URL it supplied
            itself.

    Returns:
        The profile whose endpoint this is, or ``None``.
    """
    if base_url:
        # The operator named the endpoint, so nothing but the endpoint decides:
        # a profile applies only where it *is* the host being pointed at, and
        # then its window is that host's own.
        host = _hostname(base_url)
        return next((profile for profile in _PROFILES if host in profile.hosts), None)
    name = model.casefold()
    return next((profile for profile in _PROFILES if name in profile.models), None)


def estimate_content_tokens(messages: Sequence[Message]) -> int:
    """Bound only what was really sent — no template or per-message overheads.

    :func:`estimate_prompt_tokens` is what a *refusal* is computed against, and
    it deliberately over-counts: the wrapping a chat template adds is real, and
    a refusal that ignored it would let a prompt through that does not fit.

    :attr:`Completion.prompt_truncated` is a different question and needs a
    different number. It asks *did the server read fewer tokens than these bytes
    can possibly cost*, and only bytes nodum actually put on the wire can answer
    it — :data:`TEMPLATE_OVERHEAD_TOKENS` and :data:`MESSAGE_OVERHEAD_TOKENS` are
    a guess at somebody else's template, not content. On a long prompt the
    difference is noise; on a short one the guess is most of the estimate, and
    the check fired on completions the server had read in full. Measured: the
    reachability probe reported ``prompt_tokens: 12`` against an 85-token
    estimate of a 33-byte prompt, was raised as :class:`ContextOverflow`, and
    ``nodum llm status`` announced a failed call on a healthy install.
    """
    return sum(
        estimate_tokens(message.role) + estimate_tokens(message.content) for message in messages
    )


def _names_field(detail: str, markers: Sequence[str]) -> bool:
    """Does this 400 name one of these fields, rather than a path inside one?

    The blunt half is :meth:`OpenAICompatProvider._negotiate`'s own argument: a
    server's sentence is the only signal this wire carries, so the markers are
    substrings and a false positive is a weaker request every endpoint accepts.

    The sharp half is :data:`_FIELD_PATH_SEPARATORS`: a marker immediately
    followed by ``.`` or ``[`` is the server *dereferencing* the field, which
    means it accepted the field and is complaining about its contents. Those are
    two opposite findings behind one substring, and only one of them is a
    capability signal.

    A message that dereferences a marker *anywhere* is read as a validation
    error, even if it also names it bare somewhere else. That is the
    conservative side of the same one-sidedness: the cost is one
    :class:`ProviderUnavailable` reaching the caller, which is what would have
    happened with no negotiation at all.
    """
    return any(
        marker in detail
        and not any(f"{marker}{separator}" in detail for separator in _FIELD_PATH_SEPARATORS)
        for marker in markers
    )


def _optional_count(block: Any, key: str) -> int:
    """Read a non-negative integer the wire may simply not carry.

    Absent is ``0`` and unreadable is ``0``, which is the opposite of how the
    cost fields are read one function up — and the asymmetry is the point.
    ``prompt_tokens`` missing means the meter does not know what the call cost,
    and a meter that guesses zero is a budget with a hole in it. These three are
    *decompositions* of numbers already read: reasoning is a share of
    ``completion_tokens``, and the cache counters split ``prompt_tokens``. A
    provider that reports neither has still reported the whole cost, so their
    absence loses detail rather than money.
    """
    if not isinstance(block, dict):
        return 0
    value = block.get(key)
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, count)


class OpenAICompatProvider:
    """An OpenAI-compatible ``/v1/chat/completions`` endpoint — both halves.

    Local (``ollama``) and remote are the same wire shape, verified by driving
    both. Transport is :mod:`urllib` from the standard library: this package
    ships no HTTP client dependency and one call does not justify adding one.

    **Two things about that shape are not actually universal**, and each was
    found by a 400 from a live server rather than by reading a specification:

    ``response_format: {"type": "json_schema"}``
        ollama honours it. ``deepseek-v4-flash`` answers
        ``HTTP 400 {"error":{"message":"This response_format type is
        unavailable now"}}`` and takes ``{"type": "json_object"}`` instead.
    ``reasoning_effort``
        ``deepseek-v4-flash`` takes ``none``/``low``/``medium``/``high``.
        ollama takes **only** ``none`` — ``low`` is
        ``HTTP 400 "llama3.2:1b" does not support thinking`` on a model without
        thinking and ``HTTP 400 think value "low" is not supported for this
        model`` on ``qwen3:8b``, which *does* think. So a graded level sent
        unconditionally breaks the entire local half.

    Both are handled the same way, and the way is **capability negotiation, not
    retry**: the request carries the strongest form this provider still believes
    in, a 400 whose body names the offending field downgrades that belief *on
    this instance*, and the call is re-sent once under the weaker form. It is
    bounded (:data:`_MAX_NEGOTIATIONS`), it happens at most once per capability
    per process, and it is not the retry policy the module docstring refuses to
    own — there is no backoff, no second attempt at the same request, and
    nothing here reacts to a timeout, a 5xx or a dropped connection. Those still
    go straight to the caller, whose budget is the only thing entitled to decide
    whether to pay twice.

    :func:`profile_for` seeds those beliefs for endpoints this ships knowing
    about, so the ordinary install pays no failed round trip at all.

    Args:
        base_url: The OpenAI-compatible root, e.g.
            ``http://localhost:11434/v1``. A trailing slash is tolerated.
        model: The model name sent as ``model``, and recorded as
            :attr:`model_id` on every completion.
        api_key: Bearer token, or ``None`` for a server that needs none.
        context_tokens: The window this endpoint *serves* — for ollama that is
            ``num_ctx``, not the model card's number. A prompt whose estimate
            plus its reserved output exceeds this is refused rather than sent.
        thinking: One of :data:`THINKING_LEVELS`. Anything else is a
            ``ValueError`` naming the set, because the alternative is a value
            the API neither rejects nor honours.
        structured_mode: The structured-output form to try first.
        graded_thinking: Whether this endpoint is believed to accept a
            ``reasoning_effort`` above ``none``. ``THINKING_NONE`` is sent
            regardless — it is accepted by every endpoint measured, and on
            ``qwen3:8b`` it is the documented cure for a ``<think>`` block
            eating the whole output ceiling.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str,
        api_key: str | None = None,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
        thinking: str = DEFAULT_THINKING,
        structured_mode: str = STRUCTURED_JSON_SCHEMA,
        graded_thinking: bool = True,
    ) -> None:
        if context_tokens < 1:
            raise ValueError(f"context_tokens must be at least 1, got {context_tokens}")
        if thinking not in THINKING_LEVELS:
            raise ValueError(
                f"thinking must be one of {', '.join(THINKING_LEVELS)}, got {thinking!r}"
            )
        if structured_mode not in (STRUCTURED_JSON_SCHEMA, STRUCTURED_JSON_OBJECT):
            raise ValueError(
                f"structured_mode must be {STRUCTURED_JSON_SCHEMA!r} or "
                f"{STRUCTURED_JSON_OBJECT!r}, got {structured_mode!r}"
            )
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._context_tokens = context_tokens
        self._thinking = thinking
        #: Downgraded in place by :meth:`_negotiate`, never upgraded: a server
        #: that refused a form once is not asked again for the life of the
        #: process, which is what keeps the failed round trip to one.
        self._structured_mode = structured_mode
        self._thinking_wire = _WIRE_GRADED if graded_thinking else _WIRE_OFF_ONLY

    @property
    def provider_id(self) -> str:
        """Where the text came from — the base URL, which is what distinguishes
        a local ollama from a remote API serving the same model name."""
        return self._base_url

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def context_tokens(self) -> int:
        return self._context_tokens

    @property
    def thinking(self) -> str:
        """The configured reasoning level — what was *asked* for."""
        return self._thinking

    @property
    def thinking_applied(self) -> bool:
        """Will the configured level actually reach the endpoint?

        ``False`` means the level is being withheld and the model is running at
        *its own* default — which measured as the most expensive setting of all,
        1 492 reasoning tokens where ``none`` spent 0. A knob that silently does
        nothing is worse than one that is absent, so ``nodum llm status`` reports
        this beside the level.
        """
        if self._thinking_wire == _WIRE_ABSENT:
            return False
        return self._thinking == THINKING_NONE or self._thinking_wire == _WIRE_GRADED

    @property
    def structured_mode(self) -> str:
        """Which ``response_format`` a schema will be sent under, right now.

        Starts at what :func:`profile_for` believed and drops to
        :data:`STRUCTURED_JSON_OBJECT` the first time a server refuses the
        stronger form. Public because the drop **weakens what a caller may
        assume about the body**, and a caller relying on a schema ``pattern``
        for correctness has to be able to see that the pattern is now advice.
        """
        return self._structured_mode

    def estimate_prompt_tokens(
        self, messages: Sequence[Message], *, schema: dict[str, Any] | None = None
    ) -> int:
        """See :func:`estimate_prompt_tokens` — an over-count, never an under-count.

        ``schema`` matters because under :data:`STRUCTURED_JSON_OBJECT` the
        schema is *stated in the prompt*, so it costs prompt tokens that a
        caller fitting its context to this number has to be told about.
        Omitting it estimates the messages alone, which is exact under
        :data:`STRUCTURED_JSON_SCHEMA` and an under-count under the fallback —
        and an under-count is the one failure this estimate may not have. So a
        caller that will pass a schema should pass it here too.
        """
        return estimate_prompt_tokens(self._outgoing(messages, schema))

    def _outgoing(
        self, messages: Sequence[Message], schema: dict[str, Any] | None
    ) -> list[Message]:
        """The messages as they will actually be sent.

        Under :data:`STRUCTURED_JSON_SCHEMA` that is the caller's list unchanged.
        Under the fallback it gains one system message stating the schema,
        because ``json_object`` fixes only that the body *is* an object — every
        constraint inside the schema has to be said in words or it is not said
        at all.

        **Two measured facts decide where it goes and what it says.** The
        endpoint refuses ``response_format: json_object`` outright unless the
        prompt contains the word "json"
        (``HTTP 400 "Prompt must contain the word 'json' in some form"``), so the
        instruction is not optional decoration. And prompt caching matches on a
        **prefix** at 1/50th the price, so the stable block goes at the front:
        this message is inserted after the caller's leading system messages,
        keeping instructions contiguous ahead of whatever varies per call, and
        the schema is rendered with sorted keys so two calls that mean the same
        schema produce the same bytes.
        """
        if schema is None or self._structured_mode != STRUCTURED_JSON_OBJECT:
            return list(messages)
        rendered = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        instruction = Message(
            role="system", content=JSON_OBJECT_INSTRUCTION.format(schema=rendered)
        )
        outgoing = list(messages)
        cut = 0
        while cut < len(outgoing) and outgoing[cut].role == "system":
            cut += 1
        outgoing.insert(cut, instruction)
        return outgoing

    def output_reservation(self, max_output_tokens: int) -> int:
        """How much of the window this call reserves for its answer.

        The reservation is a **share of the window** capped by the caller's
        ceiling, rather than the ceiling itself (see
        :data:`OUTPUT_RESERVATION_FRACTION`). The returned number is both
        subtracted from the allowance *and* sent as ``max_tokens``, so the two
        can never disagree: telling the server it may generate more than was
        reserved is exactly the overrun the reservation exists to stop.
        """
        share = int(self._context_tokens * OUTPUT_RESERVATION_FRACTION)
        return max(1, min(max_output_tokens, share))

    def chat(
        self,
        messages: Sequence[Message],
        *,
        schema: dict[str, Any] | None = None,
        max_output_tokens: int,
        timeout: float,
        thinking: str | None = None,
    ) -> Completion:
        """Send one prompt; return one completion.

        Args:
            messages: The prompt, in order.
            schema: A JSON schema for ``response_format``. It makes the
                *envelope* reliable — five probes that produced one unparseable
                body free-form produced five parseable ones under a schema —
                and it makes the *content* no truer at all. Do not read a
                schema-valid object as a correct one. Under
                :data:`STRUCTURED_JSON_OBJECT` it is weaker still: the schema
                becomes a sentence in the prompt, so a ``pattern`` inside it is
                a request rather than a constraint. :attr:`structured_mode` and
                :attr:`Completion.structured_mode` are how a caller tells which
                it got.
            max_output_tokens: The output ceiling asked for. What is actually
                reserved and sent is :meth:`output_reservation` of it, which is
                capped at a share of the window. A call that comes back at that
                ceiling is a failed call (B3), not a short one.
            timeout: Per-call wall-clock ceiling in seconds (B2). Tokens alone
                do not bound a night: 2 395 prompt tokens cost 47 seconds here.
                It bounds the **whole** call including any capability
                negotiation, so a downgrade cannot buy a second full ceiling.
            thinking: Per-call-site reasoning level, overriding the provider's
                configured one. The call sites do not want the same thing: a
                reachability probe has nothing to reason about and measured 506
                thinking tokens and an *empty body* when it was allowed to try,
                while an answer worth reviewing is exactly where reasoning is
                wanted.

        Returns:
            The completion, whatever ``finish_reason`` it carries. Classifying
            a ``length`` finish or a filled context as a *failure* is
            :mod:`nodum.agent`'s job, because that is where the budget the
            failed call must still be charged against lives.

        Raises:
            ValueError: If ``max_output_tokens`` or ``thinking`` is unusable.
            PromptTooLong: If the prompt's estimate exceeds what is left of the
                window — raised **before** the request, so nothing is spent.
            ProviderTimeout: If ``timeout`` bit.
            ProviderUnavailable: If the endpoint could not be reached, answered
                a non-2xx status, or answered something this cannot read.
        """
        if max_output_tokens < 1:
            raise ValueError(f"max_output_tokens must be at least 1, got {max_output_tokens}")
        level = self._thinking if thinking is None else thinking
        if level not in THINKING_LEVELS:
            raise ValueError(f"thinking must be one of {', '.join(THINKING_LEVELS)}, got {level!r}")
        reservation = self.output_reservation(max_output_tokens)
        allowance = self._context_tokens - reservation
        if allowance < 1:
            raise ValueError(
                f"a {self._context_tokens}-token context window leaves no room for a prompt "
                f"once {reservation} is reserved for the answer"
            )
        started = time.monotonic()
        deadline = started + timeout
        # The first attempt gets the ceiling **exactly**, not the ceiling minus
        # however long the arithmetic took: a caller that asked for 5 s and can
        # read what the transport was given should see 5 s. Only a re-send after
        # a negotiation is charged what the first attempt used, which is the
        # point of the deadline — a downgrade must not buy a second full ceiling.
        remaining = timeout
        for _ in range(_MAX_NEGOTIATIONS + 1):
            # Rebuilt every attempt: a downgrade to `json_object` puts the
            # schema into the prompt, which changes both the payload and what
            # the prompt costs, so neither may be computed once outside the loop.
            outgoing = self._outgoing(messages, schema)
            estimate = estimate_prompt_tokens(outgoing)
            content_estimate = estimate_content_tokens(outgoing)
            if estimate > allowance:
                raise PromptTooLong(
                    f"prompt is about {estimate} tokens and at most {allowance} fit "
                    f"({self._context_tokens}-token window, {reservation} reserved for the "
                    f"answer). Refused before sending: this server truncates a long prompt "
                    f"silently — same prompt_tokens, same finish_reason, no error — so a call "
                    f"made here would answer from a prefix and read exactly like a good answer. "
                    f"Send less context, or raise {ENV_CONTEXT_TOKENS} — but only to a window "
                    f"the server really serves (for ollama that is num_ctx / "
                    f"OLLAMA_CONTEXT_LENGTH, not the model card)"
                )
            payload = self._payload(outgoing, schema=schema, reservation=reservation, level=level)
            sent_mode = payload.get("response_format", {}).get("type", STRUCTURED_NONE)
            sent_thinking = payload.get("reasoning_effort")
            try:
                body = self._post(payload, remaining)
            except ProviderUnavailable as failure:
                if self._negotiate(failure):
                    remaining = max(_MIN_TIMEOUT, deadline - time.monotonic())
                    continue
                raise
            latency_ms = int((time.monotonic() - started) * 1000)
            return self._completion(
                body,
                latency_ms,
                estimate,
                content_estimate=content_estimate,
                structured_mode=sent_mode,
                output_ceiling=reservation,
                thinking=sent_thinking,
            )
        raise ProviderUnavailable(  # pragma: no cover — the loop returns or raises
            f"provider at {self._base_url} refused every form this knows how to send"
        )

    def _payload(
        self,
        outgoing: Sequence[Message],
        *,
        schema: dict[str, Any] | None,
        reservation: int,
        level: str,
    ) -> dict[str, Any]:
        """Build the request body under this provider's current beliefs."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in outgoing
            ],
            "max_tokens": reservation,
            "temperature": TEMPERATURE,
        }
        if schema is not None:
            if self._structured_mode == STRUCTURED_JSON_SCHEMA:
                payload["response_format"] = {
                    "type": STRUCTURED_JSON_SCHEMA,
                    "json_schema": {"name": "nodum", "schema": schema},
                }
            else:
                payload["response_format"] = {"type": STRUCTURED_JSON_OBJECT}
        # `none` goes to every endpoint measured; a graded level goes only where
        # one is believed to be understood, because ollama answers 400 to all
        # three of them — including on a model that really does think. An
        # endpoint that has refused the field outright gets nothing.
        if self._thinking_wire == _WIRE_GRADED:
            payload["reasoning_effort"] = level
        elif self._thinking_wire == _WIRE_OFF_ONLY and level == THINKING_NONE:
            payload["reasoning_effort"] = THINKING_NONE
        return payload

    def _negotiate(self, failure: ProviderUnavailable) -> bool:
        """Downgrade one belief if this 400 says a field is unsupported.

        Returns ``True`` when something changed and the call is worth re-sending.

        **Matching is on the server's message, which is as brittle as it
        sounds** — there is no machine-readable "unsupported feature" signal in
        this wire, and the alternative is a vendor allowlist that is wrong for
        every provider not on it. The brittleness is one-sided on purpose, the
        same way :func:`estimate_tokens` is: a false positive downgrades to a
        weaker request that every endpoint accepts and says so in
        ``nodum llm status``, while a false negative is today's behaviour
        exactly — the ``ProviderUnavailable`` reaches the caller unchanged.
        Neither can produce a silently worse answer.

        Only a **400** is read this way. A 5xx, a timeout and a dropped
        connection are transport failures that say nothing about capabilities,
        and treating one as a capability signal would permanently cripple a
        provider over a bad minute.
        """
        if getattr(failure, "status", None) != 400:
            return False
        detail = str(failure).casefold()
        if self._structured_mode == STRUCTURED_JSON_SCHEMA and _names_field(
            detail, _STRUCTURED_REJECTIONS
        ):
            self._structured_mode = STRUCTURED_JSON_OBJECT
            return True
        if self._thinking_wire != _WIRE_ABSENT and any(
            marker in detail for marker in _THINKING_REJECTIONS
        ):
            # One rung at a time: an endpoint that refuses a *graded* level may
            # still take `none` (ollama does, measured), and one that refuses
            # `none` too takes no field at all. Collapsing the two would either
            # give up `none` on ollama — the documented cure for `qwen3:8b`
            # answering with an empty body — or loop forever on the server that
            # understands neither.
            self._thinking_wire = (
                _WIRE_OFF_ONLY if self._thinking_wire == _WIRE_GRADED else _WIRE_ABSENT
            )
            return True
        return False

    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST the payload and return the parsed body, or raise."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            # Built **inside** the try, and that placement is the whole fix.
            # `Request.__init__` parses the URL, so it is the second thing here
            # that raises on bad configuration rather than on a bad network —
            # measured, `ValueError: unknown url type` for a scheme-less
            # `api.deepseek.com/v1` and `ValueError: Invalid IPv6 URL` for
            # `http://[bad/v1`. Constructed one line above the try, neither
            # reached an except clause: they escaped `LLMError`, escaped the
            # `(BudgetExhausted, LLMError)` handlers in `nodum.answers`, and
            # turned a malformed *configuration* into the traceback and the 400
            # reserved for a malformed *request*. Exactly the shape
            # `IncompleteRead` had below, one layer earlier.
            request = urllib.request.Request(
                _completions_url(self._base_url),
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
        except ValueError as failure:
            # No status: nothing was asked, so this is not a capability signal
            # and `_negotiate` will not read it as one.
            raise ProviderUnavailable(
                f"provider at {self._base_url} is not a URL this can POST to ({failure}). "
                f"Set {ENV_BASE_URL} to a full OpenAI-compatible root, scheme included "
                f"(for example {DEFAULT_BASE_URL})"
            ) from failure
        except urllib.error.HTTPError as failure:
            # Measured: an unknown model on a live server is an HTTP 404
            # carrying a JSON error body, in about a millisecond. The body is
            # the only thing that says *which* misconfiguration it was, so a
            # slice of it goes in the message.
            detail = failure.read().decode("utf-8", "replace")[:200]
            # The one rejection this module causes itself. Withholding the key
            # is right — it stops a stale variable reaching a host nobody named
            # — but a local gateway that legitimately requires a key then 401s
            # on the default base URL, and nothing in the failure said why, so
            # the operator reads it as "my key is wrong". `llm status` carried
            # the answer; the failure that needed it did not.
            withheld = ""
            if failure.code in {401, 403} and _key_withheld_reason is not None:
                withheld = f". {_key_withheld_reason}"
            raise ProviderUnavailable(
                f"provider at {self._base_url} answered HTTP {failure.code}: {detail}{withheld}",
                status=failure.code,
            ) from failure
        except TimeoutError as failure:
            raise ProviderTimeout(
                f"provider at {self._base_url} did not answer within {timeout:g}s"
            ) from failure
        except http.client.HTTPException as failure:
            # A provider that died mid-response: `response.read()` raises
            # `IncompleteRead` when the body stops short of its
            # `Content-Length`. That class derives from `HTTPException`, **not**
            # from `OSError`, so the clause below misses it entirely — and it
            # then escaped `LLMError`, escaped the callers' handlers and reached
            # a CLI traceback and an HTTP 500. It is the shape a killed
            # provider, a proxy timeout and a dropped load-balancer connection
            # all produce, which is to say the ordinary way a long call dies.
            raise ProviderUnavailable(
                f"provider at {self._base_url} is not reachable: the response ended early "
                f"({type(failure).__name__}: {failure})"
            ) from failure
        except (urllib.error.URLError, OSError) as failure:
            # `URLError` wraps a socket timeout on some Python versions, so the
            # timeout case is re-checked here rather than trusted to land above.
            reason = getattr(failure, "reason", failure)
            if isinstance(reason, TimeoutError):
                raise ProviderTimeout(
                    f"provider at {self._base_url} did not answer within {timeout:g}s"
                ) from failure
            raise ProviderUnavailable(
                f"provider at {self._base_url} is not reachable: {reason}"
            ) from failure
        try:
            parsed = json.loads(raw)
        except ValueError as failure:
            raise ProviderUnavailable(
                f"provider at {self._base_url} answered something that is not JSON"
            ) from failure
        if not isinstance(parsed, dict):
            raise ProviderUnavailable(
                f"provider at {self._base_url} answered a {type(parsed).__name__}, not an object"
            )
        return parsed

    def _completion(
        self,
        body: dict[str, Any],
        latency_ms: int,
        estimate: int,
        *,
        content_estimate: int = 0,
        structured_mode: str,
        output_ceiling: int,
        thinking: str | None,
    ) -> Completion:
        """Read the wire shape into a :class:`Completion`, or say what was missing.

        The counted fields are read defensively: a provider that answers 200
        with a shape this does not recognise is unavailable, not a completion
        with zeros in it. A silently zeroed ``usage`` would be a call that cost
        nothing according to the budget, which is the one lie a meter may not
        tell.

        **The reasoning and cache counters are the exception, and deliberately
        so** (:func:`_optional_count`). They are absent from every ollama
        response and — measured — absent from ``deepseek-v4-flash``'s own
        response at ``reasoning_effort: "none"``, where the entire
        ``completion_tokens_details`` block disappears rather than reporting
        zero. Treating their absence as an unreadable shape would make the one
        predictable reasoning setting the one that cannot be used.

        **Two ways the block can be self-contradictory are checked here rather
        than believed**, because both of them are arithmetic this module has
        already stated as an invariant and neither can be checked one function
        down, where only one number is in scope (:func:`_optional_count`):

        - ``reasoning_tokens`` above ``completion_tokens``.
          :attr:`Completion.reasoning_tokens` documents itself as a *share* of
          the output — measured true on every ``deepseek-v4-flash`` call — and
          :attr:`Completion.content_tokens` subtracts one from the other. A wire
          reporting 5 000 reasoning inside 50 completion makes that difference
          ``0`` and prints a report where the thinking is larger than the output
          it is part of. No budget moves either way (reasoning is never summed
          into the spend), so it is clamped rather than refused.
        - ``usage.total_tokens`` disagreeing with ``prompt + completion``.
          Overriding it is right — :attr:`Completion.total_tokens` is what
          budgets are denominated in and it must be one rule everywhere — but
          *not reading it at all* meant a provider that bills differently from
          the way this meters could never be noticed.

        Neither fails the call. Both are logged, which is the only thing in this
        module that is: they are facts about the *provider*, actionable by
        whoever operates it, and useless to the caller who wanted an answer.
        """
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
            finish_reason = choice["finish_reason"]
            usage = body["usage"]
            prompt_tokens = int(usage["prompt_tokens"])
            output_tokens = int(usage["completion_tokens"])
        except (KeyError, IndexError, TypeError, ValueError) as failure:
            raise ProviderUnavailable(
                f"provider at {self._base_url} answered a shape this cannot read "
                f"({type(failure).__name__}: {failure}); expected OpenAI-compatible "
                f"choices[0].message.content plus a usage block"
            ) from failure
        reasoning_tokens = _optional_count(
            (usage.get("completion_tokens_details") or {}), "reasoning_tokens"
        )
        if reasoning_tokens > output_tokens:
            _log.warning(
                "provider at %s reported %d reasoning tokens inside %d completion tokens; "
                "reasoning is a share of the completion, so it is clamped to it",
                self._base_url,
                reasoning_tokens,
                output_tokens,
            )
            reasoning_tokens = output_tokens
        # `0` is "the wire did not say" — the ordinary ollama shape — and not a
        # provider claiming the call cost nothing.
        reported_total = _optional_count(usage, "total_tokens")
        if reported_total and reported_total != prompt_tokens + output_tokens:
            _log.warning(
                "provider at %s reported usage.total_tokens %d for %d prompt + %d completion "
                "tokens; nodum bills prompt + completion, so the two disagree about this call",
                self._base_url,
                reported_total,
                prompt_tokens,
                output_tokens,
            )
        return Completion(
            text=text if isinstance(text, str) else "",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            finish_reason=str(finish_reason),
            model_id=self._model,
            provider_id=self._base_url,
            context_tokens=self._context_tokens,
            latency_ms=latency_ms,
            prompt_estimate=estimate,
            prompt_content_estimate=content_estimate,
            reasoning_tokens=reasoning_tokens,
            cache_hit_tokens=_optional_count(usage, "prompt_cache_hit_tokens"),
            cache_miss_tokens=_optional_count(usage, "prompt_cache_miss_tokens"),
            structured_mode=structured_mode,
            output_ceiling=output_ceiling,
            thinking=thinking,
        )


# ── Provider resolution (cached process-wide) ────────────────────────────────
#
# The same three functions plus a reset that `nodum.embeddings` exposes, for the
# reason in the module docstring: one way to be offline.

_provider: LLMProvider | None = None
_unavailable_reason: str | None = None
_key_withheld_reason: str | None = None
_resolved = False


def get_provider() -> LLMProvider | None:
    """Return the configured provider, or ``None`` when there is none.

    **Resolution reads configuration and makes no network call**, which is
    where this deliberately differs from :func:`nodum.embeddings.get_provider`
    (whose construction loads a model, so an unusable model fails there rather
    than mid-run). Here "configured" and "reachable" are genuinely different
    facts: a server that is down at 03:00 and up at 03:05 is not a
    configuration change, and a probe at resolution time would cache one
    instant's answer for the life of the process. An unreachable endpoint
    surfaces as :class:`ProviderUnavailable` on the first call, in a
    millisecond — measured.
    """
    global _provider, _unavailable_reason, _key_withheld_reason, _resolved
    if not _resolved:
        _provider, _unavailable_reason, _key_withheld_reason = _resolve_default()
        _resolved = True
    return _provider


def unavailable_reason() -> str | None:
    """Return why no provider is available (``None`` when one is)."""
    get_provider()
    return _unavailable_reason


def key_withheld_reason() -> str | None:
    """Return why :data:`ENV_API_KEY` is not being sent, or ``None``.

    Shaped exactly like :func:`unavailable_reason`, and for the same reason: it
    is a fact settled at *resolution* time, out of configuration alone, and a
    caller must be able to read it without holding the provider (P3).

    ``None`` covers both "no key was configured" and "the key is being sent";
    a string means a key **is** configured and this deliberately did not attach
    it. The distinction that matters to a human is the string, and it names the
    endpoint the key would otherwise have gone to. See :func:`_resolve_default`.
    """
    get_provider()
    return _key_withheld_reason


def set_provider(provider: LLMProvider | None, *, reason: str | None = None) -> None:
    """Force the provider — the test and configuration seam.

    Passing ``None`` forces the unavailable state; ``reason`` is what
    :func:`unavailable_reason` reports. No test may assert on real model output
    (temperature-0 determinism is a local-backend property, not a contract), so
    every test that needs a completion injects one here.
    """
    global _provider, _unavailable_reason, _key_withheld_reason, _resolved
    _provider = provider
    _unavailable_reason = None if provider is not None else (reason or "no LLM provider configured")
    # A forced provider was built by the caller, who named its endpoint and its
    # key together; there is nothing for this module to have withheld.
    _key_withheld_reason = None
    _resolved = True


def reset_provider() -> None:
    """Drop the cached resolution; the next use re-resolves from scratch."""
    global _provider, _unavailable_reason, _key_withheld_reason, _resolved
    _provider = None
    _unavailable_reason = None
    _key_withheld_reason = None
    _resolved = False


def _resolve_default() -> tuple[LLMProvider | None, str | None, str | None]:
    """Build the configured provider, or explain why there is none.

    Returns the provider, why there is none, and why a configured
    :data:`ENV_API_KEY` was not given to it — at most one of the last two is
    ever a string.

    **The key travels only to an endpoint somebody named.** Two things can name
    one: the operator, by setting :data:`ENV_BASE_URL`, or the model id, by
    being exactly a hosted id a shipped profile serves. When neither did, the
    base URL is :data:`DEFAULT_BASE_URL` — a host **this module chose**, not one
    the key was configured for — and the key is dropped here rather than
    attached in :meth:`OpenAICompatProvider._post`.

    The hole that closes: ``NODUM_LLM_MODEL`` naming a hosted model this does
    not recognise (a typo, a newer id) plus ``NODUM_LLM_API_KEY`` resolved to
    ``http://localhost:11434/v1`` and sent the vendor's bearer token there.
    Driven end to end: the prompt correctly stayed local and the credential did
    not stay with the vendor — it arrived at a throwaway listener as
    ``Authorization: Bearer …``, ready to be read out of any local process's
    logs.

    **Withheld, not refused**, because the local default needs no key: a stale
    variable left over from an experiment must not stop a working local install,
    and refusing would make it. And the case the fix may not break — a key
    against a **self-hosted gateway** that requires one — is precisely the case
    where the operator names ``NODUM_LLM_BASE_URL``, so it keeps its key by the
    same rule.
    """
    model = (os.environ.get(ENV_MODEL) or "").strip()
    if not model:
        return (
            None,
            (
                f"no LLM provider configured (set {ENV_MODEL} to a model the endpoint serves; "
                f"{ENV_BASE_URL} defaults to {DEFAULT_BASE_URL})"
            ),
            None,
        )
    configured_base = (os.environ.get(ENV_BASE_URL) or "").strip() or None
    profile = profile_for(model=model, base_url=configured_base)
    default_base = profile.base_url if profile is not None else DEFAULT_BASE_URL
    base_url = configured_base or default_base
    problem = base_url_problem(base_url)
    if problem is not None:
        return (
            None,
            (
                # Attribute the URL to whatever actually produced it. Only
                # `configured_base` came from the operator; naming ENV_BASE_URL
                # for a profile's URL or the shipped default would send them to
                # edit a variable they never set. Unreachable while both shipped
                # constants parse — which is exactly how long a wrong sentence
                # here would go unnoticed, so it is written right rather than
                # argued away.
                f"{ENV_BASE_URL}={base_url!r}"
                if configured_base is not None
                else f"the default endpoint {base_url!r}"
            )
            + (
                f" is not a URL this can POST to ({problem}). "
                f"Give it a full OpenAI-compatible root, scheme included — for example "
                f"{DEFAULT_BASE_URL} or {DEEPSEEK_BASE_URL}"
            ),
            None,
        )
    api_key = (os.environ.get(ENV_API_KEY) or "").strip() or None
    key_withheld: str | None = None
    if api_key is not None and configured_base is None and profile is None:
        key_withheld = (
            f"{ENV_API_KEY} is set and is not being sent: nothing named an endpoint for it. "
            f"{ENV_MODEL}={model!r} is not a hosted model id this ships a profile for and "
            f"{ENV_BASE_URL} is unset, so calls go to the local default {base_url} — a host "
            f"the key was not configured for. Name the endpoint the key belongs to with "
            f"{ENV_BASE_URL} (a local gateway that requires a key counts), or use the exact "
            f"hosted model id"
        )
        api_key = None
    raw_context = (os.environ.get(ENV_CONTEXT_TOKENS) or "").strip()
    context_tokens = profile.context_tokens if profile is not None else DEFAULT_CONTEXT_TOKENS
    if raw_context:
        try:
            context_tokens = int(raw_context)
        except ValueError:
            return (
                None,
                f"{ENV_CONTEXT_TOKENS}={raw_context!r} is not a whole number of tokens",
                None,
            )
    raw_thinking = (os.environ.get(ENV_THINKING) or "").strip().casefold()
    thinking = raw_thinking or DEFAULT_THINKING
    if thinking not in THINKING_LEVELS:
        # Refused rather than defaulted, which is where this deliberately parts
        # company with `nodum.agent`'s "an unparseable value falls back" rule.
        # That rule is right for a *number*, where the fallback is a smaller
        # ceiling and the worst case is less work. A reasoning level is a name,
        # and a name the API does not know is not a slower call — it is one the
        # server may accept and not honour, spending tokens under a setting
        # nobody chose and reporting the level that was asked for. So an
        # unrecognised level is no provider at all, with a reason, exactly as
        # an unparseable context window already is.
        return (
            None,
            (
                f"{ENV_THINKING}={raw_thinking!r} is not a reasoning level nodum accepts "
                f"(one of: {', '.join(THINKING_LEVELS)})"
            ),
            None,
        )
    # ollama is the default endpoint and takes only `none`, so an unrecognised
    # provider on the default base URL starts out disbelieving graded levels
    # rather than paying a 400 to find out. Anything else starts optimistic and
    # negotiates down — see `OpenAICompatProvider._negotiate`.
    if profile is not None:
        structured_mode = profile.structured_mode
        graded_thinking = profile.graded_thinking
    else:
        structured_mode = STRUCTURED_JSON_SCHEMA
        graded_thinking = base_url.rstrip("/") != DEFAULT_BASE_URL
    try:
        provider = OpenAICompatProvider(
            base_url=base_url,
            model=model,
            api_key=api_key,
            context_tokens=context_tokens,
            thinking=thinking,
            structured_mode=structured_mode,
            graded_thinking=graded_thinking,
        )
    except ValueError as failure:
        return None, f"LLM provider configuration is unusable: {failure}", None
    return provider, None, key_withheld
