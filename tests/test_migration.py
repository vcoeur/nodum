"""Migration test: upgrade a pre-typed (MVP) graph in place to the typed schema.

Builds an MVP-shaped graph by raw SQL — content nodes carrying ``data.type``, a
shared type-as-node, an ``is`` edge to it, and an untyped ``supports`` edge —
then runs :func:`migrate_mvp` and asserts the typed result. Because it reshapes
the shared tables, a finalizer rebuilds the canonical typed schema afterwards so
later tests are unaffected.
"""

from __future__ import annotations

import pytest
from psycopg.types.json import Json

from nodum.db import connect, init_schema, migrate_mvp

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


def _restore_typed_schema() -> None:
    """Drop the MVP-reshaped tables and rebuild the canonical typed schema."""
    with connect() as conn:
        conn.cursor().execute("DROP TABLE IF EXISTS edges, nodes CASCADE")
        conn.commit()
        init_schema(conn)


def test_migrate_mvp_to_typed(request: pytest.FixtureRequest) -> None:
    """An MVP graph (type-as-node, untyped edges) migrates to typed nodes/edges."""
    # This test reshapes the shared tables; rebuild the typed schema afterwards.
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
