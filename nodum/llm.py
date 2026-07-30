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
    OpenAI-compatible base URL; defaults to :data:`DEFAULT_BASE_URL`.
``NODUM_LLM_API_KEY``
    Bearer token. Optional — the local default needs none.
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
import os
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel

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

#: Environment variables. Named as constants so a test asserts on the same
#: string the code reads.
ENV_MODEL = "NODUM_LLM_MODEL"
ENV_BASE_URL = "NODUM_LLM_BASE_URL"
ENV_API_KEY = "NODUM_LLM_API_KEY"
ENV_CONTEXT_TOKENS = "NODUM_LLM_CONTEXT_TOKENS"


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
    """The provider could not be reached, or answered something unusable."""


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

    @property
    def total_tokens(self) -> int:
        """What the call cost, in the unit budgets are denominated in (B2)."""
        return self.prompt_tokens + self.output_tokens

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
        if self.prompt_estimate <= 0:
            return False
        return self.prompt_tokens * MAX_BYTES_PER_TOKEN < self.prompt_estimate


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

    def estimate_prompt_tokens(self, messages: Sequence[Message]) -> int:
        """An over-count of what these messages will cost as a prompt."""
        ...

    def chat(
        self,
        messages: Sequence[Message],
        *,
        schema: dict[str, Any] | None = None,
        max_output_tokens: int,
        timeout: float,
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


class OpenAICompatProvider:
    """An OpenAI-compatible ``/v1/chat/completions`` endpoint — both halves.

    Local (``ollama``) and remote are the same wire shape, verified by driving
    the local one. Transport is :mod:`urllib` from the standard library: this
    package ships no HTTP client dependency and one call does not justify
    adding one.

    Args:
        base_url: The OpenAI-compatible root, e.g.
            ``http://localhost:11434/v1``. A trailing slash is tolerated.
        model: The model name sent as ``model``, and recorded as
            :attr:`model_id` on every completion.
        api_key: Bearer token, or ``None`` for a server that needs none.
        context_tokens: The window this endpoint *serves* — for ollama that is
            ``num_ctx``, not the model card's number. A prompt whose estimate
            plus its reserved output exceeds this is refused rather than sent.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str,
        api_key: str | None = None,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    ) -> None:
        if context_tokens < 1:
            raise ValueError(f"context_tokens must be at least 1, got {context_tokens}")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._context_tokens = context_tokens

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

    def estimate_prompt_tokens(self, messages: Sequence[Message]) -> int:
        """See :func:`estimate_prompt_tokens` — an over-count, never an under-count."""
        return estimate_prompt_tokens(messages)

    def chat(
        self,
        messages: Sequence[Message],
        *,
        schema: dict[str, Any] | None = None,
        max_output_tokens: int,
        timeout: float,
    ) -> Completion:
        """Send one prompt; return one completion.

        Args:
            messages: The prompt, in order.
            schema: A JSON schema for ``response_format``. It makes the
                *envelope* reliable — five probes that produced one unparseable
                body free-form produced five parseable ones under a schema —
                and it makes the *content* no truer at all. Do not read a
                schema-valid object as a correct one.
            max_output_tokens: The output ceiling, reserved out of the context
                window before the prompt is measured against it. A call that
                comes back at this ceiling is a failed call (B3), not a short
                one — the body is cut mid-token.
            timeout: Per-call wall-clock ceiling in seconds (B2). Tokens alone
                do not bound a night: 2 395 prompt tokens cost 47 seconds here.

        Returns:
            The completion, whatever ``finish_reason`` it carries. Classifying
            a ``length`` finish or a filled context as a *failure* is
            :mod:`nodum.agent`'s job, because that is where the budget the
            failed call must still be charged against lives.

        Raises:
            ValueError: If ``max_output_tokens`` leaves no room for a prompt.
            PromptTooLong: If the prompt's estimate exceeds what is left of the
                window — raised **before** the request, so nothing is spent.
            ProviderTimeout: If ``timeout`` bit.
            ProviderUnavailable: If the endpoint could not be reached, answered
                a non-2xx status, or answered something this cannot read.
        """
        if max_output_tokens < 1:
            raise ValueError(f"max_output_tokens must be at least 1, got {max_output_tokens}")
        allowance = self._context_tokens - max_output_tokens
        if allowance < 1:
            raise ValueError(
                f"max_output_tokens ({max_output_tokens}) leaves no room in a "
                f"{self._context_tokens}-token context window"
            )
        estimate = self.estimate_prompt_tokens(messages)
        if estimate > allowance:
            raise PromptTooLong(
                f"prompt is about {estimate} tokens and at most {allowance} fit "
                f"({self._context_tokens}-token window, {max_output_tokens} reserved for the "
                f"answer). Refused before sending: this server truncates a long prompt "
                f"silently — same prompt_tokens, same finish_reason, no error — so a call "
                f"made here would answer from a prefix and read exactly like a good answer. "
                f"Send less context, or raise {ENV_CONTEXT_TOKENS} — but only to a window "
                f"the server really serves (for ollama that is num_ctx / "
                f"OLLAMA_CONTEXT_LENGTH, not the model card)"
            )
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "max_tokens": max_output_tokens,
            "temperature": TEMPERATURE,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "nodum", "schema": schema},
            }
        started = time.monotonic()
        body = self._post(payload, timeout)
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._completion(body, latency_ms, estimate)

    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST the payload and return the parsed body, or raise."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as failure:
            # Measured: an unknown model on a live server is an HTTP 404
            # carrying a JSON error body, in about a millisecond. The body is
            # the only thing that says *which* misconfiguration it was, so a
            # slice of it goes in the message.
            detail = failure.read().decode("utf-8", "replace")[:200]
            raise ProviderUnavailable(
                f"provider at {self._base_url} answered HTTP {failure.code}: {detail}"
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

    def _completion(self, body: dict[str, Any], latency_ms: int, estimate: int) -> Completion:
        """Read the wire shape into a :class:`Completion`, or say what was missing.

        Every field is read defensively: a provider that answers 200 with a
        shape this does not recognise is unavailable, not a completion with
        zeros in it. A silently zeroed ``usage`` would be a call that cost
        nothing according to the budget, which is the one lie a meter may not
        tell.
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
        )


