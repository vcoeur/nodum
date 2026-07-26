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


#: Bootstrap space ids created by ``0009_spaces_and_type_nodes`` (Q13). Meta
#: holds the type vocabulary and, later, the gardener's learned conventions;
#: everyday content reads exclude it — the type catalog is served by the
#: type queries, not by content listings. ``main`` is the first default
#: space and carries no special rules (design-pass note 03 Q9).
META_SPACE_ID = "meta"
MAIN_SPACE_ID = "main"


SPACES_AND_TYPE_NODES_DDL = """
-- Spaces and types-are-nodes (Q13, design §5.1/§5.2 as amended 2026-07-25).
-- `graph_id` becomes `space_id` on nodes only; types and edge types stop
-- being tables and become ordinary nodes living in the meta space, keeping
-- their ids so every existing `type_id` value stays valid across the rewire.
-- The rebuild needs foreign-key enforcement out of the way: the bootstrap is
-- mutually referential (the metaclass root is its own type, meta's space is
-- itself), and create-copy-drop-rename transiently drops tables that others
-- reference. `nodum.db.apply_migration` runs every migration with
-- `PRAGMA foreign_keys=OFF` and runs `foreign_key_check` over the whole
-- database before committing — deferring instead is not enough, because
-- dropping a populated parent leaves a deferred-violation counter that the
-- rename does not clear. This line keeps the script self-describing if it is
-- ever replayed by hand with enforcement on.
PRAGMA defer_foreign_keys = ON;

CREATE TABLE nodes_new (
    id          TEXT PRIMARY KEY,
    space_id    TEXT REFERENCES nodes_new(id),
    type_id     TEXT NOT NULL REFERENCES nodes_new(id),
    parent_id   TEXT REFERENCES nodes_new(id),
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

-- Existing nodes all land in the main space.
INSERT INTO nodes_new
    (id, space_id, type_id, parent_id, position, title, content, props,
     state, created_by, created_at, updated_at)
SELECT id, 'main', type_id, parent_id, position, title, content, props,
       state, created_by, created_at, updated_at
FROM nodes;

-- Bootstrap, in dependency order (enforcement is deferred, so the mutual
-- references resolve at COMMIT): the `type` metaclass root (its own type),
-- the `space` type, and the two space nodes. Meta's space is itself, which
-- keeps "every node has a space" uniform. `type_kind` in props distinguishes
-- node types from edge types among the type-nodes.
INSERT INTO nodes_new
    (id, space_id, type_id, title, props, state, created_by)
VALUES
    ('type',  'meta', 'type',  'type',
     '{"type_kind":"node","is_builtin":1}', 'active', 'system'),
    ('space', 'meta', 'type',  'space',
     '{"type_kind":"node","is_builtin":1}', 'active', 'system'),
    ('meta',  'meta', 'space', 'meta',  '{}', 'active', 'system'),
    ('main',  'meta', 'space', 'main',  '{}', 'active', 'system');

-- Type rows become type-nodes in meta, ids preserved. Their catalogs'
-- columns move into props.
INSERT INTO nodes_new
    (id, space_id, type_id, title, props, state, created_by, created_at, updated_at)
SELECT id, 'meta', 'type', name,
       json_object('type_kind', 'node',
                   'schema_json', json(schema_json),
                   'is_builtin', is_builtin,
                   'parent_type_id', parent_type_id),
       'active', 'system', created_at, created_at
FROM types;

INSERT INTO nodes_new
    (id, space_id, type_id, title, props, state, created_by, created_at, updated_at)
SELECT id, 'meta', 'type', name,
       json_object('type_kind', 'edge',
                   'schema_json', json(schema_json),
                   'is_builtin', is_builtin,
                   'inverse_name', inverse_name),
       'active', 'system', datetime('now'), datetime('now')
FROM edge_types;

DROP TABLE nodes;
ALTER TABLE nodes_new RENAME TO nodes;
CREATE INDEX idx_nodes_parent ON nodes(parent_id, position);
CREATE INDEX idx_nodes_type   ON nodes(type_id);
CREATE INDEX idx_nodes_state  ON nodes(state);
CREATE INDEX idx_nodes_space  ON nodes(space_id);

-- Edges lose `graph_id` entirely (space derives from the endpoints) and
-- retarget `type_id` at the type-nodes.
CREATE TABLE edges_new (
    id          TEXT PRIMARY KEY,
    src_id      TEXT NOT NULL REFERENCES nodes(id),
    dst_id      TEXT NOT NULL REFERENCES nodes(id),
    type_id     TEXT NOT NULL REFERENCES nodes(id),
    props       TEXT NOT NULL DEFAULT '{}',
    confidence  REAL CHECK (confidence BETWEEN 0 AND 1),
    created_by  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','proposed','archived')),
    valid_from  TEXT,
    valid_to    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO edges_new
    (id, src_id, dst_id, type_id, props, confidence, created_by, state,
     valid_from, valid_to, created_at)
SELECT id, src_id, dst_id, type_id, props, confidence, created_by, state,
       valid_from, valid_to, created_at
FROM edges;

DROP TABLE edges;
ALTER TABLE edges_new RENAME TO edges;
CREATE INDEX idx_edges_src ON edges(src_id, state);
CREATE INDEX idx_edges_dst ON edges(dst_id, state);

DROP TABLE types;
DROP TABLE edge_types;

-- Nothing enforced the (hash, space) rule before this migration — not even
-- for proposed rows — so a pre-0009 database can hold duplicates that make
-- the index below fail with a bare IntegrityError, rolling the upgrade back
-- with no way forward but hand SQL. Dedupe first: keep the earliest live
-- describing node per (hash, space), archive the rest. Archiving (rather
-- than deleting) keeps the rows and their history, and the index skips
-- archived rows, so retiring an asset_ref also frees its hash for a new one.
UPDATE nodes
SET state = 'archived', updated_at = datetime('now')
WHERE type_id = 'asset_ref'
  AND state != 'archived'
  AND json_extract(props,'$.asset_hash') IS NOT NULL
  AND rowid NOT IN (
      SELECT rowid FROM (
          SELECT rowid, ROW_NUMBER() OVER (
              PARTITION BY json_extract(props,'$.asset_hash'), space_id
              ORDER BY created_at, rowid
          ) AS rank
          FROM nodes
          WHERE type_id = 'asset_ref'
            AND state != 'archived'
            AND json_extract(props,'$.asset_hash') IS NOT NULL
      )
      WHERE rank = 1
  );

-- One live describing asset_ref node per (hash, space) — guards the shape
-- before Phase 4 writes any (design-pass note 04).
CREATE UNIQUE INDEX idx_asset_ref_per_space ON nodes(
    json_extract(props,'$.asset_hash'), space_id
) WHERE type_id = 'asset_ref' AND state != 'archived';
"""


