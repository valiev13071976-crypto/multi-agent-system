"""Block 8 domain contracts — versioned knowledge, chunks, retrieval, lifecycle."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from memory.models import MemoryScope, utc_now

KNOWLEDGE_INGESTION_PROFILE_VERSION = "1.0.0"
KNOWLEDGE_CHUNKER_VERSION = "1.0.0"
KNOWLEDGE_INDEX_VERSION = "1.0.0"
EMBEDDING_MODEL_FAKE = "fake-deterministic-v1"
EMBEDDING_DIM = 32

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"
STATUS_DELETED = "deleted"
STATUS_TOMBSTONED = "tombstoned"
STATUS_PURGE_PENDING = "purge_pending"
STATUS_PURGED = "purged"

INGEST_STAGE_VALIDATE = "SOURCE_VALIDATE"
INGEST_STAGE_PARSE = "PARSE"
INGEST_STAGE_NORMALIZE = "NORMALIZE"
INGEST_STAGE_CHUNK = "CHUNK"
INGEST_STAGE_METADATA = "METADATA"
INGEST_STAGE_EMBED = "EMBED"
INGEST_STAGE_INDEX = "INDEX"
INGEST_STAGE_VALIDATE_OUT = "VALIDATE"
INGEST_STAGE_COMPLETE = "COMPLETE"

RETRIEVAL_KEYWORD = "keyword"
RETRIEVAL_VECTOR = "vector"
RETRIEVAL_HYBRID = "hybrid"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _ensure_utc(stamp: datetime | None) -> datetime | None:
    if stamp is None:
        return None
    from datetime import timezone

    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class KnowledgeVersion:
    version_id: str
    knowledge_id: str
    tenant_ref: str
    source_id: str
    content_hash: str
    version_num: int
    status: str
    parser_version: str = KNOWLEDGE_INGESTION_PROFILE_VERSION
    chunker_version: str = KNOWLEDGE_CHUNKER_VERSION
    embedding_model: str = EMBEDDING_MODEL_FAKE
    embedding_version: str = "1"
    index_version: str = KNOWLEDGE_INDEX_VERSION
    supersedes_version_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    version_id: str
    knowledge_id: str
    tenant_ref: str
    sequence: int
    content: str
    content_hash: str
    token_estimate: int
    scope: MemoryScope
    source_id: str
    status: str = STATUS_ACTIVE
    page_ref: str | None = None
    section_ref: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    overlap_prev: int = 0
    created_at: datetime = field(default_factory=utc_now)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class KnowledgeIndexRecord:
    record_id: str
    chunk_id: str
    version_id: str
    knowledge_id: str
    tenant_ref: str
    embedding_model: str
    embedding_version: str
    embedding_dim: int
    index_version: str
    vector: tuple[float, ...]
    status: str = STATUS_ACTIVE
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk_id: str
    version_id: str
    knowledge_id: str
    tenant_ref: str
    content: str
    score: float
    retrieval_method: str
    source_id: str
    citation_ref: str
    stale: bool = False
    superseded: bool = False
    score_components: Mapping[str, float] = field(default_factory=dict)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "score_components", _meta(self.score_components))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class RetrievalResult:
    request_id: str
    tenant_ref: str
    profile_version: str
    candidates: tuple[RetrievalCandidate, ...]
    no_results: bool
    truncated: bool
    warnings: tuple[str, ...] = ()
    timing_ms: int = 0
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class KnowledgeIngestionJob:
    job_id: str
    tenant_ref: str
    source_id: str
    stage: str
    status: str
    content_hash: str = ""
    checkpoint: int = 0
    chunk_total: int = 0
    retry_count: int = 0
    profile_version: str = KNOWLEDGE_INGESTION_PROFILE_VERSION
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class DeletionRequest:
    tenant_ref: str
    target_knowledge_id: str | None = None
    target_source_id: str | None = None
    target_version_id: str | None = None
    scope: MemoryScope | None = None
    reason: str = "user_request"


@dataclass(frozen=True)
class DeletionReceipt:
    deletion_id: str
    tenant_ref: str
    status: str
    affected_versions: int = 0
    affected_chunks: int = 0
    affected_index_records: int = 0
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    policy_version: str = "1.0.0"
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetentionPolicy:
    policy_version: str = "1.0.0"
    scope_category: str = "tenant"
    ttl_seconds: int | None = None
    expire_behavior: str = "tombstone"
    purge_derivatives: bool = True


def chunk_content_hash(content: str) -> str:
    normalized = " ".join(str(content or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
