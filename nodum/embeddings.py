"""Embedding providers and chunking for the vector (sqlite-vec) signal.

Design D10: the provider sits behind a small interface — ``model_id``,
``dimensions``, and ``embed(texts) -> vectors`` — so an API-key provider can
slot in later without touching the projector or search layers. The default
is a local, in-process fastembed model (ONNX Runtime on CPU; no daemon, no
API key), served from nodum's own model cache — see the cache rule below.

Design D6: node text is embedded in fixed-window chunks (512 tokens, ~15%
overlap — approximated as whitespace-separated words, see :func:`chunk_text`),
every chunk records the producing ``model_id``, and search reads only the
chunks carrying the *active* provider's id (finding M13: a different model's
chunks live in a different vector space, so they are excluded from the KNN
join). A model change is therefore a full ``projector rebuild vec`` — the
projector is derived state, so replaying the event log re-embeds everything
with the new model.

**The model cache is nodum's, and it lives beside the database.**
:data:`DEFAULT_CACHE_PATH` is ``~/.local/share/nodum/models``, overridable with
``NODUM_EMBED_CACHE``, and it is passed to fastembed explicitly. Leaving it out
is not a neutral default: fastembed falls back to
``<tempdir>/fastembed_cache`` (``fastembed.common.utils.define_cache_dir``),
and a system that clears ``/tmp`` on boot — the ``D`` type in
``systemd-tmpfiles``, which is the shipped default on many distributions —
deletes the whole 241 MB download. The next run then finds no model, resolves
no provider, and the vector signal drops out of search *and* consolidation with
no error anywhere: results simply get quieter and worse. And because a download
needs the explicit ``NODUM_EMBED_DOWNLOAD=1`` gate, nothing re-fetches it — it
stays degraded until a human notices. The cache belongs with the user's data
for exactly the reason the database does.

Graceful degradation: when no provider is usable (fastembed not installed,
model not in nodum's cache) the ``vec`` projector reports itself unavailable
and search falls back to BM25 + graph expansion — nothing crashes. A model is
never downloaded implicitly: set ``NODUM_EMBED_DOWNLOAD=1`` for the first run
to fetch it, afterwards it is served from the cache. :func:`unavailable_reason`
distinguishes a cache that was never populated from one that *had* a model and
no longer does, because those need different things from the human.
"""

from __future__ import annotations

import math
import os
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
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
#: provider only resolves when the model is already in nodum's model cache).
ENV_DOWNLOAD_VAR = "NODUM_EMBED_DOWNLOAD"

#: Environment override for where model files are cached.
ENV_CACHE_VAR = "NODUM_EMBED_CACHE"

#: Default model cache when ``NODUM_EMBED_CACHE`` is not set.
#:
#: Beside the database (:data:`nodum.db.DEFAULT_DB_PATH`) and resolved the same
#: way, deliberately: a 241 MB download the user is gated into fetching by hand
#: is their data, and it belongs wherever their graph does. ``~/.local/share``
#: is spelled out rather than read from ``XDG_DATA_HOME`` because ``db.py``
#: spells it out too — honouring the variable here alone would put the graph and
#: the model that indexes it under different roots on any machine that sets it.
#: Both should move together, or neither.
DEFAULT_CACHE_PATH = Path("~/.local/share/nodum/models").expanduser()


def cache_path() -> Path:
    """Return the configured model cache directory (``NODUM_EMBED_CACHE`` or the default).

    Mirrors :func:`nodum.db.db_path` exactly, because it answers the same
    question about the same kind of user data.
    """
    raw = os.environ.get(ENV_CACHE_VAR)
    return Path(raw).expanduser() if raw else DEFAULT_CACHE_PATH


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

    Construction loads the model (from nodum's model cache, or downloading it
    into that cache when the caller allowed that), so a missing model fails
    here rather than mid-projector-run.

    ``cache_dir`` is always passed to fastembed, never left to default. See the
    module docstring for what the default costs: fastembed would put the
    download under the system temp directory, where a reboot deletes it and the
    vector signal disappears without an error.
    """

    def __init__(
        self, model_name: str = DEFAULT_MODEL, *, cache_dir: str | Path | None = None
    ) -> None:
        from fastembed import TextEmbedding  # pyright: ignore[reportMissingImports] degraded-mode

        self._cache_dir = Path(cache_dir).expanduser() if cache_dir is not None else cache_path()
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(self._cache_dir))
        self._model_id = model_name
        # Probe one embedding: verifies the model actually runs and pins the
        # dimensionality without trusting registry metadata.
        self._dimensions = len(self.embed(["nodum dimension probe"])[0])

    @property
    def cache_dir(self) -> Path:
        """Where this provider's model files are cached."""
        return self._cache_dir

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


def node_chunks(node: dict[str, Any]) -> list[str]:
    """The chunk texts a node is embedded as — the single definition of that.

    Both consumers start here: the ``vec`` projector stores one vector per
    chunk (search retrieves the *best chunk*, so it needs them separately),
    and :func:`node_vectors` reduces the same chunks to the one vector a
    pairwise comparison needs. Spelling the chunking once is what keeps a
    node from having two different vectors depending on which subsystem asks.
    """
    return chunk_text(node_text(node))