PRINCIPALS_DDL = """
-- Principals and grants (Q13, design §5.2 as amended 2026-07-25). Human
-- accounts are identity + credentials + attribution, never a permission
-- scope — the file is the only isolation boundary, and every human is
-- full-rights. Agents act within per-(agent, space) grants; the owner
-- holds no grants at all. The policies table dies here (design §8.3:
-- learned trust, no policy layer) — auto-accept on the write path dies
-- with it.
CREATE TABLE humans (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    credential_hash TEXT,                -- argon2id; NULL until a password is set
    disabled        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE agents (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('internal','external')),
    name            TEXT NOT NULL,
    owner_human_id  TEXT REFERENCES humans(id),
    credential_hash TEXT,                -- sha-256 of the current token; NULL for internal
    disabled        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- An external agent answers to a human (that is what cascading
    -- revocation walks); an internal one is the graph's own, and has none.
    CHECK (kind = 'internal' OR owner_human_id IS NOT NULL)
);

CREATE TABLE grants (
    agent_id   TEXT NOT NULL REFERENCES agents(id),
    space_id   TEXT NOT NULL REFERENCES nodes(id),   -- a node of builtin type 'space'
    level      TEXT NOT NULL CHECK (level IN ('read','suggest','edit')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, space_id)
);

CREATE TABLE sessions (
    id         TEXT PRIMARY KEY,         -- random; the cookie value
    human_id   TEXT NOT NULL REFERENCES humans(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL             -- 30 days, sliding
);

-- The first human account: trusted-local bootstrap, no password yet — it
-- cannot log in over HTTP until `nodum human passwd` sets one.
INSERT INTO humans (id, name) VALUES ('owner', 'owner');

-- One agents row per agent identity the log already knows (event actors,
-- version actors, created_by columns, and the dying policies table), all
-- external, owned by the first human, with no token — they cannot
-- authenticate until one is minted. Every source is the *bare* name: an
-- agent id is what `verify_agent_token` looks up and what `agent:` is
-- prefixed to, so a row seeded as 'agent:x' would be unusable and would
-- attribute writes to 'agent:agent:x'. `policies.agent` is the one column
-- that stored the full actor string, hence its own strip.
INSERT INTO agents (id, kind, name, owner_human_id)
SELECT DISTINCT name, 'external', name, 'owner' FROM (
    SELECT substr(actor, 7) AS name FROM events WHERE actor LIKE 'agent:%'
    UNION SELECT substr(actor, 7) FROM versions WHERE actor LIKE 'agent:%'
    UNION SELECT substr(created_by, 7) FROM nodes WHERE created_by LIKE 'agent:%'
    UNION SELECT substr(created_by, 7) FROM edges WHERE created_by LIKE 'agent:%'
    UNION SELECT substr(agent, 7) FROM policies WHERE agent LIKE 'agent:%'
)
-- A bare 'agent:' actor would otherwise seed an empty-id row.
WHERE name IS NOT NULL AND length(name) > 0;

-- Parity grants: exactly today's behaviour (read everything, propose
-- anywhere) so migration changes no agent's effective reach. The owner
-- tightens from here.
INSERT INTO grants (agent_id, space_id, level)
SELECT id, 'meta', 'read' FROM agents;
INSERT INTO grants (agent_id, space_id, level)
SELECT id, 'main', 'suggest' FROM agents;

DROP TABLE policies;
"""


