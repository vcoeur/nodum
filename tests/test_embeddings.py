"""Chunking, the provider interface, and the resolution seam (nodum.embeddings)."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

from nodum import embeddings


@pytest.fixture()
def stub_fastembed(monkeypatch):
    """Make ``import fastembed`` succeed without the extra being installed.

    Resolution is a two-step gate — the import, then constructing the model —
    and the branches below all live past the first step. The autouse
    ``_no_embedding_provider`` fixture pins the cached state, so every test
    here calls :func:`embeddings.reset_provider` to force a real resolution
    and relies on its teardown to clear the result.
    """
    monkeypatch.setitem(sys.modules, "fastembed", types.ModuleType("fastembed"))


def _provider_stub(dimensions=embeddings.EMBEDDING_DIMS, error=None, seen=None):
    """Build a stand-in for FastembedProvider that never loads a model."""

    class Stub:
        def __init__(self, model_name, *, cache_dir=None):
            if seen is not None:
                seen["model_name"] = model_name
                seen["cache_dir"] = cache_dir
                seen["hf_offline"] = os.environ.get("HF_HUB_OFFLINE")
            if error is not None:
                raise error
            self.model_id = model_name
            self.dimensions = dimensions
            self.cache_dir = cache_dir

        def embed(self, texts):
            return [[0.0] * dimensions for _ in texts]

    return Stub


def _fastembed_module(recorder):
    """A fake ``fastembed`` module whose TextEmbedding records its kwargs.

    Reaches one level deeper than :func:`_provider_stub`: these tests are about
    what the *real* ``FastembedProvider`` hands to fastembed, so the provider
    itself has to run.
    """
    module = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, *, model_name, cache_dir=None, **kwargs):
            recorder["model_name"] = model_name
            recorder["cache_dir"] = cache_dir

        def embed(self, texts):
            return [_Vector([0.0] * embeddings.EMBEDDING_DIMS) for _ in texts]

    module.TextEmbedding = TextEmbedding
    return module


class _Vector(list):
    """A list that also answers ``tolist()``, the way a numpy vector does."""

    def tolist(self):
        return list(self)


def test_chunk_empty_and_short_texts():
    assert embeddings.chunk_text("") == []
    assert embeddings.chunk_text("   \n ") == []
    assert embeddings.chunk_text("one two three") == ["one two three"]


def test_chunk_exact_window_is_one_chunk():
    text = " ".join(f"w{i}" for i in range(embeddings.CHUNK_WORDS))
    assert embeddings.chunk_text(text) == [text]


def test_chunk_boundaries_and_overlap():
    words = [f"w{i}" for i in range(embeddings.CHUNK_WORDS + 100)]
    chunks = embeddings.chunk_text(" ".join(words))
    assert len(chunks) == 2
    first, second = (chunk.split() for chunk in chunks)
    assert len(first) == embeddings.CHUNK_WORDS
    # ~15% overlap: the tail of chunk n is the head of chunk n+1.
    overlap = embeddings.CHUNK_OVERLAP_WORDS
    assert first[-overlap:] == second[:overlap]
    assert second[overlap:] == words[embeddings.CHUNK_WORDS :]


def test_chunk_long_text_strides_consistently():
    words = [f"w{i}" for i in range(1200)]
    chunks = [chunk.split() for chunk in embeddings.chunk_text(" ".join(words))]
    stride = embeddings.CHUNK_WORDS - embeddings.CHUNK_OVERLAP_WORDS
    assert len(chunks) == 3
    for index, chunk in enumerate(chunks):
        assert chunk[0] == words[index * stride]
    assert chunks[-1][-1] == words[-1]


def test_node_text_combines_title_and_content():
    assert embeddings.node_text({"title": "T", "content": "body"}) == "T\n\nbody"
    assert embeddings.node_text({"title": None, "content": "body"}) == "body"
    assert embeddings.node_text({"title": "  ", "content": "body"}) == "body"


# ── One definition of a node's chunks, one reduction to a node vector ────────


def test_node_chunks_is_node_text_then_chunk_text():
    """The single definition both the projector and the cycle start from."""
    node = {"title": "T", "content": " ".join(f"w{i}" for i in range(embeddings.CHUNK_WORDS + 50))}
    assert embeddings.node_chunks(node) == embeddings.chunk_text(embeddings.node_text(node))
    assert len(embeddings.node_chunks(node)) == 2


def test_a_node_vector_is_the_mean_of_its_chunk_vectors(fake_embedder):
    """The reduction is a pure function of the chunks the projector stores.

    Not "close to" the chunk vectors — computed *from* them, so the vector the
    consolidation cycle compares can be recovered from `node_vec` rows alone.
    """
    node = {
        "title": "Long",
        "content": " ".join(f"w{i}" for i in range(3 * embeddings.CHUNK_WORDS)),
    }
    chunks = embeddings.node_chunks(node)
    assert len(chunks) > 1

    (actual,) = embeddings.node_vectors(fake_embedder, [node])

    chunk_vectors = fake_embedder.embed(chunks)
    expected = [sum(values) / len(chunk_vectors) for values in zip(*chunk_vectors, strict=True)]
    norm = math.sqrt(sum(value * value for value in expected))
    assert actual == pytest.approx([value / norm for value in expected])


def test_a_one_chunk_node_reduces_to_that_chunk(fake_embedder):
    """The identity case — nearly every node — so calibration stays comparable."""
    node = {"title": "Short", "content": "a handful of words"}
    assert len(embeddings.node_chunks(node)) == 1

    (actual,) = embeddings.node_vectors(fake_embedder, [node])

    (chunk_vector,) = fake_embedder.embed(embeddings.node_chunks(node))
    norm = math.sqrt(sum(value * value for value in chunk_vector))
    assert actual == pytest.approx([value / norm for value in chunk_vector])


def test_the_tail_of_a_long_node_changes_its_vector(fake_embedder):
    """The defect this replaced: a long node was embedded to its first window.

    Two nodes sharing an opening window and differing only past it must not
    land on the same vector, or clustering groups documents by first page.
    """
    opening = " ".join(f"w{i}" for i in range(embeddings.CHUNK_WORDS))
    first = {"title": None, "content": f"{opening} alpha beta gamma"}
    second = {"title": None, "content": f"{opening} delta epsilon zeta"}

    one, two = embeddings.node_vectors(fake_embedder, [first, second])

    assert one != pytest.approx(two)


def test_node_vectors_keeps_input_order_across_differing_chunk_counts(fake_embedder):
    """Chunks are embedded in one flat batch, so the regrouping has to be right."""
    short = {"title": None, "content": "tiny"}
    long_node = {
        "title": None,
        "content": " ".join(f"w{i}" for i in range(2 * embeddings.CHUNK_WORDS)),
    }

    vectors = embeddings.node_vectors(fake_embedder, [short, long_node, short])

    assert len(vectors) == 3
    assert vectors[0] == pytest.approx(vectors[2])
    assert vectors[0] != pytest.approx(vectors[1])


def test_a_node_with_no_text_is_at_cosine_zero_from_everything(fake_embedder):
    """Two empty nodes are not each other's duplicate — they are simply empty."""
    empty = {"title": None, "content": ""}
    assert embeddings.node_chunks(empty) == []

    (vector,) = embeddings.node_vectors(fake_embedder, [empty])

    assert vector == [0.0] * fake_embedder.dimensions


