"""In-memory knowledge index — tenant-filtered keyword + vector retrieval."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from knowledge.embeddings import DeterministicEmbeddingProvider, cosine_similarity
from knowledge.errors import KnowledgeIndexIncompatible
from knowledge.platform_models import (
    KNOWLEDGE_INDEX_VERSION,
    RETRIEVAL_HYBRID,
    RETRIEVAL_KEYWORD,
    RETRIEVAL_VECTOR,
    STATUS_ACTIVE,
    STATUS_TOMBSTONED,
    KnowledgeChunk,
    KnowledgeIndexRecord,
    RetrievalCandidate,
)
from security.tenant import normalize_tenant_id


@dataclass
class _IndexedChunk:
    chunk: KnowledgeChunk
    record: KnowledgeIndexRecord | None


class KnowledgeIndex:
    """Tenant-partitioned index — filter enforced before candidate exposure."""

    def __init__(self, *, embedding_provider: DeterministicEmbeddingProvider | None = None):
        self._provider = embedding_provider or DeterministicEmbeddingProvider()
        self._by_tenant: dict[str, list[_IndexedChunk]] = {}
        self.index_version = KNOWLEDGE_INDEX_VERSION

    def clear_tenant(self, tenant_ref: str) -> None:
        tenant = normalize_tenant_id(tenant_ref)
        self._by_tenant.pop(tenant, None)

    def index_chunks(
        self,
        chunks: list[KnowledgeChunk],
        *,
        records: list[KnowledgeIndexRecord] | None = None,
    ) -> None:
        rec_map = {r.chunk_id: r for r in (records or [])}
        for ch in chunks:
            if ch.status not in {STATUS_ACTIVE}:
                continue
            tenant = normalize_tenant_id(ch.tenant_ref)
            bucket = self._by_tenant.setdefault(tenant, [])
            bucket.append(_IndexedChunk(chunk=ch, record=rec_map.get(ch.chunk_id)))
        for tenant, bucket in self._by_tenant.items():
            bucket.sort(key=lambda x: (x.chunk.knowledge_id, x.chunk.sequence))

    def remove_version(self, *, tenant_ref: str, version_id: str) -> None:
        tenant = normalize_tenant_id(tenant_ref)
        bucket = self._by_tenant.get(tenant, [])
        self._by_tenant[tenant] = [x for x in bucket if x.chunk.version_id != version_id]

    def tombstone_version(self, *, tenant_ref: str, version_id: str) -> None:
        tenant = normalize_tenant_id(tenant_ref)
        for entry in self._by_tenant.get(tenant, []):
            if entry.chunk.version_id == version_id:
                entry.chunk = KnowledgeChunk(
                    chunk_id=entry.chunk.chunk_id,
                    version_id=entry.chunk.version_id,
                    knowledge_id=entry.chunk.knowledge_id,
                    tenant_ref=entry.chunk.tenant_ref,
                    sequence=entry.chunk.sequence,
                    content=entry.chunk.content,
                    content_hash=entry.chunk.content_hash,
                    token_estimate=entry.chunk.token_estimate,
                    scope=entry.chunk.scope,
                    source_id=entry.chunk.source_id,
                    status=STATUS_TOMBSTONED,
                )

    def search(
        self,
        *,
        tenant_ref: str,
        query_text: str,
        limit: int = 10,
        method: str = RETRIEVAL_HYBRID,
        embedding_model: str | None = None,
        embedding_version: str | None = None,
        max_per_source: int = 3,
    ) -> list[RetrievalCandidate]:
        tenant = normalize_tenant_id(tenant_ref)
        bucket = self._by_tenant.get(tenant, [])
        if not bucket:
            return []

        model = embedding_model or self._provider.model_id
        version = embedding_version or self._provider.version

        active = [x for x in bucket if x.chunk.status == STATUS_ACTIVE]
        tokens = set(re.findall(r"[a-z0-9_]+", query_text.lower()))
        query_vec: tuple[float, ...] | None = None
        if method in {RETRIEVAL_VECTOR, RETRIEVAL_HYBRID}:
            query_vec = self._provider.embed([query_text])[0]

        scored: list[tuple[float, _IndexedChunk, dict[str, float], str]] = []
        for entry in active:
            text = entry.chunk.content.lower()
            kw_score = 0.0
            if tokens:
                hits = sum(1 for t in tokens if t in text)
                kw_score = hits / max(len(tokens), 1)
            elif query_text.strip() and query_text.lower() in text:
                kw_score = 1.0

            vec_score = 0.0
            if query_vec and entry.record is not None:
                if (
                    entry.record.embedding_model != model
                    or entry.record.embedding_version != version
                    or entry.record.embedding_dim != len(query_vec)
                ):
                    raise KnowledgeIndexIncompatible("embedding_space_mismatch")
                vec_score = max(0.0, cosine_similarity(query_vec, entry.record.vector))

            if method == RETRIEVAL_KEYWORD:
                score = kw_score
                m = RETRIEVAL_KEYWORD
            elif method == RETRIEVAL_VECTOR:
                score = vec_score
                m = RETRIEVAL_VECTOR
            else:
                score = 0.55 * kw_score + 0.45 * vec_score
                m = RETRIEVAL_HYBRID

            if score <= 0 and query_text.strip():
                continue
            scored.append((score, entry, {"keyword": kw_score, "vector": vec_score}, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        seen_sources: dict[str, int] = {}
        out: list[RetrievalCandidate] = []
        for score, entry, components, method_used in scored:
            src_key = f"{entry.chunk.source_id}:{entry.chunk.knowledge_id}"
            if seen_sources.get(src_key, 0) >= max_per_source:
                continue
            seen_sources[src_key] = seen_sources.get(src_key, 0) + 1
            citation = f"knowledge:{entry.chunk.knowledge_id}#chunk:{entry.chunk.chunk_id}#v:{entry.chunk.version_id}"
            out.append(
                RetrievalCandidate(
                    chunk_id=entry.chunk.chunk_id,
                    version_id=entry.chunk.version_id,
                    knowledge_id=entry.chunk.knowledge_id,
                    tenant_ref=tenant,
                    content=entry.chunk.content,
                    score=round(score, 4),
                    retrieval_method=method_used,
                    source_id=entry.chunk.source_id,
                    citation_ref=citation,
                    score_components=components,
                    metadata_safe={"sequence": entry.chunk.sequence},
                )
            )
            if len(out) >= limit:
                break
        return out

    def build_records(
        self,
        chunks: list[KnowledgeChunk],
    ) -> list[KnowledgeIndexRecord]:
        texts = [c.content for c in chunks]
        vectors = self._provider.embed(texts)
        records: list[KnowledgeIndexRecord] = []
        for ch, vec in zip(chunks, vectors):
            records.append(
                KnowledgeIndexRecord(
                    record_id=str(uuid.uuid4()),
                    chunk_id=ch.chunk_id,
                    version_id=ch.version_id,
                    knowledge_id=ch.knowledge_id,
                    tenant_ref=ch.tenant_ref,
                    embedding_model=self._provider.model_id,
                    embedding_version=self._provider.version,
                    embedding_dim=len(vec),
                    index_version=self.index_version,
                    vector=vec,
                )
            )
        return records
