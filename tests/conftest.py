"""Shared fixtures: every test runs against a fresh, migrated SQLite file."""

from __future__ import annotations

import hashlib
import math

import pytest

from nodum import db, embeddings, service


@pytest.fixture(autouse=True)
def _never_the_real_database(tmp_path_factory, monkeypatch):
    """Make the default database path unreachable for the whole test session.

    ``fresh_db`` points ``NODUM_DB`` at a temp file, but any test that unsets
    that variable mid-run — ``monkeypatch.undo()`` undoes *every* patch the
    fixture made, ``NODUM_DB`` included — falls back to
    :data:`nodum.db.DEFAULT_DB_PATH` and starts working against the developer's
    own graph. That is not hypothetical: it applied an unreleased migration to
    a live ``~/.local/share/nodum/nodum.db`` during this phase's build.

    Redirecting the constant is the structural fix rather than the local one:
    a future test cannot reintroduce the leak by patching the environment
    carelessly, because there is no longer a real path behind the fallback.
    Asserting on the constant's *value* would be the weaker move — this
    removes the reachable state instead (see the repo's own
    "structural tests don't hold invariants" finding).
    """
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path_factory.mktemp("default-db") / "nodum.db")


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