def test_provider_seam_reports_forced_unavailability():
    # The autouse fixture forces unavailable; the reason must surface.
    assert embeddings.get_provider() is None
    assert embeddings.unavailable_reason() is not None


def test_hash_embedder_is_deterministic_and_normalized(fake_embedder):
    first = fake_embedder.embed(["alpha beta", "alpha beta"])
    second = fake_embedder.embed(["alpha beta", "alpha beta"])
    assert first == second
    assert len(first[0]) == embeddings.EMBEDDING_DIMS
    norm = sum(value * value for value in first[0]) ** 0.5
    assert norm == pytest.approx(1.0)
    # Shared vocabulary pulls vectors together; disjoint texts stay apart.
    (shared_a, shared_b), (unrelated,) = (
        fake_embedder.embed(["photosynthesis sunlight energy", "photosynthesis sunlight"]),
        fake_embedder.embed(["quantum entanglement qubit"]),
    )

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(shared_a, shared_b) > cosine(shared_a, unrelated)


# ── The model cache lives with the user's data, never in a temp dir ──────────


def test_the_default_cache_sits_beside_the_database():
    """Model files are user data, so they belong where the graph does.

    Spelled literally rather than derived from ``db.DEFAULT_DB_PATH``: the
    autouse ``_never_the_real_database`` fixture redirects that constant to a
    temp path for the whole session, so deriving from it here would compare two
    temp paths and assert nothing about where the model actually lands.
    """
    assert Path("~/.local/share/nodum/models").expanduser() == embeddings.DEFAULT_CACHE_PATH
    assert embeddings.DEFAULT_CACHE_PATH.is_absolute()
    assert not embeddings.DEFAULT_CACHE_PATH.is_relative_to(Path(tempfile.gettempdir()))


