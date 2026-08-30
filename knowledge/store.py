"""Knowledge persistence contract — tenant-scoped durable store."""

from __future__ import annotations

from abc import ABC, abstractmethod

from knowledge.platform_models import (
    DeletionReceipt,
    KnowledgeChunk,
    KnowledgeIngestionJob,
    KnowledgeIndexRecord,
    KnowledgeVersion,
)
from memory.models import MemoryScope


class KnowledgeStore(ABC):
    available: bool = True

    @abstractmethod
    def save_version(self, version: KnowledgeVersion, *, scope: MemoryScope) -> KnowledgeVersion:
        raise NotImplementedError

    @abstractmethod
    def get_version(self, version_id: str, *, tenant_ref: str) -> KnowledgeVersion | None:
        raise NotImplementedError

    @abstractmethod
    def list_active_versions(
        self,
        *,
        tenant_ref: str,
        scope: MemoryScope,
        source_id: str | None = None,
    ) -> tuple[KnowledgeVersion, ...]:
        raise NotImplementedError

    @abstractmethod
    def find_version_by_hash(
        self,
        *,
        tenant_ref: str,
        scope: MemoryScope,
        source_id: str,
        content_hash: str,
    ) -> KnowledgeVersion | None:
        raise NotImplementedError

    @abstractmethod
    def supersede_version(self, old_version_id: str, new_version_id: str, *, tenant_ref: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_chunks(
        self,
        *,
        tenant_ref: str,
        version_id: str | None = None,
        knowledge_id: str | None = None,
        active_only: bool = True,
    ) -> tuple[KnowledgeChunk, ...]:
        raise NotImplementedError

    @abstractmethod
    def save_index_records(self, records: list[KnowledgeIndexRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_index_records(
        self,
        *,
        tenant_ref: str,
        embedding_model: str,
        embedding_version: str,
        active_only: bool = True,
    ) -> tuple[KnowledgeIndexRecord, ...]:
        raise NotImplementedError

    @abstractmethod
    def tombstone_knowledge(
        self,
        *,
        tenant_ref: str,
        knowledge_id: str | None = None,
        source_id: str | None = None,
        version_id: str | None = None,
    ) -> DeletionReceipt:
        raise NotImplementedError

    @abstractmethod
    def save_job(self, job: KnowledgeIngestionJob) -> KnowledgeIngestionJob:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str, *, tenant_ref: str) -> KnowledgeIngestionJob | None:
        raise NotImplementedError

    @abstractmethod
    def expire_before(self, *, tenant_ref: str, before_iso: str) -> int:
        raise NotImplementedError
