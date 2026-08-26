"""P15 External Knowledge / RAG models."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from memory.models import MemoryScope, utc_now
from security.encryption import (
    SENSITIVITY_INTERNAL,
    SENSITIVITY_SECRET,
    SENSITIVITY_SENSITIVE,
)

KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_POLICY_VERSION = "1.0.0"
KNOWLEDGE_RETRIEVAL_VERSION = "1.0.0"
KNOWLEDGE_SOURCE_REGISTRY_VERSION = "1.0.0"

SOURCE_MEMORY = "memory"
SOURCE_DOCUMENT = "document"
SOURCE_LOCAL_FILE = "local_file"
SOURCE_READ_ONLY_EXTERNAL = "read_only_external"
SOURCE_SEARCH_PROVIDER = "search_provider"
SOURCE_MANUAL_REFERENCE = "manual_reference"
SOURCE_TYPES = (
    SOURCE_MEMORY,
    SOURCE_DOCUMENT,
    SOURCE_LOCAL_FILE,
    SOURCE_READ_ONLY_EXTERNAL,
    SOURCE_SEARCH_PROVIDER,
    SOURCE_MANUAL_REFERENCE,
)

TRUST_SYSTEM = "system_trusted"
TRUST_OPERATOR = "operator_trusted"
TRUST_VALIDATED_INTERNAL = "validated_internal"
TRUST_DOCUMENT = "document_sourced"
TRUST_READ_ONLY_EXTERNAL = "read_only_external"
TRUST_UNVERIFIED = "unverified_external"
KNOWLEDGE_TRUST_LEVELS = (
    TRUST_SYSTEM,
    TRUST_OPERATOR,
    TRUST_VALIDATED_INTERNAL,
    TRUST_DOCUMENT,
    TRUST_READ_ONLY_EXTERNAL,
    TRUST_UNVERIFIED,
)

TRUST_RANK = {
    TRUST_SYSTEM: 6,
    TRUST_OPERATOR: 5,
    TRUST_VALIDATED_INTERNAL: 4,
    TRUST_DOCUMENT: 3,
    TRUST_READ_ONLY_EXTERNAL: 2,
    TRUST_UNVERIFIED: 1,
}

STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"
STATUS_DELETED = "deleted"
KNOWLEDGE_STATUSES = (
    STATUS_ACTIVE,
    STATUS_STALE,
    STATUS_SUPERSEDED,
    STATUS_EXPIRED,
    STATUS_DELETED,
)

FRESHNESS_STATIC = "static"
FRESHNESS_TTL = "ttl"
FRESHNESS_MANUAL = "manual_refresh"
FRESHNESS_ON_DEMAND = "on_demand"
FRESHNESS_POLICIES = (
    FRESHNESS_STATIC,
    FRESHNESS_TTL,
    FRESHNESS_MANUAL,
    FRESHNESS_ON_DEMAND,
)

SENSITIVITIES = (SENSITIVITY_INTERNAL, SENSITIVITY_SENSITIVE, SENSITIVITY_SECRET)

DEFAULT_MAX_SOURCES = 64
DEFAULT_MAX_RESULTS = 10
DEFAULT_MAX_ITEM_BYTES = 32_768
DEFAULT_MAX_CONTEXT_BYTES = 64_000
DEFAULT_TTL_SECONDS = 86_400
MAX_QUERY_CHARS = 2_000

_WHITESPACE_RE = re.compile(r"\s+")


def content_hash_text(text: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", str(text or "").strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_knowledge_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(text or "").strip())


def citation_ref_for(
    *,
    knowledge_id: str | None = None,
    memory_id: str | None = None,
    document_id: str | None = None,
    chunk_id: str | None = None,
    source_id: str | None = None,
    safe_ref: str | None = None,
) -> str:
    if memory_id:
        return f"memory:{memory_id}"
    if document_id and chunk_id:
        return f"document:{document_id}#chunk:{chunk_id}"
    if document_id:
        return f"document:{document_id}"
    if knowledge_id:
        return f"knowledge:{knowledge_id}"
    if source_id and safe_ref:
        safe = re.sub(r"[^a-zA-Z0-9._\-:/]", "_", str(safe_ref))[:120]
        return f"external:{source_id}:{safe}"
    if source_id:
        return f"external:{source_id}"
    return "knowledge:unknown"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _ensure_utc(stamp: datetime | None) -> datetime | None:
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreshnessPolicy:
    policy: str = FRESHNESS_TTL
    ttl_seconds: int | None = DEFAULT_TTL_SECONDS
    max_stale_seconds: int | None = None
    allow_stale: bool = False
    refresh_required: bool = False

    def __post_init__(self):
        if self.policy not in FRESHNESS_POLICIES:
            raise ValueError(f"invalid_freshness_policy:{self.policy}")


@dataclass(frozen=True)
class KnowledgeProvenance:
    source_id: str
    source_type: str
    source_ref: str
    ingested_at: datetime
    source_hash: str = ""
    trust_level: str = TRUST_UNVERIFIED
    validation_state: str = "unvalidated"
    document_id: str | None = None
    chunk_id: str | None = None
    tool_id: str | None = None
    external_reference: str | None = None
    retrieved_at: datetime | None = None

    def __post_init__(self):
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if self.trust_level not in KNOWLEDGE_TRUST_LEVELS:
            raise ValueError(f"invalid_trust_level:{self.trust_level}")
        if not str(self.source_id or "").strip():
            raise ValueError("source_id_required")
        if not str(self.source_ref or "").strip():
            raise ValueError("source_ref_required")
        object.__setattr__(self, "ingested_at", _ensure_utc(self.ingested_at) or utc_now())
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", _ensure_utc(self.retrieved_at))


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: str
    scope: MemoryScope
    source_type: str
    name: str
    trust_level: str
    enabled: bool = True
    refresh_policy: FreshnessPolicy = field(default_factory=FreshnessPolicy)
    freshness_ttl: int | None = DEFAULT_TTL_SECONDS
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    version: int = 1
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if self.trust_level not in KNOWLEDGE_TRUST_LEVELS:
            raise ValueError(f"invalid_trust_level:{self.trust_level}")
        if not str(self.source_id or "").strip():
            raise ValueError("source_id_required")
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "updated_at", _ensure_utc(self.updated_at) or utc_now())
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class KnowledgeItem:
    knowledge_id: str
    scope: MemoryScope
    source_id: str
    content: str
    content_hash: str
    trust_level: str
    provenance: KnowledgeProvenance
    sensitivity: str
    status: str
    created_at: datetime
    updated_at: datetime
    summary_safe: str | None = None
    confidence: float | None = None
    freshness: str = FRESHNESS_TTL
    expires_at: datetime | None = None
    version: int = 1
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    memory_id: str | None = None

    def __post_init__(self):
        if self.trust_level not in KNOWLEDGE_TRUST_LEVELS:
            raise ValueError(f"invalid_trust_level:{self.trust_level}")
        if self.status not in KNOWLEDGE_STATUSES:
            raise ValueError(f"invalid_status:{self.status}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid_sensitivity:{self.sensitivity}")
        if self.confidence is not None:
            c = float(self.confidence)
            if c < 0.0 or c > 1.0:
                raise ValueError("invalid_confidence")
            object.__setattr__(self, "confidence", c)
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "updated_at", _ensure_utc(self.updated_at) or utc_now())
        object.__setattr__(self, "expires_at", _ensure_utc(self.expires_at))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))

    @property
    def citation_ref(self) -> str:
        return citation_ref_for(
            knowledge_id=self.knowledge_id,
            memory_id=self.memory_id,
            document_id=self.provenance.document_id,
            chunk_id=self.provenance.chunk_id,
            source_id=self.source_id,
        )


@dataclass(frozen=True)
class KnowledgeIngestRequest:
    scope: MemoryScope
    source_id: str
    content: str
    trust_level: str
    provenance_source_ref: str
    sensitivity: str = SENSITIVITY_INTERNAL
    tags: tuple[str, ...] = ()
    freshness: FreshnessPolicy | None = None
    confidence: float | None = None
    persist: bool = True
    validated: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    document_id: str | None = None
    chunk_id: str | None = None
    tool_id: str | None = None
    external_reference: str | None = None

    def __post_init__(self):
        if self.trust_level not in KNOWLEDGE_TRUST_LEVELS:
            raise ValueError(f"invalid_trust_level:{self.trust_level}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid_sensitivity:{self.sensitivity}")
        object.__setattr__(self, "tags", tuple(str(t) for t in self.tags))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class KnowledgeQuery:
    query_text: str
    scope: MemoryScope
    source_ids: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    trust_min: str | None = None
    freshness_required: bool = False
    memory_types: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    limit: int = DEFAULT_MAX_RESULTS
    include_stale: bool = False
    allow_ephemeral_external: bool = False

    def __post_init__(self):
        text = str(self.query_text or "")
        if len(text) > MAX_QUERY_CHARS:
            raise ValueError("knowledge_query_too_large")
        # Reject arbitrary URL as query targeting mechanism
        lowered = text.strip().lower()
        if lowered.startswith(("http://", "https://", "file://")):
            raise ValueError("arbitrary_url_query_denied")
        limit = max(1, min(int(self.limit), 20))
        object.__setattr__(self, "limit", limit)
        object.__setattr__(self, "source_ids", tuple(self.source_ids or ()))
        object.__setattr__(self, "source_types", tuple(self.source_types or ()))
        object.__setattr__(self, "memory_types", tuple(self.memory_types or ()))
        object.__setattr__(self, "document_types", tuple(self.document_types or ()))
        object.__setattr__(self, "tags", tuple(self.tags or ()))
        if self.trust_min is not None and self.trust_min not in KNOWLEDGE_TRUST_LEVELS:
            raise ValueError(f"invalid_trust_min:{self.trust_min}")


@dataclass(frozen=True)
class KnowledgeResult:
    knowledge_id: str
    content: str
    score: float
    source_id: str
    source_type: str
    trust_level: str
    freshness: str
    stale: bool
    provenance: KnowledgeProvenance
    citation_ref: str
    confidence: float | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