def test_the_cache_path_is_overridable_and_expands_a_tilde(monkeypatch):
    monkeypatch.setenv(embeddings.ENV_CACHE_VAR, "~/somewhere/else")
    assert embeddings.cache_path() == Path("~/somewhere/else").expanduser()

    monkeypatch.delenv(embeddings.ENV_CACHE_VAR)
    assert embeddings.cache_path() == embeddings.DEFAULT_CACHE_PATH


def test_the_provider_always_hands_fastembed_an_explicit_cache_dir(monkeypatch):
    """The regression guard: fastembed's own default is a temp directory.

    `define_cache_dir` falls back to `<tempdir>/fastembed_cache`, and a system
    that clears /tmp on boot deletes the whole download — after which the
    vector signal disappears from search and consolidation with no error at
    all. Dropping the `cache_dir=` argument would restore exactly that, so what
    is asserted is the argument reaching fastembed, not merely that nodum can
    compute a path.
    """
    recorder: dict = {}
    monkeypatch.setitem(sys.modules, "fastembed", _fastembed_module(recorder))

    embeddings.FastembedProvider()

    assert recorder["cache_dir"] is not None, "fastembed was left to pick its own cache"
    resolved = Path(recorder["cache_dir"])
    assert resolved.is_absolute()
    assert resolved == embeddings.DEFAULT_CACHE_PATH
    # The heart of it: not under the system temp directory, wherever that is.
    assert not resolved.is_relative_to(Path(tempfile.gettempdir()))


def test_the_cache_override_reaches_fastembed(monkeypatch, tmp_path):
    """`NODUM_EMBED_CACHE` is the supported way to move the download."""
    recorder: dict = {}
    monkeypatch.setitem(sys.modules, "fastembed", _fastembed_module(recorder))
    monkeypatch.setenv(embeddings.ENV_CACHE_VAR, str(tmp_path / "models"))

    provider = embeddings.FastembedProvider()

    assert Path(recorder["cache_dir"]) == tmp_path / "models"
    assert provider.cache_dir == tmp_path / "models"


def test_resolution_passes_the_configured_cache_through(monkeypatch, tmp_path, stub_fastembed):
    """The resolver uses the same cache the provider would, not fastembed's default."""
    seen: dict = {}
    monkeypatch.setattr(embeddings, "FastembedProvider", _provider_stub(seen=seen))
    monkeypatch.setenv(embeddings.ENV_CACHE_VAR, str(tmp_path / "models"))
    monkeypatch.setenv(embeddings.ENV_DOWNLOAD_VAR, "1")
    embeddings.reset_provider()

    assert embeddings.get_provider() is not None
    assert seen["cache_dir"] == tmp_path / "models"


# ── An unavailable provider says which kind of unavailable it is ─────────────


def test_an_empty_cache_reads_as_never_downloaded(monkeypatch, tmp_path, stub_fastembed):
    """Nothing has been fetched yet — the fix is the download flag."""
    monkeypatch.setattr(
        embeddings, "FastembedProvider", _provider_stub(error=ValueError("no such file"))
    )
    monkeypatch.setenv(embeddings.ENV_CACHE_VAR, str(tmp_path / "empty"))
    monkeypatch.delenv(embeddings.ENV_DOWNLOAD_VAR, raising=False)
    embeddings.reset_provider()

    assert embeddings.get_provider() is None
    reason = embeddings.unavailable_reason()
    assert "has not been downloaded yet" in reason
    assert embeddings.ENV_DOWNLOAD_VAR in reason


