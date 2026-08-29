"""The endpoints this ships knowing about, and which of them a deployment offers.

**Why this is a module and not a table inside :mod:`nodum.llm`.** Two other
modules need it and neither may import the provider: :mod:`nodum.settings`
builds one secret row per endpoint from it, and every surface that renders the
settings page reads the labels back. So the registry is a leaf — it imports
nothing from ``nodum`` — and :mod:`nodum.llm` imports *it*, keeping the
dependency arrow pointing one way.

**This is a vendor registry, and that is a deliberate reversal.** The table it
replaces (``_PROFILES``) said of itself that it was "deliberately not a vendor
registry: a profile earns its place by being an endpoint whose defaults are
wrong otherwise". That rule was right while the only consumer was
:func:`nodum.llm.profile_for`, whose job is to stop a *wrong belief* about an
endpoint an operator already named. It stops being right the moment a second
consumer appears whose job is the opposite: being the **menu** a browser may
choose an endpoint from. A menu that lists only endpoints with surprising
defaults is not a menu. So a row now earns its place by being an endpoint a
deployment might want to offer, and the fields that used to be its whole
justification are optional.

**What a browser may and may not decide.** :data:`nodum.settings.LLM_BASE_URL`
stays environment-only, and the sentence guarding it is unchanged in substance:
the set of endpoints an API key may travel to is a deployment decision. What
changes is that the *choice within that set* is now storable, because every
member of the set was compiled into this file and vetted here rather than typed
into a form. :data:`ALLOW_LIST_ENV` is how a deployment narrows the set further.

**Each endpoint owns its own key.** :func:`key_setting` names a distinct secret
per endpoint, so selecting one can only ever send the credential entered for
that one. The alternative — a single key plus a selector — is the hole
:func:`nodum.llm._resolve_default` already carries a ``key_withheld`` branch
for, and a browser-reachable selector would have re-opened it from a surface
that needs no shell access.
"""

from __future__ import annotations

import os
from typing import NamedTuple

#: Structured output enforced by the server's constrained decoding: a string the
#: schema forbids is *unrepresentable*, not merely discouraged.
STRUCTURED_JSON_SCHEMA = "json_schema"

#: Structured output as "this will be a JSON object", with the schema demoted to
#: a sentence in the prompt. The envelope is still enforced — the body parses —
#: but every *constraint inside* the schema is now advice the model may ignore.
STRUCTURED_JSON_OBJECT = "json_object"

#: No ``response_format`` at all — what a call with no schema sends.
STRUCTURED_NONE = "none"

#: What ``ollama serve`` exposes, and the endpoint a fresh install resolves to.
LOCAL_BASE_URL = "http://localhost:11434/v1"

#: The environment variable a deployment narrows the menu with: a comma-separated
#: list of labels. Unset means every shipped endpoint is offered; a name in it
#: that this build does not ship is ignored rather than fatal, so a deployment
#: pinned to an older image does not fail to boot over a label from a newer one.
#:
#: **It is environment-only and it carries no secrets** — only labels — so a
#: surface may quote it back in full, which is what makes a refusal able to say
#: *which* endpoints are on the menu instead of only that this one is not.
ALLOW_LIST_ENV = "NODUM_LLM_ENDPOINTS"

#: The prefix of the per-endpoint key settings. The label is upper-cased and its
#: hyphens become underscores, which is why :data:`_LABEL_SHAPE` forbids every
#: other character: two labels that differed only by a character folded away here
#: would silently share one credential.
KEY_PREFIX = "NODUM_LLM_KEY_"


