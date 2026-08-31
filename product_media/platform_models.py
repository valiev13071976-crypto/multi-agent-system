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
RECIPE_PROFILE_VERSION = "1.0.0"
TARGET_PROFILE_VERSION = "1.0.0"
TEMPLATE_PROFILE_VERSION = "1.0.0"
VIDEO_PROFILE_VERSION = "1.0.0"

STATUS_ACTIVE = "active"
STATUS_TOMBSTONED = "tombstoned"
STATUS_DELETED = "deleted"
STATUS_FAILED = "failed"
STATUS_REVIEW_REQUIRED = "review_required"
STATUS_CANCELLED = "cancelled"

LINK_CONFIRMED = "CONFIRMED"
LINK_CANDIDATE = "CANDIDATE"
LINK_AMBIGUOUS = "AMBIGUOUS"
LINK_UNMATCHED = "UNMATCHED"

ROLE_HERO = "hero"
ROLE_FRONT = "front"
ROLE_BACK = "back"
ROLE_DETAIL = "detail"
ROLE_LIFESTYLE = "lifestyle"
ROLE_MAIN = "main"
ROLE_GALLERY = "gallery"
ROLE_INFOGRAPHIC = "infographic"
ROLE_PACKAGE = "package"

RIGHTS_UNKNOWN = "UNKNOWN"
RIGHTS_OWNED = "OWNED"
RIGHTS_LICENSED = "LICENSED"
RIGHTS_USER_PROVIDED = "USER_PROVIDED"
RIGHTS_GENERATED = "GENERATED"
RIGHTS_THIRD_PARTY_RESTRICTED = "THIRD_PARTY_RESTRICTED"

SOURCE_UPLOAD = "UPLOAD"
SOURCE_FILE_ARTIFACT = "FILE_ARTIFACT"
SOURCE_GENERATED = "GENERATED"
SOURCE_CONTENT_FACTORY = "CONTENT_FACTORY"
SOURCE_PRODUCT_CATALOG = "PRODUCT_CATALOG"

MAX_GENERATION_ATTEMPTS = 3
MAX_QUALITY_RETRIES = 2
MAX_VARIANTS_HARD = 4


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
    rights_status: str = RIGHTS_UNKNOWN
    source_content_hash: str = ""
    recipe_id: str = ""
    recipe_version: str = ""
    target_profile_id: str = ""

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


@dataclass(frozen=True)
class MediaRights:
    rights_id: str
    tenant_id: str
    status: str = RIGHTS_UNKNOWN
    rights_holder: str = ""
    license_ref: str = ""
    allowed_uses: tuple[str, ...] = ()
    expiry: str = ""
    source_provenance: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "allowed_uses", tuple(self.allowed_uses or ()))


@dataclass(frozen=True)
class MediaSource:
    source_id: str
    tenant_id: str
    source_kind: str
    media_type: str
    mime: str
    content_hash: str
    byte_size: int
    artifact_id: str = ""
    filename: str = ""
    product_id: str = ""
    project_id: str = ""
    rights_status: str = RIGHTS_UNKNOWN
    created_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MediaOperation:
    name: str
    version: str = "1.0.0"
    parameters: Mapping[str, object] = field(default_factory=dict)
    deterministic: bool = True
    provider_required: bool = False

    def __post_init__(self):
        object.__setattr__(self, "parameters", _meta(self.parameters))


@dataclass(frozen=True)
class MediaRecipe:
    recipe_id: str
    version: str
    tenant_id: str
    operations: tuple[MediaOperation, ...]
    target_profile_id: str = ""
    profile_version: str = RECIPE_PROFILE_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "operations", tuple(self.operations or ()))


@dataclass(frozen=True)
class TargetMediaProfile:
    profile_id: str
    channel: str
    asset_role: str
    width: int
    height: int
    format: str = "jpeg"
    max_bytes: int = 5 * 1024 * 1024
    allow_alpha: bool = False
    background: str = "white"
    version: str = TARGET_PROFILE_VERSION
    source_of_rules: str = "configurable"
    effective_at: str = ""
    safe_margin_pct: float = 0.05

    @property
    def aspect_ratio(self) -> float:
        return round(self.width / max(self.height, 1), 4)


@dataclass(frozen=True)
class MediaTemplate:
    template_id: str
    tenant_id: str
    version: str
    canvas_width: int
    canvas_height: int
    product_zone: Mapping[str, int]
    text_zones: tuple[Mapping[str, object], ...]
    profile_version: str = TEMPLATE_PROFILE_VERSION
    channel: str = "marketplace"

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "product_zone", dict(self.product_zone or {}))
        object.__setattr__(self, "text_zones", tuple(self.text_zones or ()))


@dataclass(frozen=True)
class ProductMediaContext:
    tenant_id: str
    product_id: str
    sku: str = ""
    brand: str = ""
    category: str = ""
    product_facts: Mapping[str, str] = field(default_factory=dict)
    source_version_ids: tuple[str, ...] = ()
    target_channels: tuple[str, ...] = ()
    media_brief_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "product_facts", dict(self.product_facts or {}))
        object.__setattr__(self, "source_version_ids", tuple(self.source_version_ids or ()))
        object.__setattr__(self, "target_channels", tuple(self.target_channels or ()))


@dataclass(frozen=True)
class VideoScene:
    scene_id: str
    start_sec: float
    end_sec: float
    source_version_id: str = ""
    text_overlay: str = ""
    transition: str = "cut"


@dataclass(frozen=True)
class VideoRecipe:
    recipe_id: str
    tenant_id: str
    version: str
    scenes: tuple[VideoScene, ...]
    aspect_ratio: str = "9:16"
    duration_sec: float = 15.0
    target_profile_id: str = "video_short_9x16"
    audio_refs: tuple[str, ...] = ()
    rights_status: str = RIGHTS_UNKNOWN
    profile_version: str = VIDEO_PROFILE_VERSION
    media_brief_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "scenes", tuple(self.scenes or ()))
        object.__setattr__(self, "audio_refs", tuple(self.audio_refs or ()))


@dataclass(frozen=True)
class MediaQualityResult:
    result_id: str
    tenant_id: str
    version_id: str
    profile_id: str
    passed: bool
    issues: tuple[MediaQualityIssue, ...]
    rights_status: str = RIGHTS_UNKNOWN
    fidelity_review_required: bool = False
    profile_version: str = QUALITY_PROFILE_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "issues", tuple(self.issues or ()))
