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


PROJECTOR_SKIPS_DDL = """
-- Events a projector quarantined instead of replaying forever (finding M12).
-- One row per (projector, seq): the event a projector's apply refused, the op
-- it carried, and why it raised. The checkpoint advances past a skipped
-- event, so one malformed row can no longer wedge a projector; this table is
-- the audit trail a human reads to see what was skipped and fix the writer.
-- It is append-only like the log it annotates: a rebuild replays a still-bad
-- event and refreshes the row (the writer upserts) rather than deleting it.
CREATE TABLE projector_skips (
    projector  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    op         TEXT NOT NULL,
    error      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (projector, seq)
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
-- embedding model per chunk, so search filters the KNN join to the active
-- provider's model — mixed-model chunks are excluded from results, and a
-- model change needs a `projector rebuild vec` to be effective (finding
-- M13). The vec0 dimension must match
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


UNIQUE_SPACE_TITLES_DDL = """
-- One space per title, in any state, for good (human-UI phase, gap 2). Every
-- space reference on every surface resolves as `id = ? OR title = ?`
-- (`service._resolve_space`), and nothing stopped two spaces carrying the same
-- title — after which `--space research` meant whichever row SQLite reached
-- first, silently, and differently depending on the query plan. Spaces became
-- creatable from a screen in this phase, so the collision went from theoretical
-- to likely.
--
-- What the index covers:
--
--   * `type_id = 'space'`, the resolver's own predicate. Deliberately *not*
--     also scoped to the meta space: the resolver does not care which space a
--     space node lives in, so a `space`-typed node created anywhere resolves,
--     and an index scoped to meta would leave that hole open.
--   * **Every state, archived included.** This started out as `state !=
--     'archived'`, on the argument that an archived space stops resolving so
--     its name must not stay reserved — "and it would be for good, because the
--     state machine has no un-archive anywhere". That argument was false.
--     `service.undo` never consults `TRANSITIONS`: for a non-create event it
--     writes the `before` row back with a raw UPDATE, so undoing a
--     `node.archive` restores `state = 'active'`. Archive a space, create a new
--     one with the freed name, undo the archive — and the undo died on a bare
--     `UNIQUE constraint failed: nodes.title`, which `/api/undo` served as a
--     500. Archiving exists precisely *because* it is not deletion, so a rule
--     whose correctness rests on nothing ever coming back contradicts the thing
--     it is protecting. Titles are therefore reserved forever: the cost is that
--     a retired space's name cannot be reused, and the return is that a restore
--     can never fail and that index membership no longer depends on `state`, so
--     no state change of any kind can collide. (`proposed` was always inside
--     the index, for the neighbouring reason: two proposed spaces sharing a
--     title would both accept cleanly and collide only afterwards. That hazard
--     is gone with the predicate that caused it.)
--   * Exact, never case-folded. `title = ?` compares under SQLite's default
--     BINARY collation, so `Research` and `research` are two names that tell
--     themselves apart perfectly; a NOCASE index would refuse a pair the
--     resolver handles. The constraint is exactly as tight as the lookup.
--
-- A NULL title is left out of the index entirely: a space with no title cannot
-- be named by one, so no two of them are ambiguous.
--
-- Dedupe first, or this fails with a bare IntegrityError on any database that
-- already holds a collision — 0009 was bitten by exactly that, and this is its
-- lesson applied. The losers are **renamed, not archived**: archiving retires a
-- space from the vocabulary permanently, which is far more than a duplicate
-- title deserves, while `<title> (<id>)` leaves every space usable and is
-- unique because the id is. Titles change here without an event or a version,
-- as every migration's data repair does.
--
-- Three properties the dedupe has to have, each one a defect it was found
-- without:
--
--   1. **An `active` row keeps the name**, whatever else shares it. The
--      tie-break demotes every non-active state at once (`state != 'active'`),
--      not just `archived`: a `proposed` duplicate used to sort level with a
--      live one and win on `created_at`, so an older proposal took the name off
--      a live space — `--space research` stopped resolving at all, and started
--      resolving to a *different* space the moment the proposal was accepted.
--      An agent with `suggest` on meta can file proposed spaces, and nothing
--      enforced uniqueness before this migration, so that state is reachable.
--      What a reference resolves to today must go on resolving to the same
--      space after the upgrade.
--   2. **The rename cannot itself collide.** `<title> (<id>)` is unique among
--      losers because ids are, but a database can already hold a space
--      literally titled `research (sp-b)` — and then deduping two spaces called
--      `research` produced that very string, the index refused it, and the
--      whole upgrade rolled back with the bare IntegrityError this migration
--      exists to prevent. So the name is searched rather than assumed: the base
--      `<title> (<id>)`, then `<base> 1`, `<base> 2`, … until one is free. The
--      search only has to dodge names that survive the statement (titles no
--      loser is vacating, and every space id — see 3), because two losers can
--      never land on one string: distinct ids make the bases distinct, a base
--      always ends in `)` while a suffixed name always ends in a digit, and two
--      suffixed names split unambiguously at that trailing digit.
--   3. **The id/title ambiguity is deduped too.** `_resolve_space` matches
--      `id = ? OR title = ?`, so a space *titled* `sp-x` while another space is
--      *identified* `sp-x` is the same ambiguity as two equal titles — and one
--      no index can express, which is why `service._require_space_name_free`
--      refuses to create it. The migration left it standing, so a database
--      could carry it past the upgrade and resolve `sp-x` plan-dependently
--      forever, invisibly. The row holding the *title* loses: an id is
--      immutable and is the reference of last resort.
WITH RECURSIVE
ranked AS (
    SELECT rowid AS rid, id, title, ROW_NUMBER() OVER (
        PARTITION BY title ORDER BY state != 'active', created_at, rowid
    ) AS rank
    FROM nodes
    WHERE type_id = 'space' AND title IS NOT NULL
),
space_ids AS (SELECT id FROM nodes WHERE type_id = 'space'),
losers AS (
    SELECT r.rid, r.title || ' (' || r.id || ')' AS base
    FROM ranked r
    WHERE r.rank > 1
       OR EXISTS (SELECT 1 FROM space_ids s WHERE s.id = r.title AND s.id <> r.id)
),
keepers AS (
    SELECT title FROM ranked WHERE rid NOT IN (SELECT rid FROM losers)
),
candidates(rid, base, suffix, name) AS (
    SELECT rid, base, 0, base FROM losers
    UNION ALL
    SELECT rid, base, suffix + 1, base || ' ' || (suffix + 1)
    FROM candidates
    WHERE name IN (SELECT title FROM keepers) OR name IN (SELECT id FROM space_ids)
),
resolved AS (
    SELECT rid, name FROM candidates
    WHERE name NOT IN (SELECT title FROM keepers)
      AND name NOT IN (SELECT id FROM space_ids)
)
UPDATE nodes
SET title = (SELECT name FROM resolved WHERE resolved.rid = nodes.rowid),
    updated_at = datetime('now')
