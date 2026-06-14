"""PostgreSQL access — connections, schema init + kind seeding, migrations.

Connections use ``dict_row`` so query results map onto the pydantic models in
:mod:`nodum.models`. The schema lives in ``schema.sql`` (package data); the
node/edge kind tables carry each kind's ``spec`` (field shape / endpoint
signature) as JSONB, so the schema is **data** — editable at runtime, loaded
here and handed to :mod:`nodum.metamodel` for validation. The default catalog is
seeded once (into an empty table) from :mod:`nodum.metamodel`; later edits and
deletions are never clobbered by a re-init.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg import Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Json

from nodum import metamodel
from nodum.metamodel import DEFAULT_EDGE_KINDS, DEFAULT_NODE_KINDS, EdgeKind, NodeKind
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


def _has_column(cur: Cursor, table: str, column: str) -> bool:
    """Whether ``table`` currently has a ``column`` (information_schema lookup)."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


# ── Kind seeding + loading ────────────────────────────────────────────────────


def _seed_kind_specs(cur: Cursor) -> None:
    """Seed the default catalog (name + spec) into any **empty** kind table.

    Seeds only when a table is empty — i.e. on first init. A later boot leaves an
    evolved catalog (added/edited/deleted kinds) untouched, so deletions persist.
    """
    cur.execute("SELECT count(*) AS n FROM node_kinds")
    if cur.fetchone()["n"] == 0:
        cur.executemany(
            "INSERT INTO node_kinds (name, spec) VALUES (%s, %s)",
            [
                (name, Json(metamodel.node_kind_to_spec(nk)))
                for name, nk in DEFAULT_NODE_KINDS.items()
            ],
        )
    cur.execute("SELECT count(*) AS n FROM edge_kinds")
    if cur.fetchone()["n"] == 0:
        cur.executemany(
            "INSERT INTO edge_kinds (name, spec) VALUES (%s, %s)",
            [
                (name, Json(metamodel.edge_kind_to_spec(ek)))
                for name, ek in DEFAULT_EDGE_KINDS.items()
            ],
        )


def seed_kinds(conn: psycopg.Connection) -> None:
    """Public seed entry point: insert the default catalog into empty kind tables."""
    with conn.cursor() as cur:
        _seed_kind_specs(cur)
    conn.commit()


def load_node_kinds(cur: Cursor) -> dict[str, NodeKind]:
    """Load every node kind from the DB as resolved NodeKind objects."""
    cur.execute("SELECT name, spec FROM node_kinds")
    return {
        row["name"]: metamodel.node_kind_from_spec(row["name"], row["spec"])
        for row in cur.fetchall()
    }


def load_edge_kinds(cur: Cursor) -> dict[str, EdgeKind]:
    """Load every edge kind from the DB as resolved EdgeKind objects."""
    cur.execute("SELECT name, spec FROM edge_kinds")
    return {
        row["name"]: metamodel.edge_kind_from_spec(row["name"], row["spec"])
        for row in cur.fetchall()
    }


def load_node_kind(cur: Cursor, name: str) -> NodeKind | None:
    """Load one node kind, or ``None`` if no such kind is registered."""
    cur.execute("SELECT name, spec FROM node_kinds WHERE name = %s", (name,))
    row = cur.fetchone()
    return metamodel.node_kind_from_spec(row["name"], row["spec"]) if row else None


def load_edge_kind(cur: Cursor, name: str) -> EdgeKind | None:
    """Load one edge kind, or ``None`` if no such kind is registered."""
    cur.execute("SELECT name, spec FROM edge_kinds WHERE name = %s", (name,))
    row = cur.fetchone()
    return metamodel.edge_kind_from_spec(row["name"], row["spec"]) if row else None


def node_kind_counts(cur: Cursor) -> dict[str, int]:
    """Count nodes per node kind. Kinds with no nodes are absent from the map."""
    cur.execute("SELECT kind, count(*) AS n FROM nodes GROUP BY kind")
    return {row["kind"]: row["n"] for row in cur.fetchall()}


def edge_kind_counts(cur: Cursor) -> dict[str, int]:
    """Count edges per edge kind. Kinds with no edges are absent from the map."""
    cur.execute("SELECT kind, count(*) AS n FROM edges GROUP BY kind")
    return {row["kind"]: row["n"] for row in cur.fetchall()}


