"""Tenant-scoped similarity index."""

from __future__ import annotations

from product_media.fingerprint import classify_similarity, hamming_distance
from product_media.platform_models import MediaFingerprint, MediaSimilarityResult
from product_media.policy import MAX_SIMILARITY_CANDIDATES
from security.tenant import require_tenant_id


class TenantSimilarityIndex:
    def __init__(self):
        self._by_tenant: dict[str, list[MediaFingerprint]] = {}

    def index(self, fp: MediaFingerprint) -> None:
        tenant = require_tenant_id(fp.tenant_id)
        self._by_tenant.setdefault(tenant, []).append(fp)

    def remove_version(self, *, tenant_id: str, version_id: str) -> int:
        tenant = require_tenant_id(tenant_id)
        items = self._by_tenant.get(tenant, [])
        kept = [x for x in items if x.version_id != version_id]
        removed = len(items) - len(kept)
        self._by_tenant[tenant] = kept
        return removed

    def find_similar(
        self,
        *,
        tenant_id: str,
        query: MediaFingerprint,
        max_candidates: int = MAX_SIMILARITY_CANDIDATES,
    ) -> list[MediaSimilarityResult]:
        tenant = require_tenant_id(tenant_id)
        if require_tenant_id(query.tenant_id) != tenant:
            return []
        candidates = self._by_tenant.get(tenant, [])
        scored: list[tuple[int, MediaFingerprint]] = []
        for fp in candidates:
            if fp.version_id == query.version_id:
                continue
            if fp.content_hash == query.content_hash:
                scored.append((0, fp))
                continue
            dist = hamming_distance(fp.perceptual_hash, query.perceptual_hash)
            scored.append((dist, fp))
        scored.sort(key=lambda x: x[0])
        results: list[MediaSimilarityResult] = []
        for dist, fp in scored[:max_candidates]:
            if dist == 0 and fp.content_hash == query.content_hash:
                classification = "duplicate"
                score = 1.0
            else:
                classification = classify_similarity(dist)
                score = max(0.0, 1.0 - dist / 64.0)
            results.append(
                MediaSimilarityResult(
                    query_version_id=query.version_id,
                    candidate_version_id=fp.version_id,
                    tenant_id=tenant,
                    method="dhash",
                    score=score,
                    classification=classification,
                )
            )
        return results

    def find_exact_duplicates(self, *, tenant_id: str, content_hash: str) -> list[str]:
        tenant = require_tenant_id(tenant_id)
        return [
            fp.version_id
            for fp in self._by_tenant.get(tenant, [])
            if fp.content_hash == content_hash
        ]
