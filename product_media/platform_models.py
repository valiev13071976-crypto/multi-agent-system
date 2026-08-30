"""Block 10 domain contracts — product media intelligence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import require_tenant_id

PLATFORM_SCHEMA_VERSION = "1.0.0"
INGEST_PROFILE_VERSION = "1.0.0"
QUALITY_PROFILE_VERSION = "1.0.0"
SIMILARITY_PROFILE_VERSION = "1.0.0"
TRANSFORM_PROFILE_VERSION = "1.0.0"
GENERATION_PROFILE_VERSION = "1.0.0"

STATUS_ACTIVE = "active"
STATUS_TOMBSTONED = "tombstoned"
STATUS_DELETED = "deleted"
STATUS_FAILED = "failed"

LINK_CONFIRMED = "CONFIRMED"
LINK_CANDIDATE = "CANDIDATE"
LINK_AMBIGUOUS = "AMBIGUOUS"
LINK_UNMATCHED = "UNMATCHED"

ROLE_HERO = "hero"
ROLE_FRONT = "front"
ROLE_BACK = "back"
ROLE_DETAIL = "detail"
ROLE_LIFESTYLE = "lifestyle"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ImageMetadata:
    width: int
    height: int
    format: str
    mime_type: str
    byte_size: int
    aspect_ratio: float
    has_alpha: bool = False
    orientation: int = 1
    color_mode: str = "RGB"


@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    duration_sec: float
    byte_size: int
    container: str = "mp4"
    codec: str = "unknown"


@dataclass(frozen=True)
class MediaAssetVersion:
    media_id: str
    version_id: str
    tenant_id: str
    content_hash: str
    mime_type: str
    media_type: str  # image | video
    byte_size: int
    width: int
    height: int
    status: str
    operation: str
    parent_version_id: str | None = None
    transform_profile: str = TRANSFORM_PROFILE_VERSION
    provider_id: str = "local"
    artifact_id: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class ProductMediaLink:
    link_id: str
    tenant_id: str
    media_version_id: str
    product_id: str
    sku: str = ""
    link_state: str = LINK_CANDIDATE
    source: str = "explicit"
    evidence_ref: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MediaFingerprint:
    fingerprint_id: str
    tenant_id: str
    version_id: str
    content_hash: str
    perceptual_hash: str
    profile_version: str = SIMILARITY_PROFILE_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MediaQualityIssue:
    code: str
    message: str
    severity: str = "warning"


@dataclass(frozen=True)
class MediaQualityReport:
    report_id: str
    tenant_id: str
    version_id: str
    profile_version: str
    issues: tuple[MediaQualityIssue, ...]
    measurements: Mapping[str, object] = field(default_factory=dict)
    passed: bool = True

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "measurements", _meta(self.measurements))


@dataclass(frozen=True)
class MediaSimilarityResult:
    query_version_id: str
    candidate_version_id: str
    tenant_id: str
    method: str
    score: float
    classification: str  # duplicate | similar | unrelated
    profile_version: str = SIMILARITY_PROFILE_VERSION


@dataclass(frozen=True)
class ProductMediaSet:
    set_id: str
    tenant_id: str
    product_id: str
    items: tuple[Mapping[str, str], ...]  # role -> version_id
    profile_version: str = QUALITY_PROFILE_VERSION
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MediaJob:
    job_id: str
    tenant_id: str
    operation: str
    status: str
    stage: str = "ingest"
    checkpoint: int = 0
    total: int = 0
    profile_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MediaDeletionResult:
    deletion_id: str
    tenant_id: str
    version_id: str
    status: str
    fingerprints_removed: int = 0

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
