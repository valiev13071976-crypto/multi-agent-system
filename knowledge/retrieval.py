"""Governed retrieval service — tenant filter before exposure."""

from __future__ import annotations

import uuid

from knowledge.index import KnowledgeIndex
from knowledge.models import (
    KNOWLEDGE_RETRIEVAL_VERSION,
    KnowledgeProvenance,
    KnowledgeResult,
    SOURCE_MANUAL_REFERENCE,
    TRUST_UNVERIFIED,
)
from knowledge.platform_models import RETRIEVAL_HYBRID, RetrievalResult
from knowledge.store import KnowledgeStore
from memory.models import MemoryScope, utc_now
from security.tenant import normalize_tenant_id


class KnowledgeRetrievalService:
    def __init__(
        self,
        store: KnowledgeStore,
        index: KnowledgeIndex,
        *,
        profile_version: str = KNOWLEDGE_RETRIEVAL_VERSION,
    ):
        self.store = store
        self.index = index
        self.profile_version = profile_version

    def retrieve(
        self,
        *,
        query_text: str,
        scope: MemoryScope,
        limit: int = 10,
        method: str = RETRIEVAL_HYBRID,
    ) -> RetrievalResult:
        tenant = normalize_tenant_id(scope.tenant_ref)
        # Tenant filter enforced inside index.search — never global scan + post-filter
        candidates = self.index.search(
            tenant_ref=tenant,
            query_text=query_text,
            limit=min(limit, 20),
            method=method,
        )
        return RetrievalResult(
            request_id=str(uuid.uuid4()),
            tenant_ref=tenant,
            profile_version=self.profile_version,
            candidates=tuple(candidates),
            no_results=len(candidates) == 0,
            truncated=len(candidates) >= limit,
        )

    def to_knowledge_results(self, result: RetrievalResult) -> tuple[KnowledgeResult, ...]:
        rows: list[KnowledgeResult] = []
        for c in result.candidates:
            if c.tenant_ref != result.tenant_ref:
                continue
            prov = KnowledgeProvenance(
                source_id=c.source_id,
                source_type=SOURCE_MANUAL_REFERENCE,
                source_ref=c.version_id,
                ingested_at=utc_now(),
                source_hash="",
                trust_level=TRUST_UNVERIFIED,
                chunk_id=c.chunk_id,
            )
            rows.append(
                KnowledgeResult(
                    knowledge_id=c.knowledge_id,
                    content=c.content,
                    score=c.score,
                    source_id=c.source_id,
                    source_type=SOURCE_MANUAL_REFERENCE,
                    trust_level=TRUST_UNVERIFIED,
                    freshness="current",
                    stale=False,
                    provenance=prov,
                    citation_ref=c.citation_ref,
                    metadata_safe={"retrieval_method": c.retrieval_method},
                )
            )
        return tuple(rows)
