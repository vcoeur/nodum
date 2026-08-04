"""Shared test helpers for the Q13 principal/grant model.

Write functions in the service layer take a :class:`Principal`, not an actor
string. These builders mint them the way tests need: ``owner()`` for the
seeded first human, ``agent()`` to idempotently seed an agent account plus
grants and load its principal. Attribution assertions keep working unchanged
because agent ids are the old actor names (``agent:x`` means ``agents.id``
``x``).

The file also holds the import-spelling extractor the structural rails use
(:func:`nodum_imports`), shared between the LLM rail and the identity rail so
the two cannot disagree about what reaches a module.
"""

from __future__ import annotations

import ast
import math

from nodum import auth, db, embeddings
from nodum.principal import Principal

#: The seeded owner's actor string (assertions on created_by / events.actor).
OWNER_ACTOR = auth.OWNER_ACTOR


def owner() -> Principal:
    """The owner human principal (requires a migrated database)."""
    return auth.owner_principal()


def agent(
    name: str = "test-agent",
    *,
    grants: dict[str, str] | None = None,
    kind: str = "external",
    token: str | None = None,
) -> Principal:
    """Seed (idempotently) an agent account plus grants; return its Principal.

    Accepts the id with or without the ``agent:`` prefix. Default grants are
    the migration's parity set: read meta, suggest main. With ``token``, the
    account's credential hash is set to that token's sha-256, so the MCP
    path can verify it.
    """
    import hashlib

    agent_id = name.removeprefix("agent:")
    grants = {"meta": "read", "main": "suggest"} if grants is None else grants
    conn = db.connect()
    db.init_db(conn)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, kind, name, owner_human_id)"
            " VALUES (?, ?, ?, 'owner')",
            (agent_id, kind, agent_id),
        )
        if token is not None:
            conn.execute(
                "UPDATE agents SET credential_hash = ? WHERE id = ?",
                (hashlib.sha256(token.encode()).hexdigest(), agent_id),
            )
        for space_id, level in grants.items():
            conn.execute(
                "INSERT OR REPLACE INTO grants (agent_id, space_id, level) VALUES (?, ?, ?)",
                (agent_id, space_id, level),
            )
        conn.commit()
    finally:
        conn.close()
    return auth.agent_principal(agent_id)


def seed_space(space_id: str, *, title: str | None = None) -> str:
    """Seed (idempotently) a space node in meta; return its id."""

    conn = db.connect()
    db.init_db(conn)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (id, space_id, type_id, title, props, state, created_by)"
            " VALUES (?, 'meta', 'space', ?, '{}', 'active', 'human:owner')",
            (space_id, title or space_id),
        )
        conn.commit()
    finally:
        conn.close()
    return space_id


def nodum_imports(source: str) -> set[str]:
    """Every ``nodum.*`` module this source can reach *directly*, however spelled.

    The spellings, all of which reach the same module and all of which a
    refactor might reach for:

    ``import nodum.llm`` / ``import nodum.llm as anything``
        A plain import, aliased or not — the alias is irrelevant, the module is
        what matters.
    ``from nodum import llm``
        Names the submodule as an attribute of the package.
    ``from nodum.llm import chat``
        Names it as the module being imported from.
    ``from . import llm`` / ``from .llm import chat``
        The relative spellings. This package is flat, so one level is all there
        is, and a relative import is exactly as reaching as an absolute one.
    ``importlib.import_module("nodum.llm")`` / ``__import__("nodum.llm")``
        The dynamic spellings, which no AST walk over *imports* would see —
        which is precisely why a rail that only read ``ast.Import`` would be a
        rail with a documented way around it. **Positionally or by keyword**:
        both take ``name``, and a walk over ``node.args`` alone was blind to
        ``import_module(name="nodum.llm")`` — a constant string this claims to
        catch, spelled the way an IDE's signature help suggests it.
    ``nodum.llm.something`` after a bare ``import nodum``
        An attribute chain. It only resolves if something else has already
        imported the submodule, so it is not on its own a working import — but
        it is a *reach*, and the rail is about reaching.
    """
    tree = ast.parse(source)
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "nodum" or alias.name.startswith("nodum."):
                    reached.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Flat package: any level resolves to `nodum`.
                base = "nodum" if node.module is None else f"nodum.{node.module}"
            elif node.module == "nodum" or (node.module or "").startswith("nodum."):
                base = node.module or ""
            else:
                continue
            reached.add(base)
            if base == "nodum":
                reached |= {f"nodum.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            if name in {"import_module", "__import__"}:
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                for argument in arguments:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and (argument.value == "nodum" or argument.value.startswith("nodum."))
                    ):
                        reached.add(argument.value)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "nodum"
        ):
            reached.add(f"nodum.{node.attr}")
    return reached