class Endpoint(NamedTuple):
    """One endpoint this ships knowing about.

    Attributes:
        label: The stored value of ``NODUM_LLM_ENDPOINT`` and the suffix of this
            endpoint's key setting. Lowercase, hyphen-separated.
        title: What the settings page shows in the select.
        base_url: The OpenAI-compatible root this POSTs to.
        hosts: The hostnames that *are* this endpoint, compared to a parsed
            hostname and never as a substring — a proxy at ``deepseek-gw.lan``
            is not DeepSeek.
        models: The **exact** model ids that route here when no endpoint and no
            base URL is configured. Empty for every endpoint added for the
            selector: auto-routing on a model name is the behaviour that once
            sent a local install's prompts to a vendor, and it is kept only
            where it was already measured and already shipped.
        context_tokens: The window this endpoint really serves, or ``None`` when
            it serves many and nodum will not guess. See :data:`ENDPOINTS`.
        structured_mode: Which ``response_format`` to try first.
        graded_thinking: Whether ``reasoning_effort`` above ``none`` is accepted.
        takes_key: Whether this endpoint authenticates at all. False for the
            local default, which needs no credential and therefore gets no
            secret row on the settings page.
        window_note: What to tell an operator who has to set
            ``NODUM_LLM_CONTEXT_TOKENS`` themselves, or ``None`` when
            ``context_tokens`` already answers it.
    """

    label: str
    title: str
    base_url: str
    hosts: frozenset[str]
    models: frozenset[str]
    context_tokens: int | None
    structured_mode: str
    graded_thinking: bool
    takes_key: bool
    window_note: str | None


#: The endpoint DeepSeek's own hosted model ids resolve to. Named because two
#: refusal messages quote it as the worked example of a full base URL.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

#: Every endpoint this build ships, in the order the settings select lists them.
#:
#: **``context_tokens`` is ``None`` wherever the window is a property of the
#: model rather than of the endpoint**, which is the one fact this table cannot
#: tell honestly for three of its five rows. GLM's window is model-specific,
#: Kimi serves 1M on ``kimi-k3`` and 8k on ``moonshot-v1-8k``, and OpenRouter
#: fronts hundreds of models between 4k and 1M. Asserting an endpoint's
#: flagship number would re-open exactly the silent-truncation hole
#: :func:`nodum.llm.profile_for` exists to close — a 1M-token belief carried
#: against a server serving 8k. ``None`` falls back to
#: :data:`nodum.llm.DEFAULT_CONTEXT_TOKENS` and negotiates upward, which costs a
#: refusal an operator can read and fix in one field. The asymmetry is the
#: point: under-asserting is loud and recoverable, over-asserting is silent.
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        label="local",
        title="Local (ollama)",
        base_url=LOCAL_BASE_URL,
        # **Deliberately empty**, so this row is reachable only by being chosen.
        # Matching on the hostname would claim every service on `localhost` is
        # ollama and hand it ollama's beliefs — including `graded_thinking`
        # false, which is not negotiated upward, so a local gateway that does
        # accept graded reasoning would silently never be sent it. The existing
        # comparison in `nodum.llm._resolve_default` is narrower and stays: it
        # tests the whole URL, port included, against `LOCAL_BASE_URL`.
        hosts=frozenset(),
        models=frozenset(),
        # The measured window of the local default model, and the number the
        # silent-truncation refusal is computed against.
        context_tokens=4096,
        structured_mode=STRUCTURED_JSON_SCHEMA,
        # Measured: ollama answers 400 to every graded level, including on
        # `qwen3:8b`, which does think.
        graded_thinking=False,
        takes_key=False,
        window_note=None,
    ),
    Endpoint(
        label="deepseek",
        title="DeepSeek",
        base_url=DEEPSEEK_BASE_URL,
        hosts=frozenset({"api.deepseek.com"}),
        # The two ids `GET https://api.deepseek.com/models` really lists. The
        # only row that keeps model-based auto-routing: it is the behaviour that
        # already shipped and was already measured, and widening it to the rows
        # below would mean guessing which vendor a bare model name belongs to.
        models=frozenset({"deepseek-v4-flash", "deepseek-v4-pro"}),
        # Measured on `deepseek-v4-flash`: a 1 000 000-token context and a
        # 384 000-token max output, which are separate limits rather than one
        # shared window.
        context_tokens=1_000_000,
        # Measured: `json_schema` is HTTP 400 "This response_format type is
        # unavailable now"; `json_object` works.
        structured_mode=STRUCTURED_JSON_OBJECT,
        graded_thinking=True,
        takes_key=True,
        window_note=None,
    ),
    Endpoint(
        label="glm",
        title="GLM (Z.ai)",
        base_url="https://api.z.ai/api/paas/v4",
        hosts=frozenset({"api.z.ai"}),
        models=frozenset(),
        context_tokens=None,
        structured_mode=STRUCTURED_JSON_OBJECT,
        # Z.ai documents vendor-specific thinking controls, not OpenAI's
        # ``reasoning_effort`` ladder that nodum sends.
        graded_thinking=False,
        takes_key=True,
        window_note=(
            "GLM's context window is model-specific, so nodum cannot know this one — "
            "set it for your model"
        ),
    ),
    Endpoint(
        label="kimi",
        title="Kimi (Moonshot)",
        base_url="https://api.moonshot.ai/v1",
        hosts=frozenset({"api.moonshot.ai", "api.moonshot.cn"}),
        models=frozenset(),
        context_tokens=None,
        structured_mode=STRUCTURED_JSON_SCHEMA,
        graded_thinking=True,
        takes_key=True,
        window_note=(
            "Kimi's window depends on the model: kimi-k3 serves 1M, "
            "kimi-k2.6 and kimi-k2.7-code serve 256k, moonshot-v1-8k serves 8k"
        ),
    ),
    Endpoint(
        label="openrouter",
        title="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        hosts=frozenset({"openrouter.ai"}),
        models=frozenset(),
        context_tokens=None,
        structured_mode=STRUCTURED_JSON_SCHEMA,
        graded_thinking=True,
        takes_key=True,
        window_note=(
            "OpenRouter fronts hundreds of models whose windows range from 4k "
            "to 1M, so nodum cannot know this one — set it for your model"
        ),
    ),
)

