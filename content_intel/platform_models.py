"""Block 9 domain contracts — versioned content factory objects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id, require_tenant_id

PLATFORM_SCHEMA_VERSION = "1.0.0"
RESEARCH_PROFILE_VERSION = "1.0.0"
STRATEGY_PROFILE_VERSION = "1.0.0"
GENERATION_PROFILE_VERSION = "1.0.0"
ANALYTICS_PROFILE_VERSION = "1.0.0"
OPTIMIZATION_PROFILE_VERSION = "1.0.0"

STATUS_DRAFT = "DRAFT"
STATUS_VALIDATED = "VALIDATED"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_SCHEDULED = "SCHEDULED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_FAILED = "FAILED"
STATUS_ARCHIVED = "ARCHIVED"
STATUS_STALE = "STALE"

GROUNDING_SUPPORTED = "SUPPORTED"
GROUNDING_PARTIAL = "PARTIALLY_SUPPORTED"
GROUNDING_UNSUPPORTED = "UNSUPPORTED"
GROUNDING_CONFLICTING = "CONFLICTING"

PROVENANCE_EVIDENCE = "SOURCE_EVIDENCE"
PROVENANCE_DETERMINISTIC = "DETERMINISTIC_ANALYSIS"
PROVENANCE_MODEL = "MODEL_GENERATED_INFERENCE"
PROVENANCE_CREATIVE = "CREATIVE_CONTENT"

OBSERVED = "OBSERVED"
INFERRED = "INFERRED"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContentProject:
    project_id: str
    tenant_id: str
    name: str
    owner_ref: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class ContentObjective:
    objective_id: str
    project_id: str
    tenant_id: str
    goal: str
    channels: tuple[str, ...] = ()
    kpis: tuple[str, ...] = ()
    constraints: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "channels", tuple(self.channels or ()))
        object.__setattr__(self, "kpis", tuple(self.kpis or ()))
        object.__setattr__(self, "constraints", _meta(self.constraints))


@dataclass(frozen=True)
class AudienceProfile:
    profile_id: str
    tenant_id: str
    project_id: str
    segments: tuple[str, ...]
    version: int = 1
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "segments", tuple(self.segments or ()))


@dataclass(frozen=True)
class BrandProfile:
    profile_id: str
    tenant_id: str
    project_id: str
    tone: str
    forbidden_terms: tuple[str, ...] = ()
    approved_facts: Mapping[str, str] = field(default_factory=dict)
    disclaimers: tuple[str, ...] = ()
    version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "forbidden_terms", tuple(self.forbidden_terms or ()))
        object.__setattr__(self, "approved_facts", dict(self.approved_facts or {}))
        object.__setattr__(self, "disclaimers", tuple(self.disclaimers or ()))


@dataclass(frozen=True)
class ResearchEvidence:
    evidence_id: str
    tenant_id: str
    source_type: str
    source_ref: str
    label: str
    extracted_claim: str
    content_hash: str
    retrieved_at: datetime
    trust_level: str = "unverified_external"
    relevance: float = 0.0
    publication_date: datetime | None = None
    warnings: tuple[str, ...] = ()
    provenance_kind: str = PROVENANCE_EVIDENCE

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))


@dataclass(frozen=True)
class ResearchReport:
    report_id: str
    tenant_id: str
    project_id: str
    objective_id: str
    evidence: tuple[ResearchEvidence, ...]
    grounding: str
    profile_version: str = RESEARCH_PROFILE_VERSION
    created_at: datetime = field(default_factory=_utc_now)
    conflicts: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class CompetitorProfile:
    competitor_id: str
    tenant_id: str
    name: str
    category: str
    observations: tuple[Mapping[str, object], ...]
    evidence_refs: tuple[str, ...]
    observation_kind: str = OBSERVED

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class TrendSignal:
    trend_id: str
    tenant_id: str
    topic: str
    magnitude: float
    velocity: float
    first_observed: datetime
    last_observed: datetime
    evidence_count: int
    status: str = STATUS_DRAFT
    confidence_label: str = "low"

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class ContentStrategy:
    strategy_id: str
    version_id: str
    tenant_id: str
    project_id: str
    version_num: int
    pillars: tuple[str, ...]
    channel_roles: Mapping[str, str]
    messaging_principles: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    parent_version_id: str | None = None
    profile_version: str = STRATEGY_PROFILE_VERSION
    provenance_kind: str = PROVENANCE_MODEL

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "pillars", tuple(self.pillars or ()))
        object.__setattr__(self, "channel_roles", dict(self.channel_roles or {}))


@dataclass(frozen=True)
class ContentIdea:
    idea_id: str
    version_id: str
    tenant_id: str
    project_id: str
    channel: str
    concept: str
    angle: str
    evidence_refs: tuple[str, ...] = ()
    version_num: int = 1

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class ContentHook:
    hook_id: str
    tenant_id: str
    idea_id: str
    text: str
    channel: str
    version_num: int = 1

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class ContentScript:
    script_id: str
    version_id: str
    tenant_id: str
    idea_id: str
    hook: str
    beats: tuple[str, ...]
    on_screen_text: tuple[str, ...]
    cta: str
    estimated_duration_sec: int
    duration_estimate_profile: str = "words_per_minute_v1"
    version_num: int = 1

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "beats", tuple(self.beats or ()))
        object.__setattr__(self, "on_screen_text", tuple(self.on_screen_text or ()))


@dataclass(frozen=True)
class ContentAssetVersion:
    asset_id: str
    version_id: str
    tenant_id: str
    project_id: str
    content_type: str
    channel: str
    body: str
    status: str
    version_num: int
    parent_version_id: str | None = None
    strategy_version_id: str | None = None
    idea_id: str | None = None
    product_facts_used: Mapping[str, str] = field(default_factory=dict)
    missing_facts: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    provenance_kind: str = PROVENANCE_CREATIVE
    generation_profile: str = GENERATION_PROFILE_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "product_facts_used", dict(self.product_facts_used or {}))
        object.__setattr__(self, "missing_facts", tuple(self.missing_facts or ()))
        object.__setattr__(self, "validation_errors", tuple(self.validation_errors or ()))


@dataclass(frozen=True)
class MediaBrief:
    brief_id: str
    tenant_id: str
    asset_version_id: str
    media_type: str
    aspect_ratio: str
    scene_description: str
    generation_profile: str = GENERATION_PROFILE_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MediaAssetRef:
    ref_id: str
    tenant_id: str
    brief_id: str
    artifact_id: str
    provider_id: str
    status: str
    content_hash: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class PublicationItem:
    item_id: str
    asset_version_id: str
    channel: str
    scheduled_at: datetime
    timezone: str
    status: str = STATUS_SCHEDULED

    def __post_init__(self):
        if not str(self.timezone or "").strip():
            raise ValueError("timezone_required")


@dataclass(frozen=True)
class PublicationPlan:
    plan_id: str
    version_id: str
    tenant_id: str
    project_id: str
    version_num: int
    items: tuple[PublicationItem, ...]
    parent_version_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class PerformanceObservation:
    observation_id: str
    tenant_id: str
    asset_version_id: str
    channel: str
    metric_name: str
    metric_value: Decimal | None
    unit: str
    window_start: datetime
    window_end: datetime
    source: str
    collected_at: datetime = field(default_factory=_utc_now)
    status: str = "present"

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class PerformanceReport:
    report_id: str
    tenant_id: str
    project_id: str
    observations: tuple[PerformanceObservation, ...]
    metrics_computed: Mapping[str, object]
    limitations: tuple[str, ...] = ()
    profile_version: str = ANALYTICS_PROFILE_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "metrics_computed", _meta(self.metrics_computed))


@dataclass(frozen=True)
class ContentExperiment:
    experiment_id: str
    tenant_id: str
    hypothesis: str
    variant_version_ids: tuple[str, ...]
    target_metric: str
    status: str = STATUS_DRAFT
    outcome: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "variant_version_ids", tuple(self.variant_version_ids or ()))


@dataclass(frozen=True)
class OptimizationDecision:
    decision_id: str
    tenant_id: str
    project_id: str
    strategy_version_id: str
    asset_version_ids: tuple[str, ...]
    observation_window: tuple[datetime, datetime]
    hypothesis: str
    recommended_action: str
    confidence_label: str
    limitations: tuple[str, ...]
    profile_version: str = OPTIMIZATION_PROFILE_VERSION
    idempotency_key: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "asset_version_ids", tuple(self.asset_version_ids or ()))
        object.__setattr__(self, "limitations", tuple(self.limitations or ()))


@dataclass(frozen=True)
class ContentJob:
    job_id: str
    tenant_id: str
    project_id: str
    stage: str
    status: str
    checkpoint: int = 0
    total: int = 0
    profile_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
