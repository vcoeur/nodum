"""Migration tests: upgrade older databases in place to the current schema.

Two paths are covered by raw SQL: the MVP (pre-typed) graph via
:func:`migrate_mvp`, and a pre-evolvable typed graph (kinds name-only, text in
``data``) brought up to the spec-carrying, ``content``-column schema via the full
:func:`migrate` chain. Each test reshapes the shared tables, so a finalizer
rebuilds the canonical schema afterwards.
"""

from __future__ import annotations

import pytest
from psycopg.types.json import Json

from nodum.db import connect, init_schema, load_node_kind, migrate, migrate_mvp

# MVP DDL: one nodes / one edges table, no `kind` columns and no kind lookups.
MVP_DDL = """
DROP TABLE IF EXISTS edges, nodes CASCADE;
CREATE TABLE nodes (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE edges (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_uuid  UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    to_uuid    UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    data       JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Pre-evolvable typed DDL ("v2"): kind columns + name-only lookups, text in data.
V2_DDL = """
DROP TABLE IF EXISTS edges, nodes, node_kinds, edge_kinds CASCADE;
CREATE TABLE node_kinds (name TEXT PRIMARY KEY);
CREATE TABLE edge_kinds (name TEXT PRIMARY KEY);
CREATE TABLE nodes (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL REFERENCES node_kinds(name),
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (data ? 'text')
);
CREATE INDEX idx_nodes_fts ON nodes USING gin (to_tsvector('english', data ->> 'text'));
CREATE TABLE edges (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL REFERENCES edge_kinds(name),
    from_uuid  UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    to_uuid    UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    data       JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_uuid <> to_uuid)
);
"""


def _restore_typed_schema() -> None:
    """Drop the reshaped tables and rebuild the canonical schema (with seeded specs)."""
    with connect() as conn:
        conn.cursor().execute("DROP TABLE IF EXISTS edges, nodes, node_kinds, edge_kinds CASCADE")
        conn.commit()
        init_schema(conn)


def test_migrate_mvp_to_typed(request: pytest.FixtureRequest) -> None:
    """An MVP graph (type-as-node, untyped edges) migrates to typed nodes/edges."""
    # This test reshapes the shared tables; rebuild the canonical schema afterwards.
    request.addfinalizer(_restore_typed_schema)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(MVP_DDL)
        cur.execute(
            "INSERT INTO nodes (data) VALUES (%s) RETURNING uuid",
            (Json({"text": "Ada wrote a program.", "type": "claim"}),),
        )
        first = cur.fetchone()["uuid"]
        cur.execute(
            "INSERT INTO nodes (data) VALUES (%s) RETURNING uuid",
            (Json({"text": "Babbage designed an engine.", "type": "claim"}),),
        )
        second = cur.fetchone()["uuid"]
        cur.execute(
            "INSERT INTO nodes (data) VALUES (%s) RETURNING uuid",
            (Json({"text": "claim", "kind": "type"}),),
        )
        type_node = cur.fetchone()["uuid"]
        # An `is` edge to the shared type node, and a content-to-content `supports` edge.
        cur.execute(
            "INSERT INTO edges (from_uuid, to_uuid, data) VALUES (%s, %s, %s)",
            (first, type_node, Json({"type": "is"})),
        )
        cur.execute(
            "INSERT INTO edges (from_uuid, to_uuid, data) VALUES (%s, %s, %s)",
            (first, second, Json({"type": "supports"})),
        )
        conn.commit()

        migrate_mvp(conn)

        # The type-as-node is gone (its `is` edge cascaded with it).
        cur.execute("SELECT count(*) AS n FROM nodes WHERE data ->> 'kind' = 'type'")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM nodes WHERE uuid = %s", (type_node,))
        assert cur.fetchone()["n"] == 0

        # Content nodes are now typed Note rows; the old `type` moved to `role`.
        cur.execute("SELECT kind, data FROM nodes WHERE uuid = %s", (first,))
        node_row = cur.fetchone()
        assert node_row["kind"] == "Note"
        assert node_row["data"]["role"] == "claim"
        assert "type" not in node_row["data"]

        # The `supports` edge took its kind from its old payload type.
        cur.execute(
            "SELECT kind FROM edges WHERE from_uuid = %s AND to_uuid = %s",
            (first, second),
        )
        assert cur.fetchone()["kind"] == "supports"

        # The kind lookup FKs are now enforced on both tables.
        cur.execute(
            "SELECT count(*) AS n FROM pg_constraint "
            "WHERE conname IN ('nodes_kind_fkey', 'edges_kind_fkey')"
        )
        assert cur.fetchone()["n"] == 2


def test_migrate_v2_to_content_and_specs(request: pytest.FixtureRequest) -> None:
    """A pre-evolvable typed graph gains the ``content`` column and kind specs."""
    request.addfinalizer(_restore_typed_schema)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(V2_DDL)
        # Seed the kind names only (the pre-evolvable state).
        cur.execute("INSERT INTO node_kinds (name) VALUES ('Person'), ('Note')")
        cur.execute("INSERT INTO edge_kinds (name) VALUES ('mentions')")
        cur.execute(
            "INSERT INTO nodes (kind, data) VALUES (%s, %s) RETURNING uuid",
            ("Person", Json({"text": "Ada Lovelace", "born": 1815})),
        )
        person = cur.fetchone()["uuid"]
        conn.commit()

        migrate(conn)

        # The text moved into a real `content` column; data is now pure metadata.
        cur.execute("SELECT content, data FROM nodes WHERE uuid = %s", (person,))
        row = cur.fetchone()
        assert row["content"] == "Ada Lovelace"
        assert "text" not in row["data"]
        assert row["data"]["born"] == 1815

        # The old `data ? 'text'` CHECK is gone (stripping text would have violated it).
        cur.execute(
            "SELECT count(*) AS n FROM pg_constraint c JOIN pg_class t ON c.conrelid = t.oid "
            "WHERE t.relname = 'nodes' AND c.contype = 'c' "
            "AND pg_get_constraintdef(c.oid) ILIKE '%text%'"
        )
        assert cur.fetchone()["n"] == 0

        # Every kind now carries a usable spec (loadable into a resolved kind).
        person_kind = load_node_kind(cur, "Person")
        assert person_kind is not None
        assert "born" in person_kind.fields
        # The full-text index now targets `content`.
        cur.execute(
            "SELECT count(*) AS n FROM pg_indexes "
            "WHERE tablename = 'nodes' AND indexdef ILIKE '%to_tsvector%content%'"
        )
        assert cur.fetchone()["n"] == 1
