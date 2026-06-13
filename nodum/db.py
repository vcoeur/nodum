"""PostgreSQL access — connections, schema init + kind seeding, MVP migration.

Connections use ``dict_row`` so query results map onto the pydantic models in
:mod:`nodum.models`. The schema lives in ``schema.sql`` (package data); the
node/edge kind lookup tables are seeded from :mod:`nodum.metamodel` so the
DB-level FK on ``kind`` always matches the registry.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg import Cursor
from psycopg.rows import dict_row

from nodum.metamodel import EDGE_KINDS, NODE_KINDS
from nodum.settings import load_settings


@contextmanager
def connect(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    """Yield a PostgreSQL connection with ``dict_row`` rows."""
    url = database_url or load_settings().database_url
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn


def _schema_sql() -> str:
    """Return the DDL shipped as package data."""
    return resources.files("nodum").joinpath("schema.sql").read_text(encoding="utf-8")


def _seed_kinds(cur: Cursor) -> None:
    """Mirror the metamodel's kind names into the lookup tables (idempotent)."""
    cur.executemany(
        "INSERT INTO node_kinds (name) VALUES (%s) ON CONFLICT DO NOTHING",
        [(name,) for name in NODE_KINDS],
    )
    cur.executemany(
        "INSERT INTO edge_kinds (name) VALUES (%s) ON CONFLICT DO NOTHING",
        [(name,) for name in EDGE_KINDS],
    )


def init_schema(conn: psycopg.Connection) -> None:
    """Create the typed schema and seed the kind lookup tables, if absent."""
    with conn.cursor() as cur:
        cur.execute(_schema_sql())
        _seed_kinds(cur)
    conn.commit()


# DDL for the single-row auth table; mirrors the auth_secret block in schema.sql
# so an already-initialised database can gain the table without a full re-init.
_AUTH_SECRET_DDL = """
CREATE TABLE IF NOT EXISTS auth_secret (
    id            BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    password_hash TEXT NOT NULL,
    signing_key   TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def migrate_auth(conn: psycopg.Connection) -> None:
    """Create the ``auth_secret`` table if absent — idempotent, a no-op once present."""
    with conn.cursor() as cur:
        cur.execute(_AUTH_SECRET_DDL)
    conn.commit()


def migrate_mvp(conn: psycopg.Connection) -> None:
    """Upgrade a pre-typed (MVP) database in place — idempotent, a no-op when fresh.

    Adds the ``kind`` columns, seeds the lookup tables, drops the MVP type-nodes
    (``data.kind = 'type'``; their ``is`` edges cascade), backfills ``kind``
    (content nodes → ``Note`` with their old ``data.type`` as ``role``; edges
    from ``data.type``), then enforces NOT NULL + the lookup FKs.
    """
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS node_kinds (name TEXT PRIMARY KEY)")
        cur.execute("CREATE TABLE IF NOT EXISTS edge_kinds (name TEXT PRIMARY KEY)")
        _seed_kinds(cur)
        cur.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS kind TEXT")
        cur.execute("ALTER TABLE edges ADD COLUMN IF NOT EXISTS kind TEXT")
        # Drop the MVP type-as-node machinery; its `is` edges cascade.
        cur.execute("DELETE FROM nodes WHERE data ->> 'kind' = 'type'")
        # Backfill kinds for any not-yet-typed rows.
        cur.execute(
            "UPDATE nodes SET kind = 'Note', "
            "data = jsonb_set(data - 'type', '{role}', "
            "to_jsonb(COALESCE(data ->> 'type', 'claim'))) "
            "WHERE kind IS NULL"
        )
        cur.execute(
            "UPDATE edges SET kind = COALESCE(data ->> 'type', 'mentions') WHERE kind IS NULL"
        )
        cur.execute("ALTER TABLE nodes ALTER COLUMN kind SET NOT NULL")
        cur.execute("ALTER TABLE edges ALTER COLUMN kind SET NOT NULL")
        cur.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'nodes_kind_fkey') THEN "
            "ALTER TABLE nodes ADD CONSTRAINT nodes_kind_fkey "
            "FOREIGN KEY (kind) REFERENCES node_kinds(name); END IF; "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'edges_kind_fkey') THEN "
            "ALTER TABLE edges ADD CONSTRAINT edges_kind_fkey "
            "FOREIGN KEY (kind) REFERENCES edge_kinds(name); END IF; END $$"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes (kind)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges (kind)")
    conn.commit()