def node_vectors(provider: EmbeddingProvider, nodes: list[dict[str, Any]]) -> list[list[float]]:
    """One comparable vector per node: the mean of its chunk vectors.

    **Why the mean, and why a node-level vector exists at all.** Search wants
    chunk granularity — a query should match the paragraph that answers it —
    so the projector keeps chunks apart and takes the closest one. Anything
    that compares two *nodes* (duplicate detection, ``relates_to`` inference,
    and the clustering the abstraction job will do) needs a single vector per
    node instead, and the only honest way to get one is to reduce the chunks
    the projector already holds. This function is that reduction, so the
    node-level vector is a pure function of the projector's rows rather than a
    second, independently produced embedding.

    Mean rather than the alternatives, in order of how badly they fail:

    * *First chunk* is the defect this replaced wearing a different hat — a
      long node would be represented by its opening window, so clustering
      would group documents by their first page.
    * *Max-pooling* per dimension lands outside the region the model was
      trained to put text in: the vector it returns is not the embedding of
      anything, so a cosine against it means nothing.
    * *Mean* is the model's own pooling extended by one level. The default
      model already averages over the tokens of a window (fastembed applies
      mean pooling for it), so averaging over windows is the same operation at
      the next scale, and every part of a long node contributes.

    The mean is unweighted, which slightly over-weights a short trailing
    chunk; that is a rounding error next to dropping the tail entirely. For a
    node that fits one window — nearly all of them at :data:`CHUNK_WORDS` —
    the reduction is the identity up to scale, since :func:`_pool`
    L2-normalises what it returns; cosine is scale-free, so thresholds
    calibrated on ordinary notes stay valid.

    Args:
        provider: The provider to embed with (its ``dimensions`` sizes the
            empty-node vector).
        nodes: Node dicts carrying ``title`` and ``content``.

    Returns:
        One L2-normalised vector per input node, in input order. A node with
        no text at all yields the zero vector, which is at cosine 0.0 from
        everything — two empty nodes are not each other's duplicates.
    """
    chunked = [node_chunks(node) for node in nodes]
    flat = [chunk for chunks in chunked for chunk in chunks]
    embedded = iter(provider.embed(flat)) if flat else iter(())
    return [_pool([next(embedded) for _ in chunks], provider.dimensions) for chunks in chunked]


def _pool(vectors: list[list[float]], dimensions: int) -> list[float]:
    """L2-normalised mean of chunk vectors; the zero vector when there are none."""
    if not vectors:
        return [0.0] * dimensions
    count = len(vectors)
    summed = [sum(values) / count for values in zip(*vectors, strict=True)]
    norm = math.sqrt(sum(value * value for value in summed))
    if not norm:
        return summed
    return [value / norm for value in summed]


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
        import fastembed  # noqa: F401  # pyright: ignore[reportMissingImports] degraded-mode
    except ImportError:
        return None, "fastembed is not installed (install the 'embeddings' extra)"
    model_name = os.environ.get(ENV_MODEL_VAR, DEFAULT_MODEL)
    cache = cache_path()
    # Sampled *before* the attempt on purpose: constructing the provider calls
    # fastembed's define_cache_dir, which creates the directory as a side
    # effect. Read afterwards, every cache would look like it had just been
    # created and the two cases below would collapse into one.
    was_populated = _cache_is_populated(cache)
    try:
        if os.environ.get(ENV_DOWNLOAD_VAR) == "1":
            provider = FastembedProvider(model_name, cache_dir=cache)
        else:
            # Default posture: never download implicitly — only an already
            # cached model resolves. HF_HUB_OFFLINE makes huggingface_hub
            # raise instead of fetching when the model is not cached.
            # fastembed's own logging/warnings are silenced for this probe:
            # the failure is expected there and surfaces via the reason.
            with _hf_offline(), _silence_fastembed():
                provider = FastembedProvider(model_name, cache_dir=cache)
    except Exception as exc:
        if was_populated:
            # The cache held something and the model still would not load. That
            # is a different problem from "not fetched yet", and it needs a
            # different response: something removed or truncated files nodum
            # had already downloaded, and if it happens repeatedly the cache is
            # on storage that does not survive a reboot.
            return None, (
                f"embedding model {model_name!r} would not load from the model cache at "
                f"{cache} ({exc}); the cache is not empty, so files that were downloaded "
                f"are now missing or incomplete — check that {ENV_CACHE_VAR} does not point "
                f"at temporary storage, then rerun with {ENV_DOWNLOAD_VAR}=1 to re-fetch it"
            )
        return None, (
            f"embedding model {model_name!r} has not been downloaded yet (model cache "
            f"{cache} is empty: {exc}); rerun with {ENV_DOWNLOAD_VAR}=1 once to fetch it"
        )
    if provider.dimensions != EMBEDDING_DIMS:
        return None, (
            f"model {model_name!r} has {provider.dimensions} dimensions but the node_vec "
            f"table holds {EMBEDDING_DIMS}; a dimension change needs a new migration"
        )
    return provider, None


def _cache_is_populated(cache: Path) -> bool:
    """Whether the model cache holds anything at all (never raises).

    An unreadable or absent cache counts as empty: this only chooses which
    sentence explains an unavailable provider, so it must not become a second
    way for resolution to fail.
    """
    try:
        return cache.is_dir() and any(cache.iterdir())
    except OSError:
        return False


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
            from loguru import logger  # pyright: ignore[reportMissingImports] degraded-mode

            logger.disable("fastembed")
            try:
                yield
            finally:
                logger.enable("fastembed")
        except ImportError:  # loguru ships with fastembed; no-op without it
            yield