# ── Provider resolution (cached process-wide) ────────────────────────────────
#
# The same three functions plus a reset that `nodum.embeddings` exposes, for the
# reason in the module docstring: one way to be offline.

_provider: LLMProvider | None = None
_unavailable_reason: str | None = None
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
    global _provider, _unavailable_reason, _resolved
    if not _resolved:
        _provider, _unavailable_reason = _resolve_default()
        _resolved = True
    return _provider


def unavailable_reason() -> str | None:
    """Return why no provider is available (``None`` when one is)."""
    get_provider()
    return _unavailable_reason


def set_provider(provider: LLMProvider | None, *, reason: str | None = None) -> None:
    """Force the provider — the test and configuration seam.

    Passing ``None`` forces the unavailable state; ``reason`` is what
    :func:`unavailable_reason` reports. No test may assert on real model output
    (temperature-0 determinism is a local-backend property, not a contract), so
    every test that needs a completion injects one here.
    """
    global _provider, _unavailable_reason, _resolved
    _provider = provider
    _unavailable_reason = None if provider is not None else (reason or "no LLM provider configured")
    _resolved = True


def reset_provider() -> None:
    """Drop the cached resolution; the next use re-resolves from scratch."""
    global _provider, _unavailable_reason, _resolved
    _provider = None
    _unavailable_reason = None
    _resolved = False


def _resolve_default() -> tuple[LLMProvider | None, str | None]:
    """Build the configured provider, or explain why there is none."""
    model = (os.environ.get(ENV_MODEL) or "").strip()
    if not model:
        return None, (
            f"no LLM provider configured (set {ENV_MODEL} to a model the endpoint serves; "
            f"{ENV_BASE_URL} defaults to {DEFAULT_BASE_URL})"
        )
    base_url = (os.environ.get(ENV_BASE_URL) or "").strip() or DEFAULT_BASE_URL
    api_key = (os.environ.get(ENV_API_KEY) or "").strip() or None
    raw_context = (os.environ.get(ENV_CONTEXT_TOKENS) or "").strip()
    context_tokens = DEFAULT_CONTEXT_TOKENS
    if raw_context:
        try:
            context_tokens = int(raw_context)
        except ValueError:
            return None, (f"{ENV_CONTEXT_TOKENS}={raw_context!r} is not a whole number of tokens")
    try:
        provider = OpenAICompatProvider(
            base_url=base_url, model=model, api_key=api_key, context_tokens=context_tokens
        )
    except ValueError as failure:
        return None, f"LLM provider configuration is unusable: {failure}"
    return provider, None