_BY_LABEL = {endpoint.label: endpoint for endpoint in ENDPOINTS}


def key_setting(label: str) -> str:
    """The name of the secret setting holding this endpoint's API key.

    Args:
        label: An endpoint label.

    Returns:
        ``NODUM_LLM_KEY_<LABEL>``, upper-cased with hyphens folded to
        underscores so the result is a legal environment-variable name.
    """
    return KEY_PREFIX + label.upper().replace("-", "_")


def shipped(label: str) -> Endpoint | None:
    """The endpoint with this label, whether or not the deployment offers it.

    Args:
        label: An endpoint label; matched case-insensitively.

    Returns:
        The endpoint, or ``None`` if this build ships no such label.
    """
    return _BY_LABEL.get(label.strip().casefold())


def offered() -> tuple[Endpoint, ...]:
    """The endpoints this deployment puts on the menu, in registry order.

    Reads :data:`ALLOW_LIST_ENV` directly rather than through
    :mod:`nodum.settings`, which is what keeps this module a leaf — and is
    correct besides, because the variable is environment-only by construction:
    a browser that could edit the menu it chooses from is not a menu.

    An unset or blank variable offers everything. A label in it that this build
    does not ship is skipped rather than raised: a deployment pinned to an older
    image must not fail to boot over a label from a newer one.

    Returns:
        The offered endpoints. Never empty — a variable that names nothing this
        build ships falls back to the full registry, because a settings page
        with an empty select and no explanation is worse than one that ignored
        a variable it could not honour.
    """
    raw = (os.environ.get(ALLOW_LIST_ENV) or "").strip()
    if not raw:
        return ENDPOINTS
    wanted = {name.strip().casefold() for name in raw.split(",") if name.strip()}
    chosen = tuple(endpoint for endpoint in ENDPOINTS if endpoint.label in wanted)
    return chosen or ENDPOINTS


def offered_labels() -> tuple[str, ...]:
    """The labels :func:`offered` returns, for a refusal message or a select."""
    return tuple(endpoint.label for endpoint in offered())


def for_host(host: str) -> Endpoint | None:
    """The endpoint a parsed hostname *is*, or ``None``.

    Args:
        host: A hostname already lower-cased and stripped of its port.

    Returns:
        The matching endpoint. Compared against :attr:`Endpoint.hosts` exactly,
        never as a substring.
    """
    return next((endpoint for endpoint in ENDPOINTS if host in endpoint.hosts), None)


def for_model(model: str) -> Endpoint | None:
    """The endpoint an exact hosted model id routes to, or ``None``.

    Only :data:`ENDPOINTS` rows carrying a non-empty :attr:`Endpoint.models` can
    match, which today is DeepSeek alone. See that field for why the rows added
    for the selector deliberately do not participate.

    Args:
        model: The configured model name, already stripped.

    Returns:
        The matching endpoint, or ``None`` for a name nobody claims.
    """
    name = model.casefold()
    return next((endpoint for endpoint in ENDPOINTS if name in endpoint.models), None)
