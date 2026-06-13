-- nodum schema — a mutable JSONB graph of nodes and UUID-keyed edges.
-- Idempotent: safe to run on every start-up. No embeddings table in the MVP.

CREATE TABLE IF NOT EXISTS nodes (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    data       JSONB NOT NULL,                       -- content + metadata together
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (data ? 'text')                            -- every node has a primary text field
);
CREATE INDEX IF NOT EXISTS idx_nodes_data_gin ON nodes USING gin (data);
CREATE INDEX IF NOT EXISTS idx_nodes_fts
    ON nodes USING gin (to_tsvector('english', data ->> 'text'));

CREATE TABLE IF NOT EXISTS edges (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),   -- addressable, merge-friendly
    from_uuid  UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    to_uuid    UUID NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
    data       JSONB NOT NULL DEFAULT '{}',          -- {type, weight, explanation, ...}
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_uuid <> to_uuid)
);
CREATE INDEX IF NOT EXISTS idx_edges_from     ON edges (from_uuid);
CREATE INDEX IF NOT EXISTS idx_edges_to       ON edges (to_uuid);
CREATE INDEX IF NOT EXISTS idx_edges_data_gin ON edges USING gin (data);
