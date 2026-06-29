-- ContextOS initial schema + Row-Level Security backstop (ADR-0002).
--
-- The application firewall scopes every query; these policies make a missing scope harmless.
-- We use FORCE ROW LEVEL SECURITY so the policies apply even to the table owner, and bind the
-- active tenant via the `app.tenant_id` GUC (set with SET LOCAL per transaction by the app).

CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector: vectors co-located with relational rows

-- ---------------------------------------------------------------------------
-- memories: long-term / episodic / semantic tiers (working/short-term live in Redis)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    namespace        TEXT NOT NULL,
    tier             TEXT NOT NULL,
    content          TEXT NOT NULL,
    importance       REAL NOT NULL DEFAULT 0.5,
    embedding        vector(384),             -- bge-small-en-v1.5; NULL until embedded async
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    provenance       JSONB NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS memories_tenant_ns_idx ON memories (tenant_id, namespace);
CREATE INDEX IF NOT EXISTS memories_hnsw_idx ON memories USING hnsw (embedding vector_cosine_ops);
-- BM25-ish lexical retrieval via tsvector; fused with ANN by RRF in the memory engine.
CREATE INDEX IF NOT EXISTS memories_fts_idx ON memories USING gin (to_tsvector('english', content));

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;

-- Tenant isolation: a row is visible only when its tenant_id matches the bound GUC.
CREATE POLICY memories_tenant_isolation ON memories
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- ---------------------------------------------------------------------------
-- trace_spans: best-effort, sampled (fail-open). Cost records live in a separate
-- fail-closed outbox table (C12) so billing integrity is never traded for telemetry.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trace_spans (
    trace_id   TEXT NOT NULL,
    span_id    TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    stage      TEXT NOT NULL,
    name       TEXT NOT NULL,
    start_ts   TIMESTAMPTZ NOT NULL,
    end_ts     TIMESTAMPTZ,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    decision   JSONB,
    -- Partitioned tables require the partition key (start_ts) in every unique/primary key.
    PRIMARY KEY (trace_id, span_id, start_ts)
) PARTITION BY RANGE (start_ts);     -- time-partitioned to bound trace write-amplification

ALTER TABLE trace_spans ENABLE ROW LEVEL SECURITY;
ALTER TABLE trace_spans FORCE ROW LEVEL SECURITY;
CREATE POLICY trace_tenant_isolation ON trace_spans
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE TABLE IF NOT EXISTS cost_ledger (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    trace_id     TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd     NUMERIC(12, 6) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE cost_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY cost_tenant_isolation ON cost_ledger
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