def test_a_populated_cache_that_will_not_load_reads_as_vanished(
    monkeypatch, tmp_path, stub_fastembed
):
    """Files were downloaded and are now gone or truncated — a different problem.

    This is the shape a cleared /tmp leaves behind, and the reason has to say
    so: the human's next move is to find out what deleted it, not to assume
    they never ran the download.
    """
    cache = tmp_path / "models"
    (cache / "models--qdrant--something").mkdir(parents=True)
    monkeypatch.setattr(
        embeddings, "FastembedProvider", _provider_stub(error=ValueError("no such file"))
    )
    monkeypatch.setenv(embeddings.ENV_CACHE_VAR, str(cache))
    monkeypatch.delenv(embeddings.ENV_DOWNLOAD_VAR, raising=False)
    embeddings.reset_provider()

    assert embeddings.get_provider() is None
    reason = embeddings.unavailable_reason()
    assert "missing or incomplete" in reason
    assert "temporary storage" in reason
    assert str(cache) in reason  # the path nobody thought to look at


def test_the_cache_state_probe_never_raises(tmp_path):
    """It only picks a sentence; it must not become a second way to fail."""
    assert embeddings._cache_is_populated(tmp_path / "absent") is False
    assert embeddings._cache_is_populated(tmp_path) is False
    (tmp_path / "something").mkdir()
    assert embeddings._cache_is_populated(tmp_path) is True
    # A file where a directory was expected is not a populated cache.
    plain = tmp_path / "plain"
    plain.write_text("")
    assert embeddings._cache_is_populated(plain) is False


# ── Default resolution: every way it can decline to produce a provider ───────


def test_missing_fastembed_names_the_extra_to_install(monkeypatch):
    """Without the extra there is no provider — and the reason says what to do."""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # makes `import` raise
    embeddings.reset_provider()

    assert embeddings.get_provider() is None
    assert "fastembed is not installed" in embeddings.unavailable_reason()
    assert "embeddings" in embeddings.unavailable_reason()