WHERE rowid IN (SELECT rid FROM resolved);

CREATE UNIQUE INDEX idx_space_title ON nodes(title)
WHERE type_id = 'space' AND title IS NOT NULL;
"""


#: The internal agent seeded by ``0014`` — the gardener, the only principal that
#: authenticates by being in-process (design §8.4). Its id is also the whole of
#: the reserved prefix below, for now.
GARDENER_AGENT_ID = "builtin-gardener"

#: Reserved id prefix for agents the system seeds. ``Principal.actor_string``
#: renders every agent as ``agent:<id>``, so an *external* agent free to take
#: this id would write events indistinguishable from the gardener's — and the
#: event log is this system's answer to "who is answerable for this write".
#: Enforced for new accounts in :func:`nodum.service.create_agent` and, for
#: databases written before that check existed, by ``0014`` refusing to upgrade.
BUILTIN_AGENT_PREFIX = "builtin-"


CYCLES_AND_GARDENER_DDL = """
-- Consolidation cycles and the gardener that runs them (Phase 5, design §8.4).
--
-- A cycle groups a set of graph writes under one id so that a human can take
-- the whole of it back in one action. `events.cycle_id` has been there since
-- 0001 and no caller ever set it; this is the table it points at, and the
-- dream journal's record: what ran, who asked for it, over what, and how it
-- ended. The per-cycle *diff* is deliberately not stored here — that is
-- `list_events` filtered by `cycle_id`, so the journal can never become a
-- second, disagreeing record of what happened.
--
-- `triggered_by` is who **asked** — a human's `human:<id>`, or the literal
-- `scheduler` when the clock did. It is deliberately not the same thing as the
-- `actor` on the events the cycle contains, which is who **acted**: the
-- gardener. A journal entry that carried only one of the two could not answer
-- "I did not ask for this" or "who ran this at 04:00", and they are different
-- questions.
--
-- `scope` carries a space id or NULL (the whole file), and takes **no foreign
-- key** on purpose, for the reason `url_tokens` takes none: a cycle row is
-- history, and a reference from history into the live graph would let an old
-- journal entry block an ordinary graph write (an undo deleting a space node)
-- long after anyone cared. `rolled_back_by` does point at `cycles(id)` — that
-- is one journal entry naming another, which is structure, not history
-- reaching forward.
CREATE TABLE cycles (
    id             TEXT PRIMARY KEY,   -- uuid4().hex, like every other generated id
    trigger        TEXT NOT NULL CHECK (trigger IN ('manual','scheduled','curative','rollback')),
    triggered_by   TEXT NOT NULL,      -- who asked: 'human:<id>', or 'scheduler'
    scope          TEXT,               -- a space id, or NULL for the whole file
    dry_run        INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL CHECK (status IN ('running','completed','failed','rolled_back')),
    report         TEXT,               -- JSON; NULL while the cycle is running
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,               -- NULL while the cycle is running
    rolled_back_by TEXT REFERENCES cycles(id)   -- the rollback cycle that reversed this one
);

