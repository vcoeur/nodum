"""Embedding providers and chunking for the vector (sqlite-vec) signal.

Design D10: the provider sits behind a small interface — ``model_id``,
``dimensions``, and ``embed(texts) -> vectors`` — so an API-key provider can
slot in later without touching the projector or search layers. The default
is a local, in-process fastembed model (ONNX Runtime on CPU; no daemon, no
API key), downloaded from Hugging Face on first use.

Design D6: node text is embedded in fixed-window chunks (512 tokens, ~15%
overlap — approximated as whitespace-separated words, see :func:`chunk_text`),
every chunk records the producing ``model_id``, and a model change is a full
``projector rebuild vec`` (the projector is derived state, so replaying the
event log re-embeds everything with the new model).

Graceful degradation: when no provider is usable (fastembed not installed,
model not in the local cache) the ``vec`` projector reports itself
unavailable and search falls back to BM25 + graph expansion — nothing
crashes. A model is never downloaded implicitly: set ``NODUM_EMBED_DOWNLOAD=1``
for the first run to fetch it, afterwards it is served from the local cache.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

#: Default embedding model: small (~0.22 GB), multilingual, 384-dimensional,
#: Apache-2.0, and in fastembed's built-in registry (no custom registration).
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

#: Embedding dimensionality the derived ``node_vec`` table is created with
#: (migration 0006) — the default model's size. A model with different
#: dimensions needs a new migration before it can be used.
EMBEDDING_DIMS = 384

#: Fixed-window chunking (design D6): a 512-"token" window with ~15% overlap.
#: Tokens are approximated as whitespace-separated words — dependency-free
#: (no tokenizer download) and close enough for ranking at personal-KM scale.
CHUNK_WORDS = 512
CHUNK_OVERLAP_WORDS = 76  # ~15% of CHUNK_WORDS

#: Environment override for the embedding model name.
ENV_MODEL_VAR = "NODUM_EMBED_MODEL"

#: Environment flag allowing the one-time model download (otherwise the
#: provider only resolves when the model is already in the local HF cache).
ENV_DOWNLOAD_VAR = "NODUM_EMBED_DOWNLOAD"


class EmbeddingProvider(Protocol):
    """Anything that turns texts into fixed-dimension vectors (design D10)."""

    @property
    def model_id(self) -> str:
        """Stable identifier recorded per chunk (model provenance, D6)."""
        ...

    @property
    def dimensions(self) -> int:
        """Vector dimensionality — must match :data:`EMBEDDING_DIMS`."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, one vector per input, in input order."""
        ...


class FastembedProvider:
    """Local in-process embeddings via fastembed (ONNX Runtime, CPU).

    Construction loads the model (from the local Hugging Face cache, or
    downloading it when the caller allowed that), so a missing model fails
    here rather than mid-projector-run.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name)
        self._model_id = model_name
        # Probe one embedding: verifies the model actually runs and pins the
        # dimensionality without trusting registry metadata.
        self._dimensions = len(self.embed(["nodum dimension probe"])[0])

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts with the local ONNX model."""
        return [vector.tolist() for vector in self._model.embed(texts)]


def chunk_text(
    text: str, *, window: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP_WORDS
) -> list[str]:
    """Split text into fixed-window chunks of ``window`` words with ``overlap``.

    Words stand in for tokens (see module docstring). Anything up to one
    window is a single chunk; longer text strides by ``window - overlap`` so
    consecutive chunks share ``overlap`` words.
    """
    words = text.split()
    if not words:
        return []
    if len(words) <= window:
        return [" ".join(words)]
    chunks = []
    start = 0
    while start < len(words):
        end = start + window
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += window - overlap
    return chunks


def node_text(node: dict[str, Any]) -> str:
    """Return the text a node is embedded from: title (when set) then content."""
    title = (node.get("title") or "").strip()
    content = node.get("content") or ""
    return f"{title}\n\n{content}" if title else content


# ── Provider resolution (cached process-wide) ────────────────────────────────

_provider: EmbeddingProvider | None = None
_unavailable_reason: str | None = None
_resolved = False


def get_provider() -> EmbeddingProvider | None:
    """Return the configured embedding provider, or ``None`` when unavailable.

    The first call resolves the default provider (fastembed, local cache
    only unless ``NODUM_EMBED_DOWNLOAD=1``); the outcome is cached for the
    process, so availability checks are cheap after that.
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


def set_provider(provider: EmbeddingProvider | None, *, reason: str | None = None) -> None:
    """Force the provider — the test and configuration seam.

    Passing ``None`` forces the unavailable state; ``reason`` is what
    :func:`unavailable_reason` (and thereby ``projector status``) reports.
    """
    global _provider, _unavailable_reason, _resolved
    _provider = provider
    _unavailable_reason = (
        None if provider is not None else (reason or "no embedding provider configured")
    )
    _resolved = True


def reset_provider() -> None:
    """Drop the cached resolution; the next use re-resolves from scratch."""
    global _provider, _unavailable_reason, _resolved
    _provider = None
    _unavailable_reason = None
    _resolved = False


def _resolve_default() -> tuple[EmbeddingProvider | None, str | None]:
    """Build the default fastembed provider, or explain why it cannot run."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return None, "fastembed is not installed (install the 'embeddings' extra)"
    model_name = os.environ.get(ENV_MODEL_VAR, DEFAULT_MODEL)
    try:
        if os.environ.get(ENV_DOWNLOAD_VAR) == "1":
            provider = FastembedProvider(model_name)
        else:
            # Default posture: never download implicitly — only an already
            # cached model resolves. HF_HUB_OFFLINE makes huggingface_hub
            # raise instead of fetching when the model is not cached.
            # fastembed's own logging/warnings are silenced for this probe:
            # the failure is expected there and surfaces via the reason.
            with _hf_offline(), _silence_fastembed():
                provider = FastembedProvider(model_name)
    except Exception as exc:
        return None, (
            f"embedding model {model_name!r} is not usable from the local cache ({exc}); "
            f"rerun with {ENV_DOWNLOAD_VAR}=1 once to download it"
        )
    if provider.dimensions != EMBEDDING_DIMS:
        return None, (
            f"model {model_name!r} has {provider.dimensions} dimensions but the node_vec "
            f"table holds {EMBEDDING_DIMS}; a dimension change needs a new migration"
        )
    return provider, None


@contextmanager
def _hf_offline() -> Iterator[None]:
    """Restrict huggingface_hub to the local cache (no network) within the block."""
    previous = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            del os.environ["HF_HUB_OFFLINE"]
        else:
            os.environ["HF_HUB_OFFLINE"] = previous


@contextmanager
def _silence_fastembed() -> Iterator[None]:
    """Mute fastembed's loguru output and warnings inside the block.

    Used for the offline availability probe, where a load failure is an
    expected outcome reported through :func:`unavailable_reason` — not
    something to log at ERROR on every CLI invocation.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            from loguru import logger

            logger.disable("fastembed")
            try:
                yield
            finally:
                logger.enable("fastembed")
        except ImportError:  # loguru ships with fastembed; no-op without it
            yield
