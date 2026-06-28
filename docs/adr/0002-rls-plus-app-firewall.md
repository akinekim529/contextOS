# ADR-0002: Row-Level Tenant Isolation via `tenant_id` + Postgres FORCE RLS Behind an Application RBAC Firewall

## Status

Accepted.

## Context

ContextOS is multi-tenant middleware. Every byte of memory, every cache entry, every retrieval candidate, every replay bundle belongs to exactly one tenant, and the canonical guarantee is **cross-tenant leakage = 0**, validated in CI by **≥ 10,000 hostile second-tenant property probes** as a hard merge gate.

Multi-tenancy can be enforced at four layers, and the choice is load-bearing:

1. **Database-per-tenant** — one Postgres instance/cluster per tenant.
2. **Schema-per-tenant** — one Postgres schema (namespace) per tenant in a shared database.
3. **Application-only filtering** — a shared schema, every query carries `WHERE tenant_id = $1` in application code.
4. **Row-level security (RLS)** — a shared schema with `tenant_id` on every row, isolation enforced by the *database* via policies, independent of whether the app remembered the filter.

The failure mode we most fear is not an external attacker; it is **one missing `WHERE tenant_id`** in one query path, on one feature branch, under deadline. Application-only filtering makes correctness depend on every developer remembering every time. That is not a guarantee; it is a hope. We need a guarantee the database enforces even when the application is wrong.

We also operate at launch scale (`<= 5M vectors/tenant`) with co-located pgvector and a two-tier cache. The isolation mechanism must not fragment connection pooling, must compose with pgvector HNSW (18 ms p95 ANN probe is the single source of truth), and must support the crypto-shred / RTBF path (ADR-0011 territory) without a per-tenant migration ceremony.

## Decision

**Tenant isolation is defense-in-depth: an application RBAC firewall as the first gate, and `tenant_id` + Postgres `FORCE ROW LEVEL SECURITY` as the database-enforced backstop. A shared schema with `tenant_id` as a non-null partition key on every row, object, and key. The RLS policy is `FORCE`d so that even the table owner cannot bypass it.**

### Layer 1 — Application RBAC firewall (first, fail-closed)

At the edge, `auth/tenant` resolution (5 ms p95 in the latency table) establishes the authenticated principal and tenant. Before any repository call, the RBAC firewall evaluates `check(principal, resource, action)` against the static policy. This is where within-tenant namespace (project/agent/user) is enforced as a **hard, fail-closed filter at the repository boundary**, evaluated together with `tenant_id`; **missing or ambiguous namespace = deny** (consistency rule C2). The firewall is the single authority for `route`/`read`/`write`/`delete`/`admin`/`cache_read` actions.

### Layer 2 — Postgres FORCE RLS (backstop, database-enforced)

Every tenant-scoped table is created with RLS enabled *and forced*. A transaction-local GUC carries the tenant; the policy reads it. The app **cannot** see another tenant's rows even if a query forgets the filter, even if the connection is the table owner.

```sql
-- Every tenant-scoped table follows this template.
CREATE TABLE memory_item (
    id          TEXT        NOT NULL,            -- ULID
    tenant_id   TEXT        NOT NULL,            -- non-null partition key on EVERY row
    namespace   TEXT        NOT NULL,            -- project/agent/user (C2 hard filter)
    created_at  TIMESTAMPTZ NOT NULL,            -- RFC-3339 UTC
    embedding   VECTOR(384),                     -- bge-small-en-v1.5
    payload_enc BYTEA       NOT NULL,            -- per-subject DEK envelope (crypto-shred scope)
    PRIMARY KEY (tenant_id, id)
) PARTITION BY LIST (tenant_id);                 -- partition key = tenant_id

ALTER TABLE memory_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_item FORCE  ROW LEVEL SECURITY;   -- owner is NOT exempt — this is the point

CREATE POLICY tenant_isolation ON memory_item
    USING      (tenant_id = current_setting('contextos.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('contextos.tenant_id', true));
```

The tenant GUC is set **transaction-locally** at the start of every request's DB work, immediately after edge auth:

```python
async def with_tenant(conn, tenant_id: str):
    # SET LOCAL is transaction-scoped: it cannot leak to the next checkout of a pooled conn.
    await conn.execute("SET LOCAL contextos.tenant_id = $1", tenant_id)
    # All subsequent statements on this txn are filtered by the RLS policy.
```

`SET LOCAL` (not `SET`) is mandatory: it is bound to the transaction, so a pooled connection returned to the pool carries no residual tenant. The `true` second argument to `current_setting` makes a *missing* GUC return NULL rather than error — and because the policy compares `tenant_id = NULL` (which is never true), a query that forgot to set the tenant returns **zero rows**: fail-closed by construction.

### Composition with the rest of the architecture

