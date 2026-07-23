"""Append-only SQLite migrations.

Migrations are applied in list order and recorded in ``schema_migrations``;
once a migration has shipped it is never edited — later changes are new
entries appended to :data:`MIGRATIONS`. Derived stores (FTS, vectors, assets,
renditions) land as their own later migrations.
"""

CORE_DDL = """
CREATE TABLE types (
    id             TEXT PRIMARY KEY,
    graph_id       TEXT NOT NULL DEFAULT 'main',
    name           TEXT NOT NULL UNIQUE,
    parent_type_id TEXT REFERENCES types(id),
    schema_json    TEXT NOT NULL DEFAULT '{}',
    is_builtin     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE nodes (
    id          TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL DEFAULT 'main',
    type_id     TEXT NOT NULL REFERENCES types(id),
    parent_id   TEXT REFERENCES nodes(id),
    position    REAL,
    title       TEXT,
    content     TEXT NOT NULL DEFAULT '',
    props       TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','proposed','archived')),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_nodes_parent ON nodes(parent_id, position);
CREATE INDEX idx_nodes_type   ON nodes(type_id);
CREATE INDEX idx_nodes_state  ON nodes(state);

CREATE TABLE edge_types (
    id           TEXT PRIMARY KEY,
    graph_id     TEXT NOT NULL DEFAULT 'main',
    name         TEXT NOT NULL UNIQUE,
    inverse_name TEXT,
    schema_json  TEXT NOT NULL DEFAULT '{}',
    is_builtin   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE edges (
    id          TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL DEFAULT 'main',
    src_id      TEXT NOT NULL REFERENCES nodes(id),
    dst_id      TEXT NOT NULL REFERENCES nodes(id),
    type_id     TEXT NOT NULL REFERENCES edge_types(id),
    props       TEXT NOT NULL DEFAULT '{}',
    confidence  REAL CHECK (confidence BETWEEN 0 AND 1),
    created_by  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','proposed','archived')),
    valid_from  TEXT,
    valid_to    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_edges_src ON edges(src_id, state);
CREATE INDEX idx_edges_dst ON edges(dst_id, state);

CREATE TABLE merge_redirects (
    tombstone_id TEXT PRIMARY KEY REFERENCES nodes(id),
    into_id      TEXT NOT NULL REFERENCES nodes(id),
    event_seq    INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT NOT NULL REFERENCES nodes(id),
    title      TEXT,
    content    TEXT NOT NULL,
    props      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    event_seq  INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_versions_node ON versions(node_id, created_at);

CREATE TABLE events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    op         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    cycle_id   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_events_cycle ON events(cycle_id);
"""

# Built-in seed set. Built-in type ids equal their names (stable, readable,
# and directly referenceable in wikilinks). Inverse pairs are seeded as two
# rows pointing at each other; symmetric relations are their own inverse.
SEED_NODE_TYPES = [
    "page",
    "block",
    "note",
    "claim",
    "concept",
    "person",
    "org",
    "source",
    "asset_ref",
    "tag",
    "daily",
]

SEED_EDGE_TYPES = [
    ("relates_to", "relates_to"),
    ("supports", "supported_by"),
    ("supported_by", "supports"),
    ("contradicts", "contradicted_by"),
    ("contradicted_by", "contradicts"),
    ("derived_from", "derived"),
    ("derived", "derived_from"),
    ("part_of", "has_part"),
    ("has_part", "part_of"),
    ("authored_by", "authored"),
    ("authored", "authored_by"),
    ("cites", "cited_by"),
    ("cited_by", "cites"),
    ("duplicate_of", "duplicate_of"),
    ("supersedes", "superseded_by"),
    ("superseded_by", "supersedes"),
    ("mentions", "mentions"),
]


def _seed_sql() -> str:
    node_rows = ",\n".join(f"    ('{name}', '{name}', 1)" for name in SEED_NODE_TYPES)
    edge_rows = ",\n".join(
        f"    ('{name}', '{name}', '{inverse}', 1)" for name, inverse in SEED_EDGE_TYPES
    )
    return f"""
INSERT INTO types (id, name, is_builtin) VALUES
{node_rows};

INSERT INTO edge_types (id, name, inverse_name, is_builtin) VALUES
{edge_rows};
"""


PROJECTORS_DDL = """
-- Per-projector checkpoints: the last event-log seq each projector applied.
-- Derived state + this table are all a projector owns; a rebuild resets both
-- and replays from event 0.
CREATE TABLE projector_checkpoints (
    name           TEXT PRIMARY KEY,
    last_event_seq INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Derived FTS index over node text, maintained by the `fts` projector from
-- the event log — never written by the service layer. The `extracted_text`
-- column matches the design schema and stays empty until the asset store
-- (and its extracted text) lands in Phase 4.
CREATE VIRTUAL TABLE node_fts USING fts5(
    node_id UNINDEXED,
    title,
    content,
    extracted_text,
    tokenize = 'porter unicode61'
);
"""


POLICIES_DDL = """
-- Per-agent policy rulesets (design §8.3). `rules` is a JSON array of rule
-- objects keyed by `job` (internal-agent jobs, evaluated by the Phase-5
-- runtime) or `edge_type` (evaluated on the write path today), each with an
-- `action` (`auto_accept` / `auto_apply` / `always_propose`) and an optional
-- `min_confidence` gate. Policy edits are themselves events (`policy.set`).
CREATE TABLE policies (
    agent      TEXT PRIMARY KEY,             -- actor string, e.g. 'agent:researcher'
    rules      TEXT NOT NULL DEFAULT '[]',   -- JSON array of rule objects
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


PROPOSED_VERSIONS_DDL = """
-- Proposed updates (design §8.1: agent `update_node` → new version →
-- `proposed`). A version row is the storage for an agent's proposed edit:
-- `proposed` until a reviewer accepts (the version's content is applied to
-- the node and the row flips to `applied`) or rejects (→ `archived`).
-- Pre-existing snapshots are human/system records of applied state, hence
-- the `applied` default.
ALTER TABLE versions ADD COLUMN state TEXT NOT NULL DEFAULT 'applied'
    CHECK (state IN ('applied','proposed','archived'));
CREATE INDEX idx_versions_state ON versions(state);
"""


#: Ordered (name, SQL) migrations. Append-only — never edit a shipped entry.
MIGRATIONS: list[tuple[str, str]] = [
    ("0001_core", CORE_DDL),
    ("0002_seed_builtin_types", _seed_sql()),
    ("0003_projector_checkpoints_and_fts", PROJECTORS_DDL),
    ("0004_policies", POLICIES_DDL),
    ("0005_proposed_versions", PROPOSED_VERSIONS_DDL),
]
