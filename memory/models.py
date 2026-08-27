"""P13 Memory / Knowledge models."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.encryption import (
    SENSITIVITY_INTERNAL,
    SENSITIVITY_SECRET,
    SENSITIVITY_SENSITIVE,
)


MEMORY_SCHEMA_VERSION = 1
MEMORY_POLICY_VERSION = "1.0.0"
MEMORY_RETRIEVAL_VERSION = "1.0.0"

MEMORY_EPISODIC = "episodic"
MEMORY_SEMANTIC = "semantic"
MEMORY_PROCEDURAL = "procedural"
MEMORY_WORKING_REFERENCE = "working_reference"
MEMORY_TYPES = (
    MEMORY_EPISODIC,
    MEMORY_SEMANTIC,
    MEMORY_PROCEDURAL,
    MEMORY_WORKING_REFERENCE,
)

SCOPE_GLOBAL_SYSTEM = "global_system"
SCOPE_WORKSPACE = "workspace"
SCOPE_PROJECT = "project"
SCOPE_WORKFLOW = "workflow"
SCOPE_AGENT = "agent"
SCOPE_TYPES = (
    SCOPE_GLOBAL_SYSTEM,
    SCOPE_WORKSPACE,
    SCOPE_PROJECT,
    SCOPE_WORKFLOW,
    SCOPE_AGENT,
)

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"
STATUS_DELETED = "deleted"
MEMORY_STATUSES = (STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_EXPIRED, STATUS_DELETED)

SOURCE_USER_INPUT = "user_input"
SOURCE_WORKFLOW_RESULT = "workflow_result"
SOURCE_TOOL_RESULT = "tool_result"
SOURCE_DOCUMENT = "document"
SOURCE_SYSTEM = "system_generated"
SOURCE_OPERATOR = "operator"
SOURCE_EXTERNAL = "external_knowledge"
SOURCE_TYPES = (
    SOURCE_USER_INPUT,
    SOURCE_WORKFLOW_RESULT,
    SOURCE_TOOL_RESULT,
    SOURCE_DOCUMENT,
    SOURCE_SYSTEM,
    SOURCE_OPERATOR,
    SOURCE_EXTERNAL,
)

SENSITIVITIES = (
    SENSITIVITY_INTERNAL,
    SENSITIVITY_SENSITIVE,
    SENSITIVITY_SECRET,
)

LINK_SUPERSEDES = "supersedes"
LINK_DERIVED_FROM = "derived_from"
LINK_RELATED_TO = "related_to"
LINK_TYPES = (LINK_SUPERSEDES, LINK_DERIVED_FROM, LINK_RELATED_TO)

DEFAULT_MAX_RECORD_BYTES = 32_768
DEFAULT_QUERY_LIMIT = 10
MAX_QUERY_LIMIT = 20
MAX_QUERY_CHARS = 2_000

_WHITESPACE_RE = re.compile(r"\s+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def normalize_memory_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(text or "").strip())


def content_hash_for_memory(text: str) -> str:
    normalized = normalize_memory_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def citation_ref_for(memory_id: str) -> str:
    return f"memory:{memory_id}"


@dataclass(frozen=True)
class MemoryScope:
    scope_type: str
    scope_id: str
    workspace_id: str | None = None
    project_id: str | None = None
    actor_ref: str | None = None
    tenant_ref: str | None = None

    def __post_init__(self):
        if self.scope_type not in SCOPE_TYPES:
            raise ValueError(f"invalid_scope_type:{self.scope_type}")
        if not str(self.scope_id or "").strip():
            raise ValueError("scope_id_required")

    def key(self) -> tuple[str, str, str]:
        tenant = str(self.tenant_ref or "").strip() or "_"
        return (tenant, self.scope_type, self.scope_id)


@dataclass(frozen=True)
class MemoryProvenance:
    source_type: str
    source_id: str
    created_by_component: str
    ingested_at: datetime
    source_hash: str = ""
    workflow_id: str | None = None
    task_id: str | None = None
    tool_id: str | None = None
    external_reference: str | None = None
    version: int = 1

    def __post_init__(self):
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if not str(self.source_id or "").strip():
            raise ValueError("source_id_required")
        stamp = self.ingested_at
        if stamp.tzinfo is None:
            object.__setattr__(
                self, "ingested_at", stamp.replace(tzinfo=timezone.utc)
            )


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    memory_type: str
    scope: MemoryScope
    content_hash: str
    source_type: str
    source_ref: str
    provenance: MemoryProvenance
    sensitivity: str
    status: str
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    content_safe: str | None = None
    encrypted_content: str | None = None
    summary_safe: str | None = None
    confidence: float | None = None
    tags: tuple[str, ...] = ()
    expires_at: datetime | None = None
    version: int = 1
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"invalid_memory_type:{self.memory_type}")
        if self.status not in MEMORY_STATUSES:
            raise ValueError(f"invalid_memory_status:{self.status}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid_sensitivity:{self.sensitivity}")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if self.confidence is not None:
            c = float(self.confidence)
            if c < 0.0 or c > 1.0:
                raise ValueError("invalid_confidence")
            object.__setattr__(self, "confidence", c)
        object.__setattr__(self, "tags", tuple(str(t) for t in self.tags))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        for name in ("created_at", "updated_at", "expires_at"):
            stamp = getattr(self, name)
            if stamp is not None and stamp.tzinfo is None:
                object.__setattr__(
                    self, name, stamp.replace(tzinfo=timezone.utc)
                )

    @property
    def citation_ref(self) -> str:
        return citation_ref_for(self.memory_id)


@dataclass(frozen=True)
class MemoryIngestRequest:
    scope: MemoryScope
    memory_type: str
    content: str
    source_type: str
    source_id: str
    sensitivity: str = SENSITIVITY_INTERNAL
    confidence: float | None = None
    tags: tuple[str, ...] = ()
    title: str | None = None
    summary_safe: str | None = None
    created_by_component: str = "memory_service"
    workflow_id: str | None = None
    task_id: str | None = None
    tool_id: str | None = None
    external_reference: str | None = None
    retention_ttl_seconds: int | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"invalid_memory_type:{self.memory_type}")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid_sensitivity:{self.sensitivity}")
        object.__setattr__(self, "tags", tuple(str(t) for t in self.tags))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class MemoryQuery:
    query_text: str
    scope: MemoryScope
    memory_types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    min_confidence: float | None = None
    limit: int = DEFAULT_QUERY_LIMIT
    include_superseded: bool = False
    include_expired: bool = False

    def __post_init__(self):
        text = str(self.query_text or "")
        if len(text) > MAX_QUERY_CHARS:
            raise ValueError("memory_query_too_large")
        limit = int(self.limit)
        if limit < 1:
            limit = 1
        if limit > MAX_QUERY_LIMIT:
            limit = MAX_QUERY_LIMIT
        object.__setattr__(self, "limit", limit)
        object.__setattr__(self, "memory_types", tuple(self.memory_types or ()))
        object.__setattr__(self, "tags", tuple(self.tags or ()))
        object.__setattr__(self, "source_types", tuple(self.source_types or ()))


@dataclass(frozen=True)
class MemorySearchResult:
    memory_id: str
    score: float
    memory_type: str
    content_or_summary: str
    provenance: MemoryProvenance
    confidence: float | None
    created_at: datetime
    source_ref: str
    citation_ref: str
    sensitivity: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryLink:
    link_id: str
    from_memory_id: str
    to_memory_id: str
    link_type: str
    created_at: datetime

    def __post_init__(self):
        if self.link_type not in LINK_TYPES:
            raise ValueError(f"invalid_link_type:{self.link_type}")


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    scope: MemoryScope
    title: str
    source_type: str
    source_ref: str
    content_hash: str
    created_at: datetime
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    scope: MemoryScope
    ordinal: int
    content_hash: str
    source_location: str
    text_safe: str | None = None
    encrypted_text: str | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
