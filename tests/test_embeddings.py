"""Chunking, the provider interface, and the resolution seam (nodum.embeddings)."""

from __future__ import annotations

import os

import pytest

from nodum import embeddings


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
