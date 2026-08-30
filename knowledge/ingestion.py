"""Governed knowledge ingestion pipeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from knowledge.chunking import chunk_text
from knowledge.embeddings import DeterministicEmbeddingProvider
from knowledge.errors import KNOWLEDGE_INGEST_FAILED, KnowledgeError
from knowledge.index import KnowledgeIndex
from knowledge.models import content_hash_text
from knowledge.platform_models import (
    INGEST_STAGE_CHUNK,
    INGEST_STAGE_COMPLETE,
    INGEST_STAGE_EMBED,
    INGEST_STAGE_INDEX,
    INGEST_STAGE_NORMALIZE,
    INGEST_STAGE_VALIDATE,
    KNOWLEDGE_INGESTION_PROFILE_VERSION,
    STATUS_ACTIVE,
    KnowledgeChunk,
    KnowledgeIngestionJob,
    KnowledgeVersion,
)
from knowledge.planner import assert_sync_ingest_allowed
from knowledge.store import KnowledgeStore
from memory.models import MemoryScope, utc_now
from security.tenant import normalize_tenant_id


@dataclass(frozen=True)
class IngestionResult:
    job: KnowledgeIngestionJob
    version: KnowledgeVersion
    chunks: tuple[KnowledgeChunk, ...]
    deduplicated: bool = False


class KnowledgeIngestionPipeline:
    def __init__(
        self,
        store: KnowledgeStore,
        index: KnowledgeIndex | None = None,
        *,
        embedding_provider: DeterministicEmbeddingProvider | None = None,
        profile_version: str = KNOWLEDGE_INGESTION_PROFILE_VERSION,
    ):
        self.store = store
        self.index = index or KnowledgeIndex()
        self.embedder = embedding_provider or DeterministicEmbeddingProvider()
        self.profile_version = profile_version

    def ingest_text(
        self,
        *,
        content: str,
        scope: MemoryScope,
        source_id: str,
        knowledge_id: str | None = None,
        bulk: bool = False,
        resume_job: KnowledgeIngestionJob | None = None,
    ) -> IngestionResult:
        tenant = normalize_tenant_id(scope.tenant_ref)
        raw_bytes = len(str(content or "").encode("utf-8"))
        specs = chunk_text(content)
        if not bulk:
            assert_sync_ingest_allowed(byte_size=raw_bytes, chunk_count=len(specs), bulk=False)

        job_id = resume_job.job_id if resume_job else str(uuid.uuid4())
        job = KnowledgeIngestionJob(
            job_id=job_id,
            tenant_ref=tenant,
            source_id=source_id,
            stage=INGEST_STAGE_VALIDATE,
            status="running",
            content_hash=content_hash_text(content),
            checkpoint=0,
            chunk_total=len(specs),
        )
        self.store.save_job(job)

        normalized = " ".join(str(content or "").split())
        digest = content_hash_text(normalized)
        existing = self.store.find_version_by_hash(
            tenant_ref=tenant,
            scope=scope,
            source_id=source_id,
            content_hash=digest,
        )
        if existing is not None:
            chunks = self.store.list_chunks(tenant_ref=tenant, version_id=existing.version_id)
            done = KnowledgeIngestionJob(
                job_id=job_id,
                tenant_ref=tenant,
                source_id=source_id,
                stage=INGEST_STAGE_COMPLETE,
                status="dedup",
                content_hash=digest,
                checkpoint=len(chunks),
                chunk_total=len(chunks),
            )
            self.store.save_job(done)
            return IngestionResult(job=done, version=existing, chunks=chunks, deduplicated=True)

        kid = knowledge_id or str(uuid.uuid4())
        prior_versions = self.store.list_active_versions(tenant_ref=tenant, scope=scope, source_id=source_id)
        version_num = max((v.version_num for v in prior_versions), default=0) + 1
        supersedes = prior_versions[0].version_id if prior_versions else None

        version = KnowledgeVersion(
            version_id=str(uuid.uuid4()),
            knowledge_id=kid,
            tenant_ref=tenant,
            source_id=source_id,
            content_hash=digest,
            version_num=version_num,
            status=STATUS_ACTIVE,
            supersedes_version_id=supersedes,
        )
        self.store.save_version(version, scope=scope)
        if supersedes:
            self.store.supersede_version(supersedes, version.version_id, tenant_ref=tenant)
            if self.index is not None:
                self.index.tombstone_version(tenant_ref=tenant, version_id=supersedes)

        chunks: list[KnowledgeChunk] = []
        start = resume_job.checkpoint if resume_job else 0
        for spec in specs[start:]:
            ch = KnowledgeChunk(
                chunk_id=str(uuid.uuid4()),
                version_id=version.version_id,
                knowledge_id=kid,
                tenant_ref=tenant,
                sequence=spec.sequence,
                content=spec.content,
                content_hash=spec.content_hash,
                token_estimate=spec.token_estimate,
                scope=scope,
                source_id=source_id,
                section_ref=spec.section_ref,
                char_start=spec.char_start,
                char_end=spec.char_end,
                overlap_prev=spec.overlap_prev,
            )
            chunks.append(ch)

        if not chunks:
            raise KnowledgeError(KNOWLEDGE_INGEST_FAILED, "no_chunks_produced")

        self.store.save_chunks(chunks)
        records = self.index.build_records(chunks)
        self.store.save_index_records(records)
        self.index.index_chunks(chunks, records=records)

        done = KnowledgeIngestionJob(
            job_id=job_id,
            tenant_ref=tenant,
            source_id=source_id,
            stage=INGEST_STAGE_COMPLETE,
            status="completed",
            content_hash=digest,
            checkpoint=len(chunks),
            chunk_total=len(chunks),
        )
        self.store.save_job(done)
        return IngestionResult(job=done, version=version, chunks=tuple(chunks))
