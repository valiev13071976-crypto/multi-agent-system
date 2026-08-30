"""Block 12 canonical SEO domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

TRUSTED_EXTERNAL = "TRUSTED_EXTERNAL_METRIC"
DETERMINISTIC = "DETERMINISTIC_OBSERVATION"
NORMALIZED = "NORMALIZED"
MODEL_INFERRED = "MODEL_INFERRED"
MODEL_GENERATED = "MODEL_GENERATED"
USER_CONFIRMED = "USER_CONFIRMED"
STALE = "STALE"
NOT_AVAILABLE = "NOT_AVAILABLE"

INTENT_INFORMATIONAL = "INFORMATIONAL"
INTENT_NAVIGATIONAL = "NAVIGATIONAL"
INTENT_COMMERCIAL = "COMMERCIAL"
INTENT_TRANSACTIONAL = "TRANSACTIONAL"
INTENT_LOCAL = "LOCAL"
INTENT_UNKNOWN = "UNKNOWN"

MAPPING_CONFIRMED = "CONFIRMED"
MAPPING_CANDIDATE = "CANDIDATE"
MAPPING_AMBIGUOUS = "AMBIGUOUS"
MAPPING_UNMAPPED = "UNMAPPED"
MAPPING_CONFLICT = "CONFLICT"

META_STATUS_DRAFT = "DRAFT"
META_STATUS_VALIDATED = "VALIDATED"
META_STATUS_APPROVED = "APPROVED"
META_STATUS_APPLIED = "APPLIED"
META_STATUS_STALE = "STALE"

ACTION_META_CHANGE = "META_CHANGE"
ACTION_CONTENT_CHANGE = "CONTENT_CHANGE"
ACTION_MEDIA_OPTIMIZATION = "MEDIA_OPTIMIZATION"
ACTION_TECHNICAL_FIX = "TECHNICAL_FIX_RECOMMENDATION"

DECISION_KEEP = "KEEP"
DECISION_REVISE = "REVISE"
DECISION_ROLLBACK_RECOMMENDED = "ROLLBACK_RECOMMENDED"
DECISION_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
DECISION_CONTINUE_MEASURING = "CONTINUE_MEASURING"

MEASURE_IMPROVED = "IMPROVED"
MEASURE_DECLINED = "DECLINED"
MEASURE_NO_CLEAR_CHANGE = "NO_CLEAR_CHANGE"
MEASURE_CONFOUNDED = "CONFOUNDED"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SeoProvenance:
    source: str
    observed_at: str
    retrieved_at: str
    trust_level: str
    source_version: str = ""
    execution_id: str = ""


@dataclass
class SeoSite:
    site_id: str
    tenant_id: str
    domain: str
    cms_binding: str = ""
    search_console_property: str = ""
    analytics_property: str = ""
    created_at: str = field(default_factory=_utc)


@dataclass
class SeoPage:
    page_id: str
    tenant_id: str
    site_id: str
    url: str
    canonical_url: str = ""
    product_id: str = ""
    version: int = 1
    created_at: str = field(default_factory=_utc)


@dataclass
class Keyword:
    keyword_id: str
    tenant_id: str
    site_id: str
    text: str
    normalized: str
    source: str
    provenance: SeoProvenance


@dataclass
class KeywordMetric:
    keyword_id: str
    metric: str
    value: Any
    unit: str
    trust_level: str
    provenance: SeoProvenance


@dataclass
class KeywordCluster:
    cluster_id: str
    tenant_id: str
    site_id: str
    label: str
    keyword_ids: tuple[str, ...]
    intent: str
    trust_level: str
    provenance: SeoProvenance


@dataclass
class KeywordOpportunity:
    opportunity_id: str
    tenant_id: str
    keyword_id: str
    score: float
    components: tuple[str, ...]
    trust_level: str


@dataclass
class KeywordPageMapping:
    mapping_id: str
    tenant_id: str
    keyword_id: str
    page_id: str
    state: str
    evidence: tuple[str, ...] = ()


@dataclass
class MetaSnapshot:
    snapshot_id: str
    tenant_id: str
    page_id: str
    page_version: int
    title: str
    description: str
    canonical: str
    robots: str
    provenance: SeoProvenance
    issues: tuple[str, ...] = ()


@dataclass
class MetaValidationResult:
    passed: bool
    issues: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass
class MetaRecommendation:
    recommendation_id: str
    tenant_id: str
    page_id: str
    page_version: int
    title: str
    description: str
    target_keyword_ids: tuple[str, ...]
    validation: MetaValidationResult
    status: str
    generator_version: str
    provenance: SeoProvenance
    created_at: str = field(default_factory=_utc)


@dataclass
class TechnicalSeoIssue:
    issue_id: str
    code: str
    severity: str
    url: str
    reason: str


@dataclass
class TechnicalSeoAudit:
    audit_id: str
    tenant_id: str
    site_id: str
    snapshot_id: str
    issues: tuple[TechnicalSeoIssue, ...]
    url_count: int
    provenance: SeoProvenance


@dataclass
class PerformanceObservation:
    observation_id: str
    tenant_id: str
    page_id: str
    metric: str
    value: float | None
    unit: str
    measurement_type: str
    provenance: SeoProvenance


@dataclass
class PerformanceAudit:
    audit_id: str
    tenant_id: str
    site_id: str
    observations: tuple[PerformanceObservation, ...]
    budget_violations: tuple[str, ...]
    provenance: SeoProvenance


@dataclass
class SearchConsoleSnapshot:
    snapshot_id: str
    tenant_id: str
    site_id: str
    property_id: str
    date_start: str
    date_end: str
    rows: tuple[dict, ...]
    retrieved_at: str
    freshness: str


@dataclass
class AnalyticsSnapshot:
    snapshot_id: str
    tenant_id: str
    site_id: str
    property_id: str
    date_start: str
    date_end: str
    rows: tuple[dict, ...]
    retrieved_at: str


@dataclass
class OptimizationPlan:
    plan_id: str
    tenant_id: str
    site_id: str
    version: int
    baseline_snapshot_ids: tuple[str, ...]
    actions: tuple[dict, ...]
    measurement_window_days: int
    status: str
    created_at: str = field(default_factory=_utc)


@dataclass
class OptimizationMeasurement:
    measurement_id: str
    tenant_id: str
    plan_id: str
    action_id: str
    outcome: str
    metrics: dict
    window_start: str
    window_end: str


@dataclass
class OptimizationDecision:
    decision_id: str
    tenant_id: str
    plan_id: str
    action_id: str
    decision: str
    attribution: str
    created_at: str = field(default_factory=_utc)


@dataclass
class SeoJob:
    job_id: str
    tenant_id: str
    operation: str
    checkpoint: int
    total: int
    status: str
    counts: dict
    payload: dict
    created_at: str = field(default_factory=_utc)
    updated_at: str = field(default_factory=_utc)