def _unit(entries: dict[int, float]) -> list[float]:
    """A 384-dimensional unit vector with only the named dimensions non-zero.

    The compact way to hand-write a vector for :class:`ReplayEmbedder`: the
    entries are a sparse geometry (dimension → value), everything else is 0,
    and the result is L2-normalised so cosines are meaningful.
    """
    vector = [0.0] * embeddings.EMBEDDING_DIMS
    for index, value in entries.items():
        vector[index] = value
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


#: The sentences :class:`ReplayEmbedder` is keyed by, exported so the tests
#: seed the exact strings the frozen table maps — one source for both.
#: The query and the three paraphrases of "birds migrating" share the same
#: vector neighbourhood; each decoy carries exactly one of the query's words
#: (the lexical trap) and lives far from it.
REPLAY_QUERY = "migration routes"
REPLAY_NEAREST = "Warmth-seeking flocks leave northern lands each autumn"
REPLAY_MIDDLE = "Many birds fly to milder regions as cold weather arrives"
REPLAY_FARTHEST = "Feathered travelers head toward balmy places when frost comes"
REPLAY_DECOY_MIGRATION = "migration patterns of urban wildlife"
REPLAY_DECOY_ROUTES = "coastal shipping routes through the strait"


class ReplayEmbedder:
    """Deterministic fake embedding provider fed a frozen table of vectors.

    The real provider's differentiator is semantic similarity — and that is
    exactly what a bag-of-words fake cannot express: its cosine *is* token
    overlap, so it ranks by the same signal BM25 uses and a test can never
    show the vector arm recalling a paraphrase that shares no word with the
    query. This one answers from a frozen snapshot instead: sentence →
    vector, with the geometry chosen by hand (see :data:`vectors`), so the
    test controls the cosines directly — the paraphrases of a concept sit
    near each other, a lexical decoy carrying the query's own words sits far
    away, and the query embeds to its own row.

    A text not in the table embeds to the zero vector: at cosine 0.0 from
    everything it is below the similarity floor, so an unkeyed text can
    never win a search it was not given a vector for.
    """

    model_id = "test-replay-embedder"
    dimensions = embeddings.EMBEDDING_DIMS

    #: The frozen vectors, built once at import from the sparse geometry:
    #: query and paraphrases on shared dimensions (cosines ≈ 1.0 / 0.76 /
    #: 0.68, all above the search floor), decoys on their own (cosine 0.0,
    #: below the floor).
    vectors: dict[str, list[float]] = {
        REPLAY_QUERY: _unit({10: 4.0, 20: 3.0, 30: 2.0}),
        REPLAY_NEAREST: _unit({10: 3.5, 20: 3.0, 30: 2.0}),
        REPLAY_MIDDLE: _unit({10: 2.0, 20: 3.5, 40: 2.0}),
        REPLAY_FARTHEST: _unit({10: 3.0, 20: 1.5, 60: 3.0}),
        REPLAY_DECOY_MIGRATION: _unit({70: 5.0}),
        REPLAY_DECOY_ROUTES: _unit({80: 5.0}),
    }

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text with its frozen vector; an unknown text is a zero vector."""
        zero = [0.0] * self.dimensions
        return [self.vectors.get(text, zero) for text in texts]