-- The journal is read newest-first and, later, by date range; nothing else here
-- is looked up by anything but the primary key.
CREATE INDEX idx_cycles_started ON cycles(started_at);

-- **One consolidation cycle at a time, in the whole file.** This index is the
-- lock, and it is here rather than in Python because the thing it serialises
-- crosses processes: a `nodum consolidate` fired while `nodum serve` runs one is
-- two interpreters, and a module-level lock is in neither of the other's. Both
-- runs completed, and since every job's "leave what is already there alone" is a
-- read followed by a write with no transaction spanning it, every duplicate pair
-- was proposed twice — 1580 `duplicate_of` edges over 790 pairs, and two journal
-- rows for one human intention. The review queue is the human's; doubling it is
-- the defect the in-process lock was raised against, and a lock that covers one
-- process covers the wrong half of it.
--
-- The `cycles` table already *is* the cross-process state — a `running`
-- consolidation row means one is running, whoever opened it — so the guard is a
-- uniqueness constraint over exactly that, and the second opener loses on the
-- INSERT. That matters: a `SELECT` then an `INSERT` is two statements with a
-- window between them, and two runners racing that window is the case this
-- exists for. `status` is the indexed column and it is constant 'running' across
-- every indexed row, so the index admits at most one.
--
-- **Scoped to the two triggers a consolidation run opens.** A `curative` cycle
-- is one human-driven operation and a `rollback` is the human's undo; both are
-- short, both open a cycle of their own, and blocking either for the length of a
-- nightly sweep would take the curative tier offline every night. They are not
-- what proposes a duplicate pair twice.
--
-- A cycle left `running` by a `SIGKILL` or a power cut therefore blocks every
-- later run, which is what `nodum cycle-abandon <id>` is the door out of —
-- named in the refusal itself, since advice nobody can carry out is the failure
-- shape this repo has already fixed once.
CREATE UNIQUE INDEX idx_cycles_one_running_consolidation
    ON cycles(status)
    WHERE status = 'running' AND trigger IN ('manual', 'scheduled');