def init_schema(conn: psycopg.Connection) -> None:
    """Create the typed schema and seed the default kind catalog, if absent."""
    with conn.cursor() as cur:
        cur.execute(_schema_sql())
        _seed_kind_specs(cur)
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
    """Upgrade a pre-typed (MVP) database in place — idempotent, a no-op once typed.

    Adds the ``kind`` columns, seeds the default kind catalog, drops the MVP
    type-nodes (``data.kind = 'type'``; their ``is`` edges cascade), backfills
    ``kind`` (content nodes → ``Note`` with their old ``data.type`` as ``role``;
    edges from ``data.type``), then enforces NOT NULL + the lookup FKs. The
    ``content`` column is added by :func:`migrate_content`.
    """
    with conn.cursor() as cur:
        if _has_column(cur, "nodes", "kind"):
            return  # already a typed schema; nothing for the MVP step to do
        cur.execute("CREATE TABLE IF NOT EXISTS node_kinds (name TEXT PRIMARY KEY)")
        cur.execute("CREATE TABLE IF NOT EXISTS edge_kinds (name TEXT PRIMARY KEY)")
        cur.execute("ALTER TABLE node_kinds ADD COLUMN IF NOT EXISTS spec JSONB")
        cur.execute("ALTER TABLE edge_kinds ADD COLUMN IF NOT EXISTS spec JSONB")
        _seed_kind_specs(cur)
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


def migrate_kind_specs(conn: psycopg.Connection) -> None:
    """Add the ``spec`` column to the kind tables and backfill it — idempotent.

    Brings a name-only (pre-evolvable) kind catalog up to the spec-carrying schema:
    adds ``spec JSONB``, backfills the known defaults from the seed, gives any
    custom/legacy kind a permissive minimal spec, then enforces NOT NULL. A no-op
    once every kind already carries a spec.
    """
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE node_kinds ADD COLUMN IF NOT EXISTS spec JSONB")
        cur.execute("ALTER TABLE edge_kinds ADD COLUMN IF NOT EXISTS spec JSONB")
        for name, node_kind in DEFAULT_NODE_KINDS.items():
            cur.execute(
                "UPDATE node_kinds SET spec = %s WHERE name = %s AND spec IS NULL",
                (Json(metamodel.node_kind_to_spec(node_kind)), name),
            )
        for name, edge_kind in DEFAULT_EDGE_KINDS.items():
            cur.execute(
                "UPDATE edge_kinds SET spec = %s WHERE name = %s AND spec IS NULL",
                (Json(metamodel.edge_kind_to_spec(edge_kind)), name),
            )
        # Any remaining custom/legacy kind gets a permissive minimal spec.
        cur.execute(
            "UPDATE node_kinds SET spec = %s WHERE spec IS NULL",
            (Json({"group": "", "content_label": "text", "fields": {}}),),
        )
        cur.execute("SELECT name FROM node_kinds ORDER BY name")
        all_node_kinds = [row["name"] for row in cur.fetchall()]
        minimal_edge_spec = {
            "from": all_node_kinds,
            "to": all_node_kinds,
            "symmetric": False,
            "fields": {},
        }
        cur.execute(
            "UPDATE edge_kinds SET spec = %s WHERE spec IS NULL",
            (Json(minimal_edge_spec),),
        )
        cur.execute("ALTER TABLE node_kinds ALTER COLUMN spec SET NOT NULL")
        cur.execute("ALTER TABLE edge_kinds ALTER COLUMN spec SET NOT NULL")
    conn.commit()


def migrate_content(conn: psycopg.Connection) -> None:
    """Promote each node's ``data.text`` into a top-level ``content`` column.

    Adds ``content TEXT``, backfills it from ``data ->> 'text'``, drops the old
    ``data ? 'text'`` CHECK, strips the now-redundant ``text`` key from ``data``,
    and moves the full-text index onto ``content``. Idempotent — a no-op once the
    column exists.
    """
    with conn.cursor() as cur:
        if _has_column(cur, "nodes", "content"):
            return
        cur.execute("ALTER TABLE nodes ADD COLUMN content TEXT")
        cur.execute("UPDATE nodes SET content = COALESCE(data ->> 'text', '')")
        cur.execute("ALTER TABLE nodes ALTER COLUMN content SET NOT NULL")
        # Drop the old `data ? 'text'` CHECK before stripping the key (else the
        # strip would violate it). It is the only CHECK on `nodes` mentioning text.
        cur.execute(
            "SELECT conname FROM pg_constraint c JOIN pg_class t ON c.conrelid = t.oid "
            "WHERE t.relname = 'nodes' AND c.contype = 'c' "
            "AND pg_get_constraintdef(c.oid) ILIKE %s",
            ("%text%",),
        )
        for row in cur.fetchall():
            cur.execute(f'ALTER TABLE nodes DROP CONSTRAINT "{row["conname"]}"')
        cur.execute("UPDATE nodes SET data = data - 'text'")
        cur.execute("DROP INDEX IF EXISTS idx_nodes_fts")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_fts "
            "ON nodes USING gin (to_tsvector('english', content))"
        )
    conn.commit()


def migrate(conn: psycopg.Connection) -> None:
    """Run the full idempotent migration chain — upgrade any older DB to current."""
    migrate_auth(conn)
    migrate_mvp(conn)
    migrate_kind_specs(conn)
    migrate_content(conn)
