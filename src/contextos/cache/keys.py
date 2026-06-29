"""Cache key / fingerprint construction (the COARSE policy, C6).

The fingerprint partitions the cache by everything that must match for a hit to be *correct*:
tenant, namespace, model, system-prompt version, and stable-fact-set version. It is both the
exact-tier key (with the normalized query appended) and the hard pre-filter on the semantic
tier — semantic ANN only ever considers entries that already share this signature; only the
query embedding is relaxed into similarity. Tenant + namespace are inside the hash, so keys
are tenant-salted and a cross-tenant lookup can never collide.
"""

from __future__ import annotations

import hashlib

_SEP = "\x1f"  # unit separator — unambiguous join, can't appear in normal text


def normalize_query(query: str) -> str:
    """Surface normalization so byte-identical-after-normalization queries hit the exact tier."""
    return " ".join(query.lower().split())


def _sha(parts: list[str]) -> str:
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()


def prefilter_sig(
    tenant_id: str, namespace: str, model_id: str,
    system_prompt_version: str, stable_facts_version: str,
) -> str:
    """Hard pre-filter — a semantic hit MUST share all of these (C6)."""
    return _sha([tenant_id, namespace, model_id, system_prompt_version, stable_facts_version])


def exact_sig(
    tenant_id: str, namespace: str, model_id: str,
    system_prompt_version: str, stable_facts_version: str, query: str,
) -> str:
    """Embedding-free exact-tier key: the pre-filter plus the normalized query string."""
    return _sha([
        prefilter_sig(tenant_id, namespace, model_id, system_prompt_version, stable_facts_version),
        normalize_query(query),
    ])