-- The gardener's own account. D7 is "auto-apply by default", and a gardener
-- with no grant does nothing at all — every write it makes goes through the
-- same scope-bound store as an external agent's, so seeding the identity
-- without seeding its authority would ship a phase that silently no-ops. The
-- grants are **ordinary rows**: they show up in `nodum space-list` and on the
-- `/spaces` screen beside every other agent's, and `nodum revoke
-- builtin-gardener main` takes them away with the command that was already
-- there. There is no gardener-shaped exception anywhere in the grant model,
-- which is the point.
--
-- `internal` means it holds no credential: it authenticates by being
-- in-process (`auth.internal_principal`), and `rotate_agent_token` already
-- refuses to mint one for an internal agent.
--
-- **A collision under the reserved prefix refuses the upgrade rather than
-- resolving it.** `agents.id` is a PRIMARY KEY and `create_agent` sets
-- `agent_id = name` verbatim, so a database written before this migration can
-- already hold a user's agent called `builtin-gardener`. Neither way out of
-- that is safe: taking the id would attribute that agent's whole history —
-- every `agent:builtin-gardener` in `events.actor`, `versions.actor` and both
-- `created_by` columns — to the gardener, and renaming the impostor would
-- detach that same history from the account it names, since the actor strings
-- are immutable log entries and not references anything can follow. Both
-- corrupt the one question the event log exists to answer. So the migration
-- stops and says so, and the operator renames or removes the account by hand
-- and re-runs; the whole migration rolls back as one transaction, exactly as
-- any other failing one does.
--
-- **The check is the whole prefix, not the one id.** What is reserved is
-- `builtin-` (`BUILTIN_AGENT_PREFIX`), because `Principal.actor_string` renders
-- every agent as `agent:<id>` and the reservation is what keeps one such string
-- naming one principal. Checking `builtin-gardener` alone let a pre-0010
-- database whose log merely *mentions* `agent:builtin-librarian` upgrade
-- clean — 0010 back-fills an `agents` row from every actor string it finds, so
-- the upgrade installs a live, token-bearing external agent under the reserved
-- prefix, and the collision this guard exists to refuse arrives pre-installed
-- the day a second `builtin-*` agent is seeded. `LIKE 'builtin-%'` closes that,
-- and it still catches the original single-id case.
--
-- `RAISE()` is a trigger-only construct in SQLite, so the abort is a CHECK
-- constraint whose **name** carries the message (SQLite reports it verbatim as
-- `CHECK constraint failed: <name>`). The name is static SQL and **cannot
-- interpolate the ids it found**: SQLite accepts an expression as `RAISE()`'s
-- second argument only from 3.47.1 (2024-11-25), newer than the library most
-- distributions ship, and a migration that fails to *parse* on an ordinary
-- machine is a far worse failure than one whose message has to be looked up. So
-- the message carries the LIKE pattern instead — `nodum agent list`, or one
-- `SELECT id FROM agents WHERE id LIKE 'builtin-%'`, names them. The insert
-- below produces a row only when something under the prefix exists.
CREATE TABLE _reserved_agent_id (
    taken TEXT,
    CONSTRAINT
"agent ids matching 'builtin-%' are reserved: rename or remove them, then re-run"
    CHECK (taken IS NULL)
);
INSERT INTO _reserved_agent_id (taken)
SELECT id FROM agents WHERE id LIKE 'builtin-%';
DROP TABLE _reserved_agent_id;

INSERT INTO agents (id, kind, name, owner_human_id, credential_hash)
VALUES ('builtin-gardener', 'internal', 'builtin-gardener', NULL, NULL);

-- `read` on meta and `edit` on main — the shape every other curating agent in
-- this system holds. Meta because resolving a node or edge type is a READ of
-- the type vocabulary, and consolidation never writes it: every job filters
-- through `consolidate._is_curatable`, which excludes the meta space and the
-- structural types outright. `edit` on meta was the first cut, justified as
-- "reads and writes the vocabulary", and the write half was never true — what
-- the level actually bought was authority no shipped job reaches: creating
-- spaces, renaming `main`, retitling the `concept` type, and archiving the
-- `note` type, after which a *human* can no longer write a note either. A grant
-- is a ceiling, and this one is now set at what the jobs need.
--
-- Main because that is where every write naming no space lands. Any other space
-- is an explicit `nodum grant builtin-gardener <space> edit`, like it is for
-- every other agent — which is also what a scoped cycle over a space created
-- after this migration asks for by name.
INSERT INTO grants (agent_id, space_id, level) VALUES
    ('builtin-gardener', 'meta', 'read'),
    ('builtin-gardener', 'main', 'edit');
"""


CYCLE_STOP_SWITCH_DDL = """
-- The kill switch's row (Phase 5b, design K1-K3). `nodum cycle-stop <id>` typed
-- at a terminal has to stop a cycle running inside `nodum serve`, and those are
-- two interpreters -- so the stop is a **row rather than a process signal**, for
-- the same reason 0014's one-running-consolidation guard is an index rather than
-- a module-level lock. The runner reads it between jobs, between items, and
-- immediately before every provider call (`nodum.agent.cycle_stop_check`).
--
-- It is deliberately **not** a reuse of `abandon_cycle`. That verb is a repair
-- -- a human declaring somebody else's dead process dead -- and this is an
-- instruction to a live run which is expected to notice and wind down honestly.
-- A journal that could not tell "the operator stopped this" from "this process
-- died" would fail the human reading a `failed` cycle at 09:00, so the two facts
-- get different columns and different verbs.
--
-- **Two columns and no boolean flag.** `nodum.agent.cycle_stop_check`'s
-- docstring proposes `stop_requested INTEGER NOT NULL DEFAULT 0` beside the two
-- stamps; checked against the table it describes, that is one column too many.
-- `cycles` already separates facts fixed when the row is inserted (`trigger`,
-- `dry_run`, `status`, `started_at` -- NOT NULL, with defaults) from facts that
-- arrive later, and every one of the second kind is a nullable column whose
-- presence *is* the flag: `finished_at`, `report`, `rolled_back_by`. Nothing in
-- this table carries a boolean beside a nullable stamp for the same event. A
-- stop is the second kind, so it is written the way the table already writes
-- them.
--
-- The flag would also be a fourth instance of the defect class this phase has
-- recorded three times already (`merge_redirects`, `versions.state`, the
-- rollback findings): state that a later reader has to reconcile with the record
-- beside it. `stop_requested = 1` with no `stop_requested_by` is a stop nobody
-- asked for, and SQLite's ALTER TABLE cannot add the table-level CHECK that
-- would forbid it -- the alternative being a create-copy-drop-rename rebuild of
-- a table on every install, to buy a column that answers `stop_requested_at IS
-- NOT NULL`. `CycleOut.stop_requested` is that expression, computed on every
-- read and stored nowhere, exactly as `CycleDetailOut.metrics` projects the
-- report rather than copying it.
--
-- The one disagreement two columns can still have -- a requester with no time,
-- or a time with no requester -- is closed by a CHECK, which `ALTER TABLE ...
-- ADD COLUMN` *does* accept, cross-column and named. The name is the message
-- SQLite prints (`CHECK constraint failed: <name>`), the same device 0014 uses
-- for its reserved-prefix abort, because `RAISE()` is trigger-only here.
--
-- Both columns are pure additions with no back-fill: every existing row is a
-- cycle nobody asked to stop, which is what two NULLs say. So an upgrade over a
-- populated database rewrites nothing, and a database that somehow records this
-- migration without the columns is repairable in place by re-running these two
-- statements -- which is what `db._cycle_stop_problems` prints.
--
-- `stop_requested_at` is deliberately not cleared when the cycle closes: a
-- journal entry has to go on saying that this night was stopped and by whom
-- long after the run that obeyed it has ended.
ALTER TABLE cycles ADD COLUMN stop_requested_at TEXT;
ALTER TABLE cycles ADD COLUMN stop_requested_by TEXT
    CONSTRAINT "a stop records who asked and when, or neither"
    CHECK ((stop_requested_by IS NULL) = (stop_requested_at IS NULL));
"""


CONVENTIONS_AND_ANNOTATIONS_DDL = """
-- The conventions space and the annotations table (Phase 5b, design §L2).
--
-- `conventions` is the gardener's own workspace: convention nodes are ordinary
-- `note` nodes living here, written by the cycle like anything else (L2). The
-- gardener holds `edit` on it **alone** — restoring `edit` on `meta` was
-- rejected because 5a's live pass proved it buys the ability to rename `main`
-- and archive the `note` type, after which a human cannot write a note. The
-- grant is an ordinary row, like 0014's: `nodum revoke builtin-gardener
-- conventions` turns the whole feature off with the command that was already
-- there.
--
-- **A space named `conventions` already existing refuses the upgrade.**
-- `idx_space_title` (0013) is unique over space titles in every state, so the
-- INSERT below would otherwise die on a bare `UNIQUE constraint failed` —
-- the exact failure shape this repo's migrations exist to refuse readably.
-- The device is 0014's reserved-prefix abort: a CHECK whose **name** is the
-- message SQLite prints. It also catches a raw node whose *id* is
-- `conventions`, which would collide on the primary key instead.
CREATE TABLE _conventions_reserved (
    taken TEXT,
    CONSTRAINT
"a node id or space title 'conventions' already exists: rename or remove it, then re-run"
    CHECK (taken IS NULL)
);
INSERT INTO _conventions_reserved (taken)
SELECT id FROM nodes
WHERE id = 'conventions' OR (type_id = 'space' AND title = 'conventions');
DROP TABLE _conventions_reserved;

INSERT INTO nodes (id, space_id, type_id, title, props, state, created_by)
VALUES ('conventions', 'meta', 'space', 'conventions', '{}', 'active', 'system');

INSERT INTO grants (agent_id, space_id, level)
VALUES ('builtin-gardener', 'conventions', 'edit');

-- The annotations table: one per queue item, saying what a proposer's
-- acceptance signal judged and at what rate (design §L1 — "one annotation per
-- queue item saying this proposer accepts at 92 % on this edge type; these two
-- signals fired"). Written by the learned-curation cycle (5b-ii), never by a
-- human; read only attached to a `ProposalOut` the store has already filtered.
--
-- It is an **exclusive arc**, this schema's own idiom (`url_tokens`): three
-- typed nullable columns with real `ON DELETE CASCADE` foreign keys and a
-- `CHECK` that exactly one is non-null. `(target_kind, target_id)` was the
-- first-cut shape and was corrected 2026-08-02 — addressing three tables does
-- not require being untyped, and the orphan cost of the untyped shape is real
-- because `service._delete_created_row` hard-deletes edges and versions on the
-- undo path. `target_kind` drops out entirely: the non-null column *is* the
-- kind. `target_version_id` is INTEGER because `versions.id` is.
--
-- Three partial unique indexes replace one composite, holding the same rule:
-- the design says *one* annotation per queue item, so re-annotating on a later
-- cycle replaces rather than accumulates.
--
-- `cycle_id` points at the cycle that wrote the night's annotations, so they
-- roll back with it — 5a's atomic rollback still has to learn this table.
CREATE TABLE annotations (
    id                TEXT PRIMARY KEY,   -- uuid4().hex, like every generated id
    target_node_id    TEXT    REFERENCES nodes(id)    ON DELETE CASCADE,
    target_edge_id    TEXT    REFERENCES edges(id)    ON DELETE CASCADE,
    target_version_id INTEGER REFERENCES versions(id) ON DELETE CASCADE,
    body              TEXT NOT NULL,      -- JSON: the rate, and which signals fired
    actor             TEXT NOT NULL,
    cycle_id          TEXT REFERENCES cycles(id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((target_node_id IS NOT NULL) + (target_edge_id IS NOT NULL)
         + (target_version_id IS NOT NULL) = 1)
);
CREATE UNIQUE INDEX idx_annotations_node
    ON annotations(target_node_id)
    WHERE target_node_id IS NOT NULL;
CREATE UNIQUE INDEX idx_annotations_edge
    ON annotations(target_edge_id)
    WHERE target_edge_id IS NOT NULL;
CREATE UNIQUE INDEX idx_annotations_version
    ON annotations(target_version_id)
    WHERE target_version_id IS NOT NULL;
"""


#: Index the two columns the failed-login lockout filters on (finding M2).
#:
#: ``service.login_failure_count`` runs on **every** password attempt, and the
#: only index on ``events`` was ``idx_events_cycle`` — so the check was a full
#: scan of the append-only log with a ``json_extract`` per row, on the one
#: route reachable without a session. The log only grows, and failed attempts
#: grow it, so the check got slower with exactly the traffic it exists to
#: throttle.
#:
#: ``(op, created_at)`` in that order: ``op`` is the equality, ``created_at``
#: the range, so the window scan happens inside the one op's rows. The
#: ``json_extract`` on the name stays a per-row test — over five rows in a
#: quarter-hour instead of the whole log.
EVENT_OP_INDEX_DDL = """
CREATE INDEX idx_events_op_created ON events(op, created_at);
"""


UNIQUE_HUMAN_NAMES_DDL = """
-- One human per login name, for good (round-6 review, finding M1).
--
-- `humans.name` is the login handle. Ids of CLI-created humans are random hex
-- and nobody types one at a prompt, so `auth.verify_login` looks an account up
-- by name — and it refuses a name that matches more than one row, deliberately,
-- because "which human is behind this session?" has no answer otherwise. Only
-- nothing stopped two accounts carrying one name: `nodum human create owner`,
-- or `POST /api/humans {"name": "owner"}`, both supported writes behind the
-- ordinary human-only gate, and HTTP login for that name was dead from the
-- moment the second row landed. Dead **for good**: the human verbs are
-- create/list/passwd/disable/enable, none of them removes or renames an
-- account, and the ambiguity check runs before the `disabled` one, so even
-- disabling the clone brought nothing back. Recovery meant hand SQL.
--
-- `service.create_human` now refuses the duplicate the way `create_agent`
-- refuses a taken agent id, and this is the constraint under that check for the
-- same reason `agents` has one: the check and the INSERT are two statements on
-- a short-lived connection of the service's own, so two concurrent creates can
-- both read the name free and both write it. A rule a *reader* depends on
-- belongs in the schema, not only in the writer that happens to be in front of
-- it today.
--
-- What the index covers: `name`, exactly and always.
--
--   * Exact, never case-folded. `verify_login` compares `name = ?` under
--     SQLite's default BINARY collation, so `Owner` and `owner` are two names
--     that tell themselves apart perfectly, and a NOCASE index would refuse a
--     pair the lookup handles. The constraint is exactly as tight as the lookup
--     — 0013's rule for space titles, for 0013's reason.
--   * No state predicate. A disabled account keeps its name: `enable_human` is
--     supported, so a name freed by a disable would collide the instant the
--     account came back — the mistake 0013 had to correct in itself. And
--     `humans.name` is NOT NULL, so unlike a space title nothing sits outside
--     the index at all.
--
-- Unlike a space title, a login name is *not* also an id: `verify_login`
-- matches `name = ?` and nothing else, and no surface resolves a human by "id
-- or name". 0013's third property — the title that is another row's id — has no
-- analogue here, so ids are not part of the dedupe.
--
-- Dedupe first, or this fails with a bare IntegrityError on every database a
-- duplicate was already created on, rolling the upgrade back and leaving the
-- login exactly as dead as it found it. That is 0009's lesson, applied by 0013
-- and applied again here — with 0013's tie-break rewritten for what a *login*
-- name is for:
--
--   1. **The account the name is worth something to keeps it.** Enabled before
--      disabled, then password-holding before passwordless, then oldest
--      (`created_at`, `rowid`). A disabled account cannot log in at all and a
--      passwordless one cannot log in over HTTP, so reserving the handle for
--      either is reserving it for nobody. This preserves no live resolution —
--      there is none to preserve, which is the whole defect: today the shared
--      name resolves to no account whatever. It picks the row that can use it.
--      In the case this was found on the seeded `owner` wins on both keys at
--      once: it holds the password, and the clone was created passwordless.
--   2. **The rename cannot itself collide.** `<name> (<id>)` is unique among
--      losers because ids are, but a database can already hold a human
--      literally called `owner (owner)` — and then the dedupe would generate
--      that very string and the index would refuse it, which is the failure
--      this migration exists to prevent. So the free name is searched, not
--      assumed: the base, then `<base> 1`, `<base> 2`, … until one is free of
--      every name that survives the statement. 0013's argument that two losers
--      can never land on one string holds unchanged — distinct ids make the
--      bases distinct, a base always ends in `)` while a suffixed name always
--      ends in a digit, and two suffixed names split at that trailing digit.
--
-- The losers are **renamed, not disabled**: a duplicate name is no reason to
-- take an account away from whoever owns it. Every loser stays administrable by
-- id (`nodum human passwd <id>`, disable, enable), keeps its sessions, its
-- agents and its history, and can log in under the name it comes out with.
-- Names change here with no event and no version, as every migration's data
-- repair does.
--
-- One case is left standing knowingly: a loser whose name was already at
-- `service.MAX_HUMAN_NAME_LENGTH` comes out longer than the cap `POST
-- /api/login` refuses a claimed name above, and no verb shortens it again. It
-- costs that row nothing it had — a name shared by two accounts could not be
-- logged in with before this ran either — and truncating to make room would
-- throw away the name the rename exists to keep legible.
WITH RECURSIVE
ranked AS (
    SELECT rowid AS rid, id, name, ROW_NUMBER() OVER (
        PARTITION BY name
        ORDER BY disabled, credential_hash IS NULL, created_at, rowid
    ) AS rank
    FROM humans
),
losers AS (
    SELECT rid, name || ' (' || id || ')' AS base FROM ranked WHERE rank > 1
),
keepers AS (
    SELECT name FROM ranked WHERE rank = 1
),
candidates(rid, base, suffix, name) AS (
    SELECT rid, base, 0, base FROM losers
    UNION ALL
    SELECT rid, base, suffix + 1, base || ' ' || (suffix + 1)
    FROM candidates
    WHERE name IN (SELECT name FROM keepers)
),
resolved AS (
    SELECT rid, name FROM candidates WHERE name NOT IN (SELECT name FROM keepers)
)
UPDATE humans
SET name = (SELECT name FROM resolved WHERE resolved.rid = humans.rowid)
WHERE rowid IN (SELECT rid FROM resolved);

CREATE UNIQUE INDEX idx_humans_name ON humans(name);
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
    ("0013_unique_space_titles", UNIQUE_SPACE_TITLES_DDL),
    ("0014_cycles_and_gardener", CYCLES_AND_GARDENER_DDL),
    ("0015_cycle_stop_switch", CYCLE_STOP_SWITCH_DDL),
    ("0016_conventions_and_annotations", CONVENTIONS_AND_ANNOTATIONS_DDL),
    ("0017_projector_skips", PROJECTOR_SKIPS_DDL),
    ("0018_events_op_index", EVENT_OP_INDEX_DDL),
    ("0019_unique_human_names", UNIQUE_HUMAN_NAMES_DDL),
]
