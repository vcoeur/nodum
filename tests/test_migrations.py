"""Migrations from scratch: schema shape, seed catalog, idempotency."""

from __future__ import annotations

from nodum import db, service
from nodum.migrations import MIGRATIONS, SEED_EDGE_TYPES, SEED_NODE_TYPES

CORE_TABLES = {
    "types",
    "nodes",
    "edge_types",
    "edges",
    "versions",
    "events",
    "merge_redirects",
    "schema_migrations",
}


def test_init_applies_all_migrations(fresh_db):
    result = service.init()
    assert result.applied == []
    assert result.already_applied == [name for name, _ in MIGRATIONS]


def test_init_creates_core_tables(fresh_db):
    conn = db.connect()
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        assert {row["name"] for row in rows} >= CORE_TABLES
    finally:
        conn.close()


def test_init_seeds_builtin_node_types(fresh_db):
    catalog = service.list_types()
    names = {node_type.name for node_type in catalog.node_types}
    assert names == set(SEED_NODE_TYPES)
    assert all(node_type.is_builtin for node_type in catalog.node_types)
    # Built-in type ids equal their names.
    assert all(node_type.id == node_type.name for node_type in catalog.node_types)


def test_init_seeds_builtin_edge_types_with_inverses(fresh_db):
    catalog = service.list_types()
    by_name = {edge_type.name: edge_type for edge_type in catalog.edge_types}
    assert set(by_name) == {name for name, _ in SEED_EDGE_TYPES}
    for name, inverse in SEED_EDGE_TYPES:
        assert by_name[name].inverse_name == inverse
    # Inverse pairs are symmetric: the inverse of the inverse is the original.
    for name, edge_type in by_name.items():
        assert by_name[edge_type.inverse_name].inverse_name == name


def test_graph_id_defaults_to_main(fresh_db):
    node = service.create_node(type="note", title="n1")
    assert node.graph_id == "main"
    edge_target = service.create_node(type="note", title="n2")
    edge = service.create_edge(node.id, edge_target.id, "relates_to")
    assert edge.graph_id == "main"


def test_wal_mode_enabled(fresh_db):
    conn = db.connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()
