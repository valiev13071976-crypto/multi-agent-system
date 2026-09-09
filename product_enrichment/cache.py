"""Enrichment cost-control cache (requirement 13).

Deliberately as small/boring as
``business_assistant.action_continuation.ActiveTaskStore``: an in-process
dict keyed by ``(tenant_id, identity_key)`` where ``identity_key`` is the
deterministic EAN+brand+model hash from ``product_enrichment.models.
compute_identity_key``. No new infrastructure stack -- just avoids
repeating research/media/content work for the exact same product within
this process's lifetime. A repeated ``enrich_product`` call with the same
identity reuses the cached ``EnrichmentResult`` (``cache_hit=True``) instead
of re-running research/media acquisition.
"""

from __future__ import annotations

from product_enrichment.models import EnrichmentResult


class EnrichmentCache:
    def __init__(self):
        self._entries: dict[tuple[str, str], EnrichmentResult] = {}

    def get(self, *, tenant_id: str, identity_key: str) -> EnrichmentResult | None:
        return self._entries.get((str(tenant_id or ""), identity_key))

    def put(self, *, tenant_id: str, identity_key: str, result: EnrichmentResult) -> None:
        self._entries[(str(tenant_id or ""), identity_key)] = result

    def clear(self, *, tenant_id: str, identity_key: str) -> None:
        self._entries.pop((str(tenant_id or ""), identity_key), None)

    def __len__(self) -> int:
        return len(self._entries)
