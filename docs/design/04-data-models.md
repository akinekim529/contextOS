# 04 — Data Models

This section defines the five typed schemas that flow across every ContextOS boundary, the shared primitives every schema inherits, and the wire-level invariants that make the system **replay-grade** and **multi-tenant-safe**. These types are the contract: the Memory Engine, Context Assembler, RBAC firewall, two-tier cache, and the OTel-compatible observability plane all serialize and deserialize the same definitions. There is no second, "internal-only" representation — a deliberate choice (see [Schema-of-record](#001-schema-of-record-pydantic-v2-not-protobuf-first-not-orm-first)).

All schemas are authored as **Pydantic v2** classes (`pydantic>=2.6`). Pydantic v2 is the chosen validation/serialization layer because its `pydantic-core` Rust backend gives us ~5-20x faster validation than v1, native `model_dump(mode="json")` RFC-3339 emission, and `model_config = ConfigDict(extra="forbid")` to fail-closed on unknown fields at every edge. Each schema below ships with the class **and** a realistic example JSON payload — field lists alone are not a contract.

> **Locked-architecture reminders applied here (do not drift):** IDs are **ULID**; timestamps are **RFC-3339 UTC**; `tenant_id` is a **non-null partition key on every row, object, and cache key**. The vector itself never lives inside `MemoryObject` — the object holds an `embedding_ref` **indirection** to the `VectorStore` (pgvector/HNSW at launch, Qdrant escape hatch at scale). Per **C11**, the vector payload and id in the store are **encrypted under the per-subject DEK**, so crypto-shred of the DEK tombstones the embedding alongside the memory.

---

## 0. Design decisions for the data layer

### 0.0.1 Schema-of-record: Pydantic v2, not protobuf-first, not ORM-first

| Option | Verdict | Why |
| --- | --- | --- |
| **Pydantic v2 as schema-of-record** | **CHOSEN** | One definition validates HTTP edge (FastAPI native), serializes to JSON for REST/replay bundles, and round-trips through Postgres `JSONB` columns. `pydantic-core` keeps validation off the critical-path budget. |
| protobuf/`.proto` first, generate Python | Rejected (for now) | We are **REST/JSON at the edge** by locked decision; gRPC arrives only *after* internal boundaries prove out. Generating Pydantic from proto today buys schema rigidity we do not yet need and a build-step tax. We keep the door open: every schema here maps 1:1 to a future proto message. |
| SQLAlchemy ORM model as the source of truth | Rejected | An ORM model couples the wire contract to table layout. The `MemoryObject` that crosses the API and lands in a replay bundle must be **storage-agnostic** — the same object exists with its vector in pgvector *or* Qdrant. ORM-first would leak `embedding vector(384)` columns into the public type. |
| `dataclasses` + hand-rolled validators | Rejected | No declarative JSON-schema export, no fail-closed `extra="forbid"`, no Rust-speed coercion. We would rebuild a worse Pydantic. |

### 0.0.2 The five schemas and where they live

| Schema | Owning subsystem | Persisted in | Crosses replay boundary? |
| --- | --- | --- | --- |
| `MemoryObject` | Memory Engine | Postgres 16 (`memory` table, RLS) + vector in `VectorStore` | Yes — input candidates |
| `ContextBlock` | Context Assembler | Replay bundle only (derived; not a base table) | Yes — the assembly decision |
| `RBACPolicy` | RBAC firewall | Postgres 16 (`rbac_policy`, RLS) | Yes — the ACL decision record |
| `CacheEntry` | Two-tier cache | Redis (exact tier) + pgvector/Qdrant (semantic tier) | Yes — cache hit/miss decision |
| `TraceSpan` | Observability plane | Redis Streams replay log -> tiered store | Yes — the correlation spine |

---

## 1. Shared primitives

Every schema composes these. They are defined once and imported everywhere — drift here is drift everywhere.

### 1.1 ULID identifiers

All ids are **ULID** (`01J...`, 26-char Crockford base32), not UUIDv4 and not auto-increment integers.

| Option | Verdict | Why |
| --- | --- | --- |
| **ULID** | **CHOSEN** | Lexicographically sortable by creation time (the first 48 bits are an RFC-3339-aligned ms timestamp), so a `WHERE id > :cursor ORDER BY id` keyset scan replaces `ORDER BY created_at` — critical for the replay log and for B-tree locality in Postgres. URL-safe, 128-bit collision space. |
| UUIDv4 | Rejected | Random high bits destroy index locality; inserts scatter across the B-tree, inflating write amplification on the hot `memory` and `trace_span` tables. |
| Auto-increment `bigserial` | Rejected | Leaks volume across tenants (an attacker counting ids), and a global sequence is a cross-tenant coordination point that violates partition isolation. |
| UUIDv7 | Acknowledged near-equivalent, not chosen | Time-sortable like ULID, but ULID's Crockford-base32 string is friendlier in logs/URLs and our tooling already speaks it. We treat UUIDv7 as a drop-in if a dependency forces it. |

```python
from typing import Annotated
from pydantic import AfterValidator

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def _validate_ulid(v: str) -> str:
    if len(v) != 26:
        raise ValueError("ULID must be 26 chars")
    if any(c not in _CROCKFORD for c in v.upper()):
        raise ValueError("ULID must be Crockford base32")
    return v.upper()

ULID = Annotated[str, AfterValidator(_validate_ulid)]
```

### 1.2 RFC-3339 UTC timestamps

All timestamps are `datetime` constrained to **UTC** and serialized as **RFC-3339** with a `Z` offset. We reject naive datetimes (fail-closed) because a tz-naive timestamp in a replay bundle is non-deterministic across hosts.

```python
from datetime import datetime, timezone
from typing import Annotated
from pydantic import AfterValidator, PlainSerializer

def _require_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return dt.astimezone(timezone.utc)

UtcTimestamp = Annotated[
    datetime,
    AfterValidator(_require_utc),
    # Force the trailing 'Z' form; isoformat() would emit '+00:00'.
    PlainSerializer(
        lambda dt: dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        return_type=str,
    ),
]
```

`timespec="milliseconds"` is deliberate: replay equality (C7) compares serialized stage records, so we pin to millisecond precision rather than let host clocks emit variable microsecond tails that would break byte-exact diffing for no semantic reason.

### 1.3 Tenant + namespace + scope (the isolation triad)

`tenant_id` is the outermost partition key and is **non-null everywhere**. Within a tenant, a **namespace** (`project / agent / user`) is a **HARD filter, fail-closed** (**C2**): it is evaluated at the repository boundary against `tenant_id`, and a missing or ambiguous namespace is a **deny**, never a wildcard. Cross-org sharing is *opt-in only* via an `RBACPolicy` rule on a shared-org namespace.

```python
from enum import Enum
from typing import Annotated
from pydantic import StringConstraints, BaseModel, ConfigDict, Field, computed_field

TenantId = Annotated[str, StringConstraints(min_length=1, max_length=64,
                                            pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]

class NamespaceKind(str, Enum):
    PROJECT = "project"
    AGENT = "agent"
    USER = "user"
    SHARED_ORG = "shared_org"   # opt-in; requires an explicit allow RBACPolicy rule

class Namespace(BaseModel):
    """Within-tenant hard filter. Evaluated at the repository boundary WITH tenant_id.
    Missing/ambiguous namespace => deny (C2)."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: NamespaceKind
    key: Annotated[str, StringConstraints(min_length=1, max_length=128)]

    def as_filter(self) -> str:
        # The literal SQL/predicate token the repository AND-s with tenant_id.
        return f"{self.kind.value}:{self.key}"
```

**Access scope** is the visibility band attached to retrievable units (`MemoryObject`, `ContextBlock`). It is enforced by the ACL/redaction stage **before compression** (pipeline invariant: `... -> ACL/redaction -> compression -> ...`), so compression can never re-expose a redacted span.

```python
class AccessScope(str, Enum):
    PRIVATE = "private"        # subject/user only
    PROJECT = "project"        # all principals in the project namespace
    TENANT = "tenant"          # any principal in the tenant
    SHARED_ORG = "shared_org"  # cross-tenant, gated by an allow RBACPolicy rule
```

> **Rejected alternative (AccessScope 4-band enum vs free-form ACL tags):** a free-form `set[str]` of ACL tags per object was rejected — it is not statically analyzable (no compiler/CI can prove the set of reachable scopes), it cannot be mapped onto a Postgres RLS `USING` predicate without a runtime join against an arbitrary tag table, and an unbounded tag vocabulary defeats the `>= 10k` hostile-probe leak gate because the prober cannot enumerate the band space to fuzz it exhaustively. The 4-band enum is finite and totally ordered (`private < project < tenant < shared_org`), so each band lowers to one deterministic RLS clause, the ACL/redaction stage can decide visibility with a single integer compare, and CI can exhaustively assert all four bands against a hostile second tenant.

### 1.4 Provenance

Every memory and every assembled block carries `Provenance` so the Replay Debugger can answer "*why was this byte in the prompt?*". Provenance is immutable once written.

```python
class ProvenanceKind(str, Enum):
    USER_MESSAGE = "user_message"
    TOOL_OUTPUT = "tool_output"
    DOCUMENT_INGEST = "document_ingest"
    CONSOLIDATION = "consolidation"   # produced by the async, cost-tracked batch job
    SYSTEM_FACT = "system_fact"

class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ProvenanceKind
    source_ref: str = Field(description="ULID of the upstream message/doc/tool-call, or external URI")
    ingested_at: UtcTimestamp
    # If this object was synthesized by memory consolidation, the cost it added to
    # the budget ledger is recorded here (consolidation inference cost enters the ledger).
    consolidation_cost_usd: float | None = Field(default=None, ge=0.0)
    pipeline_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")] = "1.0.0"
```

`consolidation_cost_usd` makes the scope-boundary invariant *visible in the data model*: memory consolidation is **not** an agent loop; it is an async, rate-limited, **cost-tracked** batch job whose inference cost enters the budget ledger. A provenance row with `kind="consolidation"` and a null cost is a validation-rejectable inconsistency at the ledger boundary.

### 1.5 `embedding_ref` — the vector indirection (and C11 crypto-shred)

A `MemoryObject` **never inlines its vector**. It holds an `EmbeddingRef` pointing into the active `VectorStore`. This is the single most important storage decision in this section:

- The **object** (text, importance, provenance) lives in Postgres under RLS.
- The **vector** lives in pgvector (HNSW, co-located in Postgres at launch) or Qdrant (escape hatch beyond ~5M vectors/tenant), behind the `VectorStore` adapter.
- Per **C11**, the vector **payload and id are encrypted under the per-subject DEK**. RTBF is a `tombstone + idempotent GC sweep`, and **crypto-shredding the DEK renders the vector unrecoverable** without sweeping every HNSW node synchronously. Embeddings are therefore *within* crypto-shred scope, not a forgotten copy.

> **Rejected alternative (Qdrant as the escape hatch vs Milvus / Weaviate / FAISS):** the at-scale backend is **Qdrant**, not Milvus, Weaviate, or FAISS. FAISS was rejected because it is an in-process library with no native multi-tenant collections, no RLS-equivalent isolation, and no out-of-process crypto-at-rest — it cannot host the per-subject-DEK-encrypted payloads C11 requires. Milvus was rejected for operational weight: it pulls in etcd + Pulsar/Kafka + MinIO as mandatory dependencies, a far heavier control plane than a single Qdrant binary for the modest per-tenant collections we escape to. Weaviate was rejected because its strong differentiator is built-in vectorization modules we do not use (we own the `EmbeddingProvider`), so we would pay its module surface area for nothing. The crossover that triggers the migration off pgvector is **~5M vectors/tenant**: beyond that point HNSW index build + co-located query load contends with relational OLTP on the same Postgres node, so the vector workload is moved to a dedicated Qdrant cluster while `EmbeddingRef.backend` flips per-collection (the `MemoryObject` contract is unchanged — that is the whole point of the indirection).

```python
class VectorBackend(str, Enum):
    PGVECTOR = "pgvector"   # launch default, HNSW, <=5M vectors/tenant
    QDRANT = "qdrant"       # escape hatch beyond ~5M vectors/tenant (vs Milvus/Weaviate/FAISS — see note above)

class EmbeddingRef(BaseModel):
    """Indirection to the vector store. The vector lives there, encrypted under the
    per-subject DEK (C11). This object holds ONLY a pointer + crypto metadata."""
    model_config = ConfigDict(extra="forbid")
    backend: VectorBackend = VectorBackend.PGVECTOR
    collection: str = Field(description="tenant-namespaced collection/table, e.g. 'mem__acme'")
    vector_id: ULID = Field(description="ULID of the encrypted vector row; equals MemoryObject.id")
    dim: int = Field(default=384, description="BAAI/bge-small-en-v1.5 dimensionality")
    model_id: str = Field(default="BAAI/bge-small-en-v1.5")
    # C11: the DEK under which the vector payload+id are encrypted in the store.
    # Crypto-shredding this key tombstones the embedding for RTBF.
    dek_id: ULID = Field(description="per-subject Data Encryption Key id")
    encrypted: bool = Field(default=True)
```

> **Cross-section assumption (Memory Engine + Security must match):** the encrypted vector row's id in the store is **equal to `MemoryObject.id`** (`vector_id == id`). This 1:1 binding is what lets the GC sweep and the crypto-shred operate on a single ULID without a join. The DEK is **per-subject** (per end-user/data-subject), not per-tenant, so RTBF for one user does not shred a co-tenant's vectors.

The default embedder is **BAAI/bge-small-en-v1.5 (384-dim)**, self-hosted behind the `EmbeddingProvider` (in-process CPU query embedding ~6 ms p95; fail-open path is BM25-only with bounded recall loss <= 12%). The cross-encoder reranker is **opt-in, out-of-band only** and never writes back into `embedding_ref`.

---

## 2. `MemoryObject`

The unit the Memory Engine stores and returns. **C1 applies:** the Memory Engine returns candidates carrying **raw per-modality scores only** — it does *not* finalize ranking. Final ranking + budget packing is the **Context Assembler's** sole authority. So `MemoryObject` carries `importance` and `last_accessed` (memory-decay/recency inputs) but **no final rank** — recency decay is orthogonal to the assembler's lost-in-the-middle ordering.

> **Rejected alternative (5-value tier taxonomy vs a collapsed 3-tier model):** a coarser `{working, long_term, semantic}` 3-tier model was rejected because the five tiers carry **distinct eviction / decay / consolidation provenance** that a 3-tier collapse would erase. `working` and `short_term` are both Redis-TTL but differ in lifetime and eviction trigger (turn-scoped vs session-scoped), so merging them loses the per-turn eviction signal. `episodic` and `semantic` are both Postgres+pgvector but differ in **consolidation provenance**: `episodic` rows are time-stamped event memories that the async consolidation job reads, whereas `semantic` rows are the *output* of that job (`Provenance.kind == "consolidation"`). Collapsing episodic into semantic would make a consolidated fact indistinguishable from its source episode, breaking the Replay Debugger's "why was this byte here?" lineage and the recency-decay eviction math (which applies to episodic but not to derived semantic facts). Five tiers stay because each names a different (datastore, eviction rule, decay applicability, consolidation role) tuple.

```python
class MemoryTier(str, Enum):
    # working/short-term => Redis (TTL); long-term/episodic/semantic => Postgres+pgvector
    WORKING = "working"        # Redis, TTL
    SHORT_TERM = "short_term"  # Redis, TTL
    LONG_TERM = "long_term"    # Postgres + pgvector
    EPISODIC = "episodic"      # Postgres + pgvector
    SEMANTIC = "semantic"      # Postgres + pgvector

class RawScores(BaseModel):
    """C1: RAW per-modality scores ONLY. The Assembler owns the single weight
    vocabulary that fuses these. Memory NEVER emits a fused final rank."""
    model_config = ConfigDict(extra="forbid")
    vector_similarity: float | None = Field(default=None, ge=-1.0, le=1.0,
                                             description="cosine sim from pgvector ANN")
    bm25: float | None = Field(default=None, ge=0.0,
                               description="lexical BM25 score (fail-open retrieval path)")
    rrf: float | None = Field(default=None, ge=0.0,
                              description="reciprocal-rank-fusion of vector || BM25")

class MemoryObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- identity & isolation (non-null tenant_id everywhere) ---
    id: ULID
    tenant_id: TenantId
    namespace: Namespace                      # hard filter, fail-closed (C2)
    access_scope: AccessScope = AccessScope.PRIVATE

    # --- payload ---
    tier: MemoryTier
    content: Annotated[str, StringConstraints(min_length=1, max_length=32_768)]
    embedding_ref: EmbeddingRef               # INDIRECTION — vector lives in the store

    # --- ranking INPUTS (raw; assembler decides, per C1) ---
    importance: float = Field(ge=0.0, le=1.0,
                              description="durable salience; an input, not a final rank")
    raw_scores: RawScores = Field(default_factory=RawScores)

    # --- temporal (RFC-3339 UTC) ---
    created_at: UtcTimestamp
    last_accessed: UtcTimestamp               # drives memory-decay recency (orthogonal to assembler order)

    # --- lineage ---
    provenance: Provenance
```

### 2.1 Recency decay (defined, not hand-waved)

`last_accessed` feeds an exponential recency multiplier the Memory Engine applies to `importance` **only for tier eviction and candidate pre-selection** — never as a final rank. Half-life is **7 days**:

```python
import math
RECENCY_HALF_LIFE_S = 7 * 24 * 3600  # 604_800 s

def recency_weight(now: datetime, last_accessed: datetime) -> float:
    age_s = max(0.0, (now - last_accessed).total_seconds())
    return 0.5 ** (age_s / RECENCY_HALF_LIFE_S)   # 1.0 at age 0, 0.5 at 7d, 0.25 at 14d
```

This decay decides *which* candidates (out of the hard cap **<= 512**) reach the Assembler; it does **not** decide their final in-prompt position. Lost-in-the-middle edge-placement is the Assembler's job (Section 06). Two separate concerns, one weight vocabulary — exactly C1.

### 2.2 Example payload

```json
{
  "id": "01J9Z3Q8K7P4R2VN5C9XB6TY1A",
  "tenant_id": "acme-corp",
  "namespace": { "kind": "project", "key": "billing-assistant" },
  "access_scope": "project",
  "tier": "semantic",
  "content": "Customer ACME prefers invoices in EUR and net-30 terms; contact is finance@acme.example.",
  "embedding_ref": {
    "backend": "pgvector",
    "collection": "mem__acme-corp",
    "vector_id": "01J9Z3Q8K7P4R2VN5C9XB6TY1A",
    "dim": 384,
    "model_id": "BAAI/bge-small-en-v1.5",
    "dek_id": "01J9Z2W0F0AAB1CDE2FGH3JKLM",
    "encrypted": true
  },
  "importance": 0.82,
  "raw_scores": {
    "vector_similarity": 0.7913,
    "bm25": 11.42,
    "rrf": 0.0317
  },
  "created_at": "2026-05-02T14:11:07.500Z",
  "last_accessed": "2026-06-27T09:30:00.000Z",
  "provenance": {
    "kind": "consolidation",
    "source_ref": "01J7AB00CDEF11GHJK22MN33PQ",
    "ingested_at": "2026-05-02T14:11:07.000Z",
    "consolidation_cost_usd": 0.0004,
    "pipeline_version": "1.0.0"
  }
}
```

Note `vector_id == id` (C11 binding) and `provenance.kind == "consolidation"` carrying a non-null `consolidation_cost_usd` — this row's existence cost a fraction of a cent that already entered the budget ledger.

---

## 3. `ContextBlock`

The Assembler's output unit: one contiguous span placed into the final prompt under the token budget. **C3 applies:** the router selects the model — and therefore the tokenizer — **before** final packing, so `token_count` here is the **true** count from the selected model's tokenizer. If the model is not yet knowable, blocks are packed against a conservative max-tokenization estimate **+ documented margin** and **re-validated post-route** (re-pack or fail-closed **413**).

```python
class BlockSource(str, Enum):
    MEMORY = "memory"            # derived from a MemoryObject
    CACHE = "cache"              # hydrated from a CacheEntry
    SYSTEM_PROMPT = "system_prompt"
    USER_QUERY = "user_query"
    TOOL_RESULT = "tool_result"
    COMPRESSED = "compressed"    # output of post-ACL compression stage

class ContextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- identity & isolation ---
    id: ULID
    tenant_id: TenantId
    source: BlockSource
    source_ref: ULID | None = Field(
        default=None,
        description="ULID of originating MemoryObject/CacheEntry, if any",
    )

    # --- content as placed ---
    content: str = Field(min_length=0)
    token_count: int = Field(ge=0, description="TRUE count from the selected model tokenizer (C3)")

    # --- final-ranking output (Assembler is the SOLE authority, C1) ---
    score: float = Field(ge=0.0, description="fused final score under the one weight vocabulary")
    position: int = Field(ge=0, description="0-based slot in the assembled prompt order")

    # --- access & lineage ---
    access_scope: AccessScope
    provenance: Provenance

    # --- C3 tokenizer-truth audit ---
    tokenizer_id: str = Field(description="tokenizer used for token_count, e.g. 'cl100k_base' or model-native")
    token_count_estimated: bool = Field(
        default=False,
        description="True if packed pre-route against conservative estimate + margin; "
                    "must be re-validated post-route or fail-closed 413",
    )
    compression_ratio: float | None = Field(
        default=None, ge=1.0,
        description="N:1 reduction if source ran through compression (2-4x typical, >=98% fact retention)",
    )
```

### 3.1 Why `score` and `position` are distinct fields

A common mistake is to treat in-prompt order as `ORDER BY score DESC`. We reject that. `score` is the fused relevance under the **single weight vocabulary** (C1); `position` is the **edge-placement** decision that fights *lost-in-the-middle* — the highest-`score` blocks are pinned to the head **and** tail, lower-`score` filler goes to the middle. Two blocks can have `score_A > score_B` yet `position_A > position_B`. Storing both makes the Replay Debugger able to show *both* the ranking and the placement decision, which a single sorted list could never reconstruct.

### 3.2 The assembly budget invariant (in-data form)

The Assembler guarantees, for the assembled set `B`:

```
sum(b.token_count for b in B) + hard_reserve(model) <= context_window(model)
```

where every `b.token_count` is computed with `b.tokenizer_id == selected_model.tokenizer` (C3). If any block carries `token_count_estimated == True` after routing, the Assembler **must** re-pack against the now-known tokenizer or return **HTTP 413** — it must not ship an estimate into the backend. This whole stage is the **< 50 ms p95** context-assembly budget (score+MMR+budget-knapsack over <= 512 candidates; excludes retrieval I/O and inference).

### 3.3 Example payload

```json
{
  "id": "01J9ZB10M5N6P7Q8R9S0T1U2V3",
  "tenant_id": "acme-corp",
  "source": "compressed",
  "source_ref": "01J9Z3Q8K7P4R2VN5C9XB6TY1A",
  "content": "ACME billing prefs: EUR, net-30. Finance contact on file.",
  "token_count": 17,
  "score": 0.7642,
  "position": 1,
  "access_scope": "project",
  "provenance": {
    "kind": "consolidation",
    "source_ref": "01J7AB00CDEF11GHJK22MN33PQ",
    "ingested_at": "2026-05-02T14:11:07.000Z",
    "consolidation_cost_usd": 0.0004,
    "pipeline_version": "1.0.0"
  },
  "tokenizer_id": "cl100k_base",
  "token_count_estimated": false,
  "compression_ratio": 2.4
}
```

`compression_ratio: 2.4` reflects the **2-4x** compression target with **>= 98% fact retention (NLI-guarded)**, and `token_count_estimated: false` confirms the model (and thus tokenizer) was known before packing — the C3 happy path.

---

## 4. `RBACPolicy`

Attribute-based access control with **deny-overrides** evaluation. This is the single authority the firewall consults at the ACL/redaction stage and that the router consults via `check(principal, resource=model, action="route")` (**C10** — there is **no second policy store** for model allowlist/residency; `RoutePolicy.allowed_backends` *derives* from RBAC).

```python
class Action(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"
    ROUTE = "route"            # C10: single authority for model-allowlist + residency
    CACHE_READ = "cache_read"  # gate on semantic-cache hydration

class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"

class AttributeMatch(BaseModel):
    """ABAC predicate. A key absent on the subject => NO match (fail-closed)."""
    model_config = ConfigDict(extra="forbid")
    attribute: str = Field(description="dotted attribute path, e.g. 'role' or 'residency'")
    op: Annotated[str, StringConstraints(pattern=r"^(eq|neq|in|not_in|glob)$")]
    values: list[str] = Field(min_length=1)

class PrincipalMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # ALL predicates must hold (AND). Empty list matches nothing (fail-closed), NOT everything.
    predicates: list[AttributeMatch] = Field(min_length=1)

class ResourceMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_type: Annotated[str, StringConstraints(pattern=r"^(memory|model|cache|namespace|trace)$")]
    predicates: list[AttributeMatch] = Field(default_factory=list)  # empty => any resource of type

class RBACRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ULID
    principal: PrincipalMatch
    resource: ResourceMatch
    actions: list[Action] = Field(min_length=1)
    effect: Effect

class RBACPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ULID
    tenant_id: TenantId
    version: int = Field(ge=1, description="monotonic; STATIC policy snapshot id used by the router (C9)")
    rules: list[RBACRule] = Field(default_factory=list)
    created_at: UtcTimestamp

    # --- deny-overrides evaluation ---
    def check(self, principal: dict, resource: dict, action: Action) -> Effect:
        """Returns ALLOW only if some rule explicitly allows AND no rule denies.
        Default is DENY (fail-closed). Deny strictly overrides allow."""
        matched_allow = False
        for r in self.rules:
            if action not in r.actions:
                continue
            if not _principal_matches(r.principal, principal):
                continue
            if not _resource_matches(r.resource, resource):
                continue
            if r.effect is Effect.DENY:
                return Effect.DENY          # deny-overrides: short-circuit
            matched_allow = True
        return Effect.ALLOW if matched_allow else Effect.DENY  # default deny
```

### 4.1 Deny-overrides, and why not first-match or allow-overrides

| Conflict-resolution algorithm | Verdict | Why |
| --- | --- | --- |
| **Deny-overrides** | **CHOSEN** | A single explicit `deny` (e.g., residency carve-out, RTBF exclusion) wins regardless of rule order or how many allows exist. Order-independent, which makes policy review tractable and replay deterministic. |
| First-match-wins | Rejected | Outcome depends on rule ordering; a reordering during an edit can silently flip an effective-deny to an allow. Order-dependence is a security footgun. |
| Allow-overrides | Rejected | An over-broad allow could re-open something a targeted deny meant to close — the opposite of fail-closed. |
| No explicit default | Rejected | Absence of a matching rule **must** be DENY. `check()` defaults to DENY; this is the C2/C9 fail-closed posture. |

### 4.2 C9/C10 coupling: static policy, hard fail-closed

The router consumes a **specific `version`** of `RBACPolicy` as its **STATIC policy snapshot** (C9). Hard-policy filters — model allowlist, residency, capability, budget — evaluate on this static snapshot and **fail-closed independently of health-store availability**. Only optimization signals (latency/queue/quality) fail *open* to a static ranking. The safe-default backend pool must *itself* pass `check(..., action="route")` against the same snapshot — **residency is never bypassed**, even in the degraded default. `RoutePolicy.allowed_backends` is *computed* from `check`; it is never an independent list that could drift from RBAC.

### 4.3 Example payload

```json
{
  "id": "01J9ZC20A1B2C3D4E5F6G7H8J9",
  "tenant_id": "acme-corp",
  "version": 7,
  "created_at": "2026-06-01T00:00:00.000Z",
  "rules": [
    {
      "id": "01J9ZC2100AAAA1111BBBB2222C",
      "principal": { "predicates": [ { "attribute": "role", "op": "in", "values": ["analyst", "agent"] } ] },
      "resource": { "resource_type": "model", "predicates": [ { "attribute": "residency", "op": "eq", "values": ["eu"] } ] },
      "actions": ["route"],
      "effect": "allow"
    },
    {
      "id": "01J9ZC2100DDDD3333EEEE4444F",
      "principal": { "predicates": [ { "attribute": "role", "op": "eq", "values": ["analyst"] } ] },
      "resource": { "resource_type": "model", "predicates": [ { "attribute": "residency", "op": "neq", "values": ["eu"] } ] },
      "actions": ["route"],
      "effect": "deny"
    },
    {
      "id": "01J9ZC2100GGGG5555HHHH6666J",
      "principal": { "predicates": [ { "attribute": "namespace", "op": "eq", "values": ["billing-assistant"] } ] },
      "resource": { "resource_type": "memory", "predicates": [] },
      "actions": ["read", "cache_read"],
      "effect": "allow"
    }
  ]
}
```

Read this as: an EU analyst may **route** to EU-resident models (rule 1), but is **explicitly denied** routing to any non-EU model (rule 2) — and because of **deny-overrides**, even if a future broad allow appeared, rule 2 keeps residency enforced. Rule 3 grants memory read + semantic-cache hydration within the `billing-assistant` namespace.

---

## 5. `CacheEntry`

The two-tier cache record. **C5/C6 apply.** The key is **tenant-salted** (per-tenant namespaced — no cross-tenant key collision is even representable). The match is the **COARSE fingerprint**: `hash(normalized-query-embedding-bucket + model_id + system_prompt_version + stable_fact_set_version)`. **Memory-private-grounded responses are non-cacheable** and carry `non_cacheable=True`.

```python
class CacheTier(str, Enum):
    EXACT = "exact"        # Redis, exact-hash, < 1 ms p99
    SEMANTIC = "semantic"  # pgvector/Qdrant ANN, 8-15 ms p95 (incl. embed on miss)

class CoarseFingerprint(BaseModel):
    """C6 COARSE signature. NOT the raw query — a bucketed, versioned signature.
    Memory-private-grounded responses must set non_cacheable on the entry."""
    model_config = ConfigDict(extra="forbid")
    query_embedding_bucket: str = Field(description="quantized ANN bucket id of normalized query embedding")
    model_id: str
    system_prompt_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
    stable_fact_set_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]

    def digest(self) -> str:
        import hashlib
        raw = "|".join([
            self.query_embedding_bucket, self.model_id,
            self.system_prompt_version, self.stable_fact_set_version,
        ])
        return hashlib.sha256(raw.encode()).hexdigest()

class CacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- tenant-salted key (per-tenant namespaced; no cross-tenant collision possible) ---
    tenant_id: TenantId
    key: str = Field(description="'cache:{tenant_id}:{tier}:{fingerprint.digest()}' — tenant-salted")
    tier: CacheTier
    fingerprint: CoarseFingerprint

    # --- payload by reference (response body NOT inlined; ref into blob/Redis value) ---
    response_ref: ULID = Field(description="ULID of stored response body (materialized final response)")

    # --- lifecycle ---
    ttl_s: int = Field(ge=0, description="0 => no expiry (exact tier rarely uses this)")
    hit_count: int = Field(default=0, ge=0)
    created_at: UtcTimestamp
    last_hit_at: UtcTimestamp | None = None

    # --- correctness flags (C6) ---
    non_cacheable: bool = Field(
        default=False,
        description="memory-private-grounded responses => True; such entries are never written",
    )
    access_scope: AccessScope = AccessScope.PRIVATE
```

### 5.1 Tenant-salting and latency truth (C5)

The key template is **`cache:{tenant_id}:{tier}:{digest}`**. Tenant id is *inside* the key, salted ahead of the digest, so two tenants whose normalized queries land in the same embedding bucket still produce disjoint keys — cross-tenant cache leakage is **0** by construction, reinforcing the FORCE-RLS + RBAC firewall guarantee. Latency truth (single source, do not restate differently):

| Tier | Datastore | Latency | Notes |
| --- | --- | --- | --- |
| `exact` | Redis | **< 1 ms p99** | sub-1ms claim applies **only** here |
| `semantic` | pgvector/Qdrant ANN | **8-15 ms p95** | includes ~6 ms query embedding on miss + ANN |

The coarse-fingerprint policy yields a re-derived **25-45% hit-ratio** on a realistic mixed workload, contributing the **15-30%** caching slice of the **40-65%** combined token-cost savings.

### 5.2 Why coarse, not exact-text and not full-prompt-embedding

| Fingerprint design | Verdict | Why |
| --- | --- | --- |
| **Coarse: embedding-bucket + model_id + sys-prompt-ver + fact-set-ver** | **CHOSEN** | Survives trivial paraphrase (semantic bucket) yet versioned on the *things that change the answer* (model, system prompt, stable facts). Re-derives the honest 25-45% hit-ratio. |
| Exact verbatim-text hash only | Rejected as the *semantic* tier | Zero paraphrase tolerance collapses hit-ratio toward single digits. We keep exact-hash as the *fast* tier (< 1 ms) but it cannot be the whole story. |
| Full per-query prompt embedding as the key | Rejected | Too fine — every minor context shuffle is a miss, *and* it risks keying on private-grounded content. The bucket quantization + explicit `non_cacheable` flag is the safer, cheaper signal. |

A **memory-private-grounded** response (one whose answer depends on a `PRIVATE`-scope `MemoryObject`) sets `non_cacheable=True` and is **never written** to either tier — caching it could serve one subject's private grounding to a bucket-colliding query. The offline cache-correctness eval harness (roadmap) validates this flag's precision.

### 5.3 Example payload

```json
{
  "tenant_id": "acme-corp",
  "key": "cache:acme-corp:semantic:9f2c5b1e7a4d0c83b6e1f0a2d9c4b7e6a1f3c8d2b5e7a0c4d6f8b1e3a5c7d9f0",
  "tier": "semantic",
  "fingerprint": {
    "query_embedding_bucket": "buck_0x1A3F",
    "model_id": "router-pool/eu-small-v1",
    "system_prompt_version": "3.2.0",
    "stable_fact_set_version": "12.0.1"
  },
  "response_ref": "01J9ZD30K1L2M3N4P5Q6R7S8T9",
  "ttl_s": 3600,
  "hit_count": 14,
  "created_at": "2026-06-28T08:00:00.000Z",
  "last_hit_at": "2026-06-28T11:42:17.250Z",
  "non_cacheable": false,
  "access_scope": "project"
}
```

`response_ref` points to the **materialized final response** — which is also what a streaming `Idempotency-Key` replay returns with **zero second backend call** (C15). The cache and the idempotency layer share this stored-body convention.

---

## 6. `TraceSpan`

The OTel-compatible correlation spine. **C12 applies:** these are **best-effort, fail-open, sampled** traces (tail 1-10% + force-keep on errors and on `cost > $0.05/req`) — distinct from **billing-grade cost records** which are fail-closed via a durable outbox. **C7 applies:** a span's `decision_record_ref` points at the deterministic stage decision that the Replay Debugger replays byte-exact.

`trace_id` (128-bit) and `span_id` (64-bit) are **W3C Trace Context hex**, not ULIDs, so external OTel collectors interoperate. (ULID is for *our* domain ids; OTel ids follow the OTel spec — a deliberate dual convention.)

```python
class PipelineStage(str, Enum):
    # Mirrors the PIPELINE ORDERING INVARIANT exactly.
    AUTH_TENANT = "auth_tenant"
    CACHE_LOOKUP = "cache_lookup"
    RETRIEVE = "retrieve"
    ACL_REDACTION = "acl_redaction"
    COMPRESSION = "compression"        # ALWAYS after acl_redaction
    ASSEMBLY = "assembly"
    ROUTING = "routing"
    ADAPTER = "adapter"
    STREAM = "stream"
    WRITE_BACK = "write_back"

class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"

class TraceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- W3C / OTel ids (hex, not ULID) ---
    trace_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    span_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
    parent_span_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")] | None = None

    # --- isolation (non-null tenant_id everywhere, even in telemetry) ---
    tenant_id: TenantId

    # --- what & when ---
    stage: PipelineStage
    name: str
    start_time: UtcTimestamp
    end_time: UtcTimestamp
    status: SpanStatus = SpanStatus.OK

    # --- OTel attributes (string-keyed, scalar values) ---
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)

    # --- C7 replay linkage: the deterministic decision this stage produced ---
    decision_record_ref: ULID | None = Field(
        default=None,
        description="ULID of the recorded deterministic stage decision in the replay bundle",
    )

    # --- C12 sampling provenance ---
    sampled: bool = Field(description="False => head-dropped by tail sampler")
    force_kept: bool = Field(
        default=False,
        description="True if retained despite sampling: status==error OR req cost > $0.05",
    )

    # DERIVED, not stored: duration_ms is computed from start_time/end_time, never
    # persisted as a column. @computed_field makes Pydantic emit it in model_dump()
    # so it appears in the example payload, but there is no settable duration_ms field.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_ms(self) -> float:
        return (self.end_time - self.start_time).total_seconds() * 1000.0
```

`duration_ms` is a **`@computed_field`** (derived from `end_time - start_time`), **not a stored field**: there is no `duration_ms` column in any table and no way to set it independently of the two timestamps — which keeps the span's duration internally consistent and replay-deterministic (a stored, separately-writable `duration_ms` could drift from `end_time - start_time` and silently corrupt the latency-budget audit).

### 6.1 C7: deterministic stages vs the non-deterministic backend

The Replay Debugger reconstructs a request from the per-tenant-encrypted, content-addressed bundle. **Every ContextOS decision stage is deterministic** and its decision is addressable via `decision_record_ref`. The **`adapter`/`backend.invoke` span is the one non-deterministic boundary**: `output_equal` is asserted **only** when replaying a *recorded* output; with `live_backend=True` the Debugger yields a **diff**, not byte-equality. A `TraceSpan` with `stage == "adapter"` therefore typically carries a `decision_record_ref` to the *inputs* (assembled prompt, routing choice) but the replay contract does not promise its *output* matches under live replay. (`ReplayResult` itself is the single schema owned by the API/observability/killer-feature sections — `TraceSpan.decision_record_ref` is the join key into it; this section does not redefine `ReplayResult`.)

### 6.2 C12: trace path vs cost path are not the same write

`TraceSpan` is **best-effort**: dropped spans are acceptable (fail-open), and the **tail sampler keeps 1-10%** plus **force-keeps** any span where `status == error` or the request's cost exceeds **$0.05**. Billing-grade cost is **never** carried only as a span — it goes through a **fail-closed durable outbox** (Section on cost ledger). So `force_kept=true` on a span and a durable cost record are **two independent guarantees**; an operator must not infer billing completeness from trace completeness.

### 6.3 C8: client-abort attribution surfaces here

When a stream ends, the **terminal-event source decides commit vs discard** (C8): if the **server finish_reason** is reached, write-back commits (the `write_back` span fires with `status=ok`); if the **client TCP-closes before the server terminal**, write-back is discarded. Partial-cost attribution (tokens already emitted by the backend before abort) is recorded as a `cost_usd_partial` attribute on the `adapter` span and is what the durable cost outbox bills — the trace shows *why* a partial charge exists even though no memory was written.

### 6.4 Example payload

```json
{
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "parent_span_id": "0a1b2c3d4e5f6071",
  "tenant_id": "acme-corp",
  "stage": "routing",
  "name": "model.route.decision",
  "start_time": "2026-06-28T11:42:17.100Z",
  "end_time": "2026-06-28T11:42:17.105Z",
  "status": "ok",
  "attributes": {
    "difficulty_score": 0.31,
    "selected_backend": "router-pool/eu-small-v1",
    "residency": "eu",
    "rbac_action": "route",
    "breaker_state": "closed",
    "fail_open_optimization": true,
    "cost_usd_partial": 0.0
  },
  "decision_record_ref": "01J9ZE40N1P2Q3R4S5T6U7V8W9",
  "sampled": true,
  "force_kept": false,
  "duration_ms": 5.0
}
```

Here `duration_ms: 5.0` is a **`@computed_field` (derived from `end_time - start_time`), not a stored field** — it appears in the serialized payload only because `@computed_field` instructs Pydantic to emit it; `11:42:17.105Z - 11:42:17.100Z == 5.0 ms`. The `duration_ms: 5.0` matches the **5 ms** model-routing line of the Section 9 latency budget; `rbac_action: "route"` and `residency: "eu"` show the C9/C10 hard filter evaluated on static policy; `decision_record_ref` is the join into the replay bundle for byte-exact replay of this routing decision.

---

## 7. Cross-schema invariants (enforced, not aspirational)

These hold across all five schemas and are checked at the repository/CI boundary, not merely documented.

| # | Invariant | Where enforced |
| --- | --- | --- |
| I1 | `tenant_id` is **non-null** on every object/row/key. | Pydantic `TenantId` (no default) + Postgres `NOT NULL` + cache key template. |
| I2 | **No vector inlined** in any object; vectors live in the store via `EmbeddingRef`. | `MemoryObject` has only `embedding_ref`; no `vector` field exists. |
| I3 | **C11** vector payload+id encrypted under per-subject DEK; `vector_id == MemoryObject.id`. | `EmbeddingRef.dek_id` + GC sweep on a single ULID. |
| I4 | **C2** namespace is a hard, fail-closed filter; missing/ambiguous => deny. | Repository AND-s `tenant_id` + `namespace.as_filter()`; `RBACPolicy.check` defaults DENY. |
| I5 | **C1** Memory emits raw scores only; only `ContextBlock` carries fused `score`+`position`. | `MemoryObject.raw_scores` vs `ContextBlock.score`/`position`. |
| I6 | **C3** every `ContextBlock.token_count` is true under `tokenizer_id`; estimates re-validated or 413. | `token_count_estimated` flag + Assembler re-pack guard. |
| I7 | **C6** memory-private-grounded responses are `non_cacheable`; never written. | `CacheEntry.non_cacheable`; write path rejects PRIVATE-grounded. |
| I8 | **C7** deterministic stage decisions are replay-addressable; backend output is not byte-equal under live replay. | `TraceSpan.decision_record_ref`; `adapter` stage excluded from `output_equal`. |
| I9 | **C12** traces fail-open + sampled; cost records fail-closed durable. | `TraceSpan.sampled`/`force_kept` vs separate cost outbox. |
| I10 | All ids ULID **except** OTel `trace_id`/`span_id` (W3C hex). | Type aliases: `ULID` vs the hex `StringConstraints`. |
| I11 | All timestamps RFC-3339 UTC, millisecond precision, `Z` suffix. | `UtcTimestamp` serializer. |

---

## 8. Postgres storage notes (RLS-aligned)

The relational projection of these schemas lives in **Postgres 16** with `tenant_id` as the **partition key** and **FORCE ROW LEVEL SECURITY** (chosen over app-only filtering because a missed `WHERE tenant_id=` in any one query path would otherwise be a cross-tenant leak; FORCE RLS makes the database itself fail-closed). The CI hard gate runs **>= 10,000 hostile second-tenant property probes** asserting cross-tenant leakage is **0**.

```sql
-- memory table (vector lives in pgvector via embedding_ref; NOT inlined here)
CREATE TABLE memory (
    id              TEXT PRIMARY KEY,                 -- ULID
    tenant_id       TEXT NOT NULL,                    -- partition key, non-null
    namespace_kind  TEXT NOT NULL,
    namespace_key   TEXT NOT NULL,
    access_scope    TEXT NOT NULL,
    tier            TEXT NOT NULL,
    content         TEXT NOT NULL,
    embedding_ref   JSONB NOT NULL,                   -- EmbeddingRef (dek_id, vector_id==id)
    importance      DOUBLE PRECISION NOT NULL CHECK (importance BETWEEN 0 AND 1),
    raw_scores      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL,
    last_accessed   TIMESTAMPTZ NOT NULL,
    provenance      JSONB NOT NULL
) PARTITION BY LIST (tenant_id);

ALTER TABLE memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory FORCE ROW LEVEL SECURITY;       -- applies even to table owner
CREATE POLICY memory_tenant_isolation ON memory
    USING (tenant_id = current_setting('app.tenant_id', true));
```

The `embedding_ref` JSONB holds the indirection (backend, collection, `vector_id`, `dek_id`); the **384-dim vector** is a separate, **per-subject-DEK-encrypted** row in the `pgvector` collection. RTBF crypto-shreds the DEK and the idempotent GC sweep removes the HNSW node by `vector_id` — so the relational delete and the vector delete are two idempotent operations keyed by the **same ULID**, never a fragile cross-store join.

---

## 9. Summary

These five Pydantic-v2 schemas — `MemoryObject`, `ContextBlock`, `RBACPolicy`, `CacheEntry`, `TraceSpan` — share one set of primitives (ULID ids, RFC-3339-UTC `Z` timestamps, the tenant/namespace/scope isolation triad, immutable `Provenance`, and the `embedding_ref` vector **indirection**) and encode the consistency resolutions directly into their fields rather than leaving them to prose. `tenant_id` is non-null on every object, row, and cache key; the vector is never inlined and is crypto-shred-covered under a per-subject DEK (C11) with `vector_id == MemoryObject.id`. Final ranking lives only on `ContextBlock` (C1), token counts are tokenizer-true (C3), private-grounded responses are non-cacheable (C6), RBAC is deny-overrides and the sole route authority (C10), and traces are fail-open/sampled while cost is fail-closed/durable (C12).
