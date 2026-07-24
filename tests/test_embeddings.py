"""Chunking, the provider interface, and the resolution seam (nodum.embeddings)."""

from __future__ import annotations

import os
import sys
import types

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
        def __init__(self, model_name):
            if seen is not None:
                seen["model_name"] = model_name
                seen["hf_offline"] = os.environ.get("HF_HUB_OFFLINE")
            if error is not None:
                raise error
            self.model_id = model_name
            self.dimensions = dimensions

        def embed(self, texts):
            return [[0.0] * dimensions for _ in texts]

    return Stub


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


# ── Default resolution: every way it can decline to produce a provider ───────


def test_missing_fastembed_names_the_extra_to_install(monkeypatch):
    """Without the extra there is no provider — and the reason says what to do."""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # makes `import` raise
    embeddings.reset_provider()

    assert embeddings.get_provider() is None
    assert "fastembed is not installed" in embeddings.unavailable_reason()
    assert "embeddings" in embeddings.unavailable_reason()


def test_uncached_model_is_a_clean_unavailable_state(monkeypatch, stub_fastembed):
    """A cache miss under the offline default explains itself; it never raises.

    This is the state CI and every fresh machine start in, so it has to be an
    ordinary answer from `get_provider`, not an exception escaping into a
    projector run.
    """
    seen: dict = {}
    monkeypatch.setattr(
        embeddings,
        "FastembedProvider",
        _provider_stub(error=ValueError("model not found in the local cache"), seen=seen),
    )
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")  # a pre-existing value to restore
    monkeypatch.delenv(embeddings.ENV_DOWNLOAD_VAR, raising=False)
    embeddings.reset_provider()

    assert embeddings.get_provider() is None
    reason = embeddings.unavailable_reason()
    assert "not usable from the local cache" in reason
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
        def __init__(self, model_name):
            constructions.append(model_name)
            super().__init__(model_name)

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