def test_uncached_model_is_a_clean_unavailable_state(monkeypatch, tmp_path, stub_fastembed):
    """A cache miss under the offline default explains itself; it never raises.

    This is the state CI and every fresh machine start in, so it has to be an
    ordinary answer from `get_provider`, not an exception escaping into a
    projector run. The cache is pointed at a temp path so the assertion does
    not depend on whether this developer happens to have downloaded the model.
    """
    seen: dict = {}
    monkeypatch.setattr(
        embeddings,
        "FastembedProvider",
        _provider_stub(error=ValueError("model not found in the cache"), seen=seen),
    )
    monkeypatch.setenv(embeddings.ENV_CACHE_VAR, str(tmp_path / "models"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")  # a pre-existing value to restore
    monkeypatch.delenv(embeddings.ENV_DOWNLOAD_VAR, raising=False)
    embeddings.reset_provider()

    assert embeddings.get_provider() is None
    reason = embeddings.unavailable_reason()
    assert "has not been downloaded yet" in reason
    assert embeddings.ENV_DOWNLOAD_VAR in reason  # tells the caller how to fix it
    # The probe really ran offline, on the default model…
    assert seen["hf_offline"] == "1"
    assert seen["model_name"] == embeddings.DEFAULT_MODEL
    # …and the caller's own environment came back unchanged.
    assert os.environ["HF_HUB_OFFLINE"] == "0"


def test_a_cached_model_resolves_and_the_probe_leaves_no_trace(monkeypatch, stub_fastembed):
    """The happy offline path: the model is cached, so no download is needed.

    `HF_HUB_OFFLINE` is scoped to the probe — unset before, unset after, so
    the setting never leaks into the rest of the process.
    """
    seen: dict = {}
    monkeypatch.setattr(embeddings, "FastembedProvider", _provider_stub(seen=seen))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv(embeddings.ENV_DOWNLOAD_VAR, raising=False)
    embeddings.reset_provider()

    provider = embeddings.get_provider()
    assert provider is not None
    assert embeddings.unavailable_reason() is None
    assert seen["hf_offline"] == "1"
    assert "HF_HUB_OFFLINE" not in os.environ


def test_download_flag_lifts_the_offline_restriction(monkeypatch, stub_fastembed):
    """`NODUM_EMBED_DOWNLOAD=1` is the only path that may reach the network."""
    seen: dict = {}
    monkeypatch.setattr(embeddings, "FastembedProvider", _provider_stub(seen=seen))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv(embeddings.ENV_DOWNLOAD_VAR, "1")
    embeddings.reset_provider()

    provider = embeddings.get_provider()
    assert provider is not None
    assert embeddings.unavailable_reason() is None
    assert seen["hf_offline"] is None  # nothing pinned it to the local cache


def test_a_model_of_the_wrong_width_is_refused(monkeypatch, stub_fastembed):
    """The guard between a `NODUM_EMBED_MODEL` override and the 384-wide vec0 table.

    Nothing downstream re-checks the width: the projector would hand 768-float
    vectors straight to `node_vec`, so this refusal is the whole defence.
    """
    monkeypatch.setattr(embeddings, "FastembedProvider", _provider_stub(dimensions=768))
    monkeypatch.setenv(embeddings.ENV_MODEL_VAR, "some/768-dim-model")
    monkeypatch.setenv(embeddings.ENV_DOWNLOAD_VAR, "1")
    embeddings.reset_provider()

    assert embeddings.get_provider() is None
    reason = embeddings.unavailable_reason()
    assert "some/768-dim-model" in reason
    assert "768 dimensions" in reason
    assert f"holds {embeddings.EMBEDDING_DIMS}" in reason
    assert "migration" in reason


def test_a_model_override_of_the_right_width_is_used(monkeypatch, stub_fastembed):
    """The override itself works — the guard rejects the width, not the env var."""
    monkeypatch.setattr(embeddings, "FastembedProvider", _provider_stub())
    monkeypatch.setenv(embeddings.ENV_MODEL_VAR, "some/384-dim-model")
    monkeypatch.setenv(embeddings.ENV_DOWNLOAD_VAR, "1")
    embeddings.reset_provider()

    provider = embeddings.get_provider()
    assert provider is not None
    assert provider.model_id == "some/384-dim-model"
    assert provider.dimensions == embeddings.EMBEDDING_DIMS


def test_resolution_is_cached_for_the_process(monkeypatch, stub_fastembed):
    """One resolution per process: availability checks stay cheap after the first."""
    constructions = []

    class CountingStub(_provider_stub()):
        def __init__(self, model_name, *, cache_dir=None):
            constructions.append(model_name)
            super().__init__(model_name, cache_dir=cache_dir)

    monkeypatch.setattr(embeddings, "FastembedProvider", CountingStub)
    monkeypatch.setenv(embeddings.ENV_DOWNLOAD_VAR, "1")
    embeddings.reset_provider()

    assert embeddings.get_provider() is embeddings.get_provider()
    embeddings.unavailable_reason()
    assert len(constructions) == 1


@pytest.mark.skipif(
    os.environ.get("NODUM_RUN_SLOW") != "1",
    reason="real-model smoke test: set NODUM_RUN_SLOW=1 (downloads the model once)",
)
def test_real_fastembed_provider_smoke(monkeypatch):
    """Opt-in smoke test against the real default model (network on first run)."""
    pytest.importorskip("fastembed")
    monkeypatch.setenv(embeddings.ENV_DOWNLOAD_VAR, "1")
    embeddings.reset_provider()
    provider = embeddings.get_provider()
    assert provider is not None
    assert provider.model_id == embeddings.DEFAULT_MODEL
    assert provider.dimensions == embeddings.EMBEDDING_DIMS
    (similar_a, similar_b, unrelated) = provider.embed(
        ["photosynthesis converts sunlight", "plants capture sunlight", "quantum qubits"]
    )

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(similar_a, similar_b) > cosine(similar_a, unrelated)
