"""Shared fixtures: every test runs against a fresh, migrated SQLite file."""

from __future__ import annotations

import hashlib
import math

import pytest

from nodum import embeddings, service


class HashEmbedder:
    """Deterministic fake embedding provider for tests — no model download.

    A signed hashing bag-of-words embedder: each token votes for one
    dimension (sha256-derived) with a random sign, and the vector is
    L2-normalized. Texts sharing vocabulary land close together, which is
    all the vector-signal tests need — and it never touches the network.
    """

    model_id = "test-hash-embedder"
    dimensions = embeddings.EMBEDDING_DIMS

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text as a normalized hashing bag-of-words vector."""
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode()).digest()
                index = int.from_bytes(digest[:4]) % self.dimensions
                vector[index] += 1.0 if digest[4] % 2 else -1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point NODUM_DB at a fresh temp file and run migrations; returns the path."""
    path = tmp_path / "graph.db"
    monkeypatch.setenv("NODUM_DB", str(path))
    service.init()
    return path


@pytest.fixture(autouse=True)
def _no_embedding_provider():
    """Force the embedding provider unavailable unless a test opts in.

    Tests must never resolve the real fastembed provider (it would download
    a model): the default here is the graceful-degradation path. The
    ``fake_embedder`` fixture installs a deterministic provider instead.
    """
    embeddings.set_provider(None, reason="test default: no embedding provider")
    yield
    embeddings.reset_provider()


@pytest.fixture()
def fake_embedder():
    """Install the deterministic hashing embedder as the provider for one test."""
    provider = HashEmbedder()
    embeddings.set_provider(provider)
    return provider
