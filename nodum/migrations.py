"""Append-only SQLite migrations.

Migrations are applied in list order and recorded in ``schema_migrations``;
once a migration has shipped it is never edited — later changes are new
entries appended to :data:`MIGRATIONS`. Derived stores (FTS, vectors, assets,
renditions) land as their own later migrations.

Each entry is applied together with its ``schema_migrations`` row in one
transaction (:func:`nodum.db.init_db`), so a migration is either wholly
applied and recorded or wholly rolled back and retried — never half-applied.
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


VECTORS_DDL = """
-- Derived vector index (design §5.2 + D6), maintained by the `vec` projector
-- from the event log — never written by the service layer. `chunks` holds
-- one fixed-window text chunk per row (512 words, ~15% overlap); `node_vec`
-- (sqlite-vec) holds each chunk's embedding, keyed by the chunk's rowid —
-- which is why `chunks.id` is an integer rowid rather than the design's TEXT
-- id (vec0 keys on integer rowids). `model_id` records the producing
-- embedding model per chunk, so mixed-model states are detectable and a
-- model change is a `projector rebuild vec`. The vec0 dimension must match
-- nodum.embeddings.EMBEDDING_DIMS (the default model's size); a dimension
-- change needs a new migration. `chunks.node_id` deliberately carries no FK:
-- the projector replays the event log (not the live tables), and the log
-- still contains events for nodes whose create was later undone — a FK to
-- `nodes` would make that replay fail.
CREATE VIRTUAL TABLE node_vec USING vec0(
    embedding float[384] distance_metric=cosine
);

CREATE TABLE chunks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    text     TEXT NOT NULL,
    model_id TEXT NOT NULL
);
CREATE INDEX idx_chunks_node ON chunks(node_id);
"""


ASSETS_DDL = """
-- Content-addressed binary assets (design §5.2/§5.5). Metadata and bytes are
-- separate tables in the *same* database file: `assets` is the metadata row,
-- `asset_blobs` holds the bytes under the same sha256 key. Splitting them
-- keeps metadata queries and FTS off blob overflow pages (and leaves the door
-- open to ATTACHing the bytes out to a second file if scale ever demanded it),
-- while `DB = everything` still holds for backup and restore. Asset bytes have
-- never lived on the filesystem: there is no `path` column anywhere here, so
-- no asset can be stranded by a later move. `extracted_text` stays NULL until
-- the Phase-4 ingestion pipeline fills it. Registration is idempotent
-- content-addressed dedup — no event-log entry (nothing to undo: the same
-- bytes always resolve to the same row; asset events land with ingestion in
-- Phase 4 and must never inline blob bytes into payloads).
CREATE TABLE assets (
    hash           TEXT PRIMARY KEY,
    mime           TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    original_name  TEXT,
    extracted_text TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The originals' bytes. Keyed by the same sha256 as `assets`, so content
-- addressing and dedup are properties of the key, not of the storage. The
-- table keeps its implicit rowid: `Connection.blobopen` addresses blobs by
-- rowid, and the streaming reads/writes that keep a large file out of memory
-- depend on it. Never inline these bytes into event payloads.
CREATE TABLE asset_blobs (
    hash TEXT PRIMARY KEY REFERENCES assets(hash),
    data BLOB NOT NULL
);

-- Derived image renditions (design §5.7): lazily generated from the original,
-- stored as bytes right here, and evictable (`asset purge`) — every row is
-- regenerable, which is why the table carries the image rather than pointing
-- at a cache file that could disagree with it. `id` is
-- sha256(asset_hash + ':' + profile).
CREATE TABLE renditions (
    id          TEXT PRIMARY KEY,
    asset_hash  TEXT NOT NULL REFERENCES assets(hash),
    profile     TEXT NOT NULL,
    data        BLOB NOT NULL,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_renditions_asset ON renditions(asset_hash);
"""


PROPOSED_FIELDS_DDL = """
-- Which node fields an agent's proposed update actually named (design §8.1).
-- A proposed version stores a full title/content/props snapshot — unnamed
-- fields are copied from the node at proposal time as reviewer context — so
-- without this column an accept would write the whole snapshot back and
-- silently revert every edit made between proposal and review. The column is
-- a JSON array of field names ('title', 'content', 'props'); NULL means "not
-- a proposal" for applied/undo snapshots, and is read as all three fields for
-- any proposal staged before this migration.
ALTER TABLE versions ADD COLUMN proposed_fields TEXT;
"""


#: Ordered (name, SQL) migrations. Append-only — never edit a shipped entry.
MIGRATIONS: list[tuple[str, str]] = [
    ("0001_core", CORE_DDL),
    ("0002_seed_builtin_types", _seed_sql()),
    ("0003_projector_checkpoints_and_fts", PROJECTORS_DDL),
    ("0004_policies", POLICIES_DDL),
    ("0005_proposed_versions", PROPOSED_VERSIONS_DDL),
    ("0006_vectors", VECTORS_DDL),
    ("0007_assets_and_renditions", ASSETS_DDL),
    ("0008_version_proposed_fields", PROPOSED_FIELDS_DDL),
]