- **pgvector** co-locates in the same Postgres, so the *same* RLS policy guards HNSW ANN queries — the 18 ms p95 probe is already tenant-scoped with no extra app logic.
- **Vector payload + id are encrypted** under the per-subject DEK (crypto-shred scope), so RLS guards *visibility* and crypto-shred guards *destruction* — orthogonal, complementary.
- **Cache** (Redis exact-tier + pgvector/Qdrant semantic-tier) is per-tenant **namespaced** by key prefix `{tenant_id}:...`; the semantic tier in pgvector inherits the same RLS policy.
- **Connection pooling** is preserved: one shared pool, tenant set per-transaction via `SET LOCAL`. We do **not** fragment into per-tenant pools.

### CI hard gate

A property-based test fixture spins up two tenants, writes known rows to each, then fires **≥ 10,000 hostile probes** as tenant B attempting to read/scan/aggregate/vector-search tenant A's data across every repository method, *including* deliberately tenant-blind queries (no `WHERE tenant_id`) to prove the RLS backstop, not just the app filter. Any non-empty cross-tenant result fails the build. This gate proves *both* layers independently.

## Consequences

**Positive**

- A forgotten `WHERE tenant_id` in application code leaks nothing: RLS returns zero rows. The guarantee is enforced by the database, not by developer discipline.
- One schema, one migration path, one connection pool, one set of indexes (including HNSW) — operationally simple at `<= 5M vectors/tenant`.
- `FORCE` closes the table-owner bypass that plain `ENABLE` leaves open — the most common RLS misconfiguration.
- Defense-in-depth: an attacker must defeat the app RBAC firewall *and* the database RLS policy *and* break per-subject DEK encryption. Independent failure domains.

**Negative / costs**

- Every transaction must `SET LOCAL` the tenant GUC. A code path that opens a raw connection and forgets is *safe by default* (returns nothing) but functionally broken — caught immediately by tests, never a silent leak.
- RLS adds a per-row policy predicate. On HNSW scans this is a cheap equality on an indexed/partition-keyed column; the 18 ms p95 already accounts for it. We measure, we do not assume.
- LIST partitioning by `tenant_id` requires partition management (create-on-tenant-onboard); we automate this in the tenant-provisioning job.

**Operational**

- Tenant onboarding creates a partition + DEK; offboarding (RTBF) drops the partition and shreds the DEK — both idempotent.

## Rejected alternatives

| Alternative | Why it fails |
|---|---|
| **Application-only filtering (`WHERE tenant_id = $1` in code, no RLS)** | Correctness depends on every query in every branch forever including the filter. One omission = a cross-tenant leak with no backstop. It cannot satisfy a *0-leakage* guarantee proven by hostile tenant-blind probes, because there is nothing beneath the app to stop a blind query. Rejected as the *sole* mechanism; we keep app filtering as Layer 1 *on top of* RLS. |
| **Schema-per-tenant** | N schemas means N copies of every table, index, and crucially N HNSW indexes — index build/maintenance cost scales with tenant count, search planning fragments, and shared connection pooling breaks (each connection must `search_path` to a tenant). Migrations become an N-way fan-out. Cross-tenant analytics and the single replay-log abstraction get painful. Isolation is real but the operational cost is unjustified at our scale, and a leaked `search_path` is as silent as a missing `WHERE`. |
| **Database-per-tenant** | Strongest isolation, worst economics: connection-pool explosion, per-tenant backup/upgrade/HA, no shared HNSW, and pgvector co-location benefits evaporate. At 5M vectors/tenant across many tenants this is operationally and financially infeasible for middleware that must stay lightweight. Reserved only for a future "dedicated-instance" enterprise tier, not the default. |
| **`ENABLE ROW LEVEL SECURITY` without `FORCE`** | The table owner (which the migration/runtime role often effectively is) bypasses non-forced RLS entirely. This is the single most common RLS footgun and would make the policy decorative for the very role that runs the app. `FORCE` is non-negotiable. |
| **Session-level `SET` (not `SET LOCAL`) for the tenant GUC** | Session GUCs persist across the connection's life; a pooled connection returned and re-checked out by a different tenant's request would carry the previous tenant — a direct cross-tenant leak. `SET LOCAL` binds to the transaction and is reset on commit/rollback. |
| **Application-level encryption as the *isolation* boundary** | Per-subject DEK encryption is for crypto-shred/RTBF (destruction), not for query-time isolation. Using it as the access boundary would force decrypt-then-filter on every read, defeating HNSW and the 18 ms probe. Encryption and RLS are complementary, not substitutes. |

## Cross-section assumptions

- `tenant_id` is a non-null partition key on **every** row/object/key (canonical fact); other sections (cache namespacing, replay bundles, memory tiers) must key on it identically.
- Within-tenant namespace (project/agent/user) is the C2 hard, fail-closed filter at the repository boundary and is evaluated *with* `tenant_id`; the RBAC firewall in this ADR is the same `check(principal, resource, action)` authority that ADR-0006 (route action) and the router rely on.
- Vector payload/id encryption under the per-subject DEK is the same envelope the RTBF/crypto-shred path (C11) destroys; embeddings are within crypto-shred scope.
- The 18 ms p95 pgvector HNSW probe already includes the RLS predicate cost; no section may quote a different vector-query latency.