ACTOR_STRINGS_DDL = """
-- Structured actor strings (Q13 R2): the bare 'human' becomes a reference
-- to the first human account. Agent strings are already 'agent:<name>'
-- with agent ids equal to the names, so they need no rewrite. Event
-- payloads (JSON before/after) are immutable history and keep old values.
UPDATE events   SET actor      = 'human:owner' WHERE actor      = 'human';
UPDATE versions SET actor      = 'human:owner' WHERE actor      = 'human';
UPDATE nodes    SET created_by = 'human:owner' WHERE created_by = 'human';
UPDATE edges    SET created_by = 'human:owner' WHERE created_by = 'human';
"""


URL_TOKENS_DDL = """
-- Short-lived capability URLs for the two escape hatches (design §5.7 rule 4,
-- Phase 4 note 01 D4). An agent host that shares no filesystem with the graph
-- needs a way to fetch an original and a way to hand bytes back; these rows
-- are that authority, and the event log records every one of them.
--
-- A token is a **random secret whose sha-256 is stored** — a capability, not
-- an HMAC signature, and the difference is the whole reason this table exists.
-- A signed URL moves the authority into a key that has to be generated,
-- stored, rotated, and kept out of every backup and log, and it is valid until
-- it expires and not one moment less: revoking one, or spending one, means a
-- table of ids anyway. Here the row *is* the authority. Expiry, single use and
-- revocation are all one UPDATE on it, there is no key to manage, and a
-- database read leak hands out no usable URL because the secret was never
-- written down. This is exactly how `nodum.auth` stores agent tokens, and it
-- reuses that module's generator and hash.
--
-- `used_at` is the single-use latch: NULL means live, and redemption is one
-- `UPDATE … WHERE used_at IS NULL AND expires_at > datetime('now')` whose
-- rowcount decides the outcome. Two concurrent redemptions of the same URL
-- therefore cannot both win — a read-then-write would let them.
--
-- **No foreign keys, deliberately.** An upload's `asset_hash` is what the
-- caller *declares* it is about to send, so by definition no `assets` row
-- exists for it yet; and a FK on `space_id` would let an expiring capability
-- block a graph write (an undo deleting a space node) minutes after anyone
-- cared about it. These rows are transient authority, not graph structure —
-- the event log is where a mint and a redemption are recorded for good.
CREATE TABLE url_tokens (
    id            TEXT PRIMARY KEY,     -- public handle; payloads name this, never the secret
    token_hash    TEXT NOT NULL UNIQUE, -- sha-256 of the secret, which is never stored
    kind          TEXT NOT NULL CHECK (kind IN ('download','upload')),
    asset_hash    TEXT,                 -- download: the target; upload: the declared hash or NULL
    original_name TEXT,                 -- upload: the name the bytes claim
    mime          TEXT,                 -- upload: the declared type
    max_bytes     INTEGER,              -- upload: the declared size, the ceiling on the body
    space_id      TEXT,                 -- upload: where the describing node is meant to land
    created_by    TEXT NOT NULL,        -- actor string; a redemption is attributed to it too
    expires_at    TEXT NOT NULL,        -- datetime('now', …): UTC, like every other timestamp
    used_at       TEXT,                 -- NULL until redeemed — the single-use latch
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- A download token with no target, or an upload with no ceiling, is a
    -- grant to nothing in particular. Refuse it in the schema rather than
    -- discovering it at redemption, when the caller is a stranger with bytes.
    CHECK (kind != 'download' OR asset_hash IS NOT NULL),
    CHECK (kind != 'upload' OR max_bytes IS NOT NULL)
);

-- Redemption keys on `token_hash`, which UNIQUE already indexes; nothing else
-- is looked up by anything else. This index is for the sweep: a token nobody
-- ever comes back for expires unnoticed otherwise, exactly as sessions did
-- before `create_session` started sweeping them (Q13 review N7).
CREATE INDEX idx_url_tokens_expires ON url_tokens(expires_at);
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
    ("0009_spaces_and_type_nodes", SPACES_AND_TYPE_NODES_DDL),
    ("0010_principals", PRINCIPALS_DDL),
    ("0011_actor_strings", ACTOR_STRINGS_DDL),
    ("0012_url_tokens", URL_TOKENS_DDL),
]
