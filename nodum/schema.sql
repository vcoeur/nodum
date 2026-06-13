-- nodum typed-graph schema. Idempotent. One nodes / one edges table; each row's
-- `kind` references a kind, mirrored into the node_kinds / edge_kinds tables for a
-- cheap hard FK. Those tables also carry the kind's `spec` (its field shape /
-- endpoint signature) as JSONB, so the schema is data — editable at runtime, not
-- frozen in code. Every node carries a plain-text `content` column (the body that
-- FTS ranks and that later embeddings will target). No embeddings yet.

CREATE TABLE IF NOT EXISTS node_kinds (
    name TEXT PRIMARY KEY,
    spec JSONB NOT NULL                                  -- {group, content_label, fields}
);
CREATE TABLE IF NOT EXISTS edge_kinds (
    name TEXT PRIMARY KEY,
    spec JSONB NOT NULL                                  -- {from, to, symmetric, fields}
);

CREATE TABLE IF NOT EXISTS nodes (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL REFERENCES node_kinds(name),
    content    TEXT NOT NULL,                            -- the embeddable plain-text body
    data       JSONB NOT NULL DEFAULT '{}',             -- kind-specific metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind     ON nodes (kind);
CREATE INDEX IF NOT EXISTS idx_nodes_data_gin ON nodes USING gin (data);
CREATE INDEX IF NOT EXISTS idx_nodes_fts
    ON nodes USING gin (to_tsvector('english', content));

CREATE TABLE IF NOT EXISTS edges (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL REFERENCES edge_kinds(name),
    from_uuid  UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    to_uuid    UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    data       JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_uuid <> to_uuid)
);
CREATE INDEX IF NOT EXISTS idx_edges_kind     ON edges (kind);
CREATE INDEX IF NOT EXISTS idx_edges_from     ON edges (from_uuid);
CREATE INDEX IF NOT EXISTS idx_edges_to       ON edges (to_uuid);
CREATE INDEX IF NOT EXISTS idx_edges_data_gin ON edges USING gin (data);

-- Single-row table holding the one "main password" (argon2 hash) and the random
-- signing_key used to sign session tokens. The `id` CHECK pins it to one row.
-- Empty until `nodum auth set-password` writes it; the install stays locked
-- until then. See nodum.auth.
CREATE TABLE IF NOT EXISTS auth_secret (
    id            BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    password_hash TEXT NOT NULL,
    signing_key   TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
