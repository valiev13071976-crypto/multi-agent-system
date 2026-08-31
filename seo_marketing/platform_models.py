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


# --- Expansion contracts (closure) ---

PLATFORM_SCHEMA_VERSION = "1.0.0"
SEMANTIC_CORE_VERSION = "1.0.0"
CWV_BUDGET_VERSION = "1.0.0"
RANK_PROFILE_VERSION = "1.0.0"

# SeoSite is the canonical SEO project/site identity (SEOProject equivalent).
SEOProject = SeoSite

PAGE_TYPE_HOME = "HOME"
PAGE_TYPE_CATEGORY = "CATEGORY"
PAGE_TYPE_PRODUCT = "PRODUCT"
PAGE_TYPE_LANDING = "LANDING"
PAGE_TYPE_ARTICLE = "ARTICLE"
PAGE_TYPE_OTHER = "OTHER"
PAGE_TYPE_UNKNOWN = "UNKNOWN"

RANK_GAINED = "gained"
RANK_LOST = "lost"
RANK_IMPROVED = "improved"
RANK_DECLINED = "declined"
RANK_NEW = "new"
RANK_DROPPED = "dropped"
RANK_UNCHANGED = "unchanged"
RANK_UNKNOWN = "unknown"

OBSERVED_RANK = "OBSERVED_RANK"
ESTIMATED_VISIBILITY = "ESTIMATED_VISIBILITY"
INFERRED_OPPORTUNITY = "INFERRED_OPPORTUNITY"

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"
SEVERITY_INFO = "INFO"

MAX_RECOMMENDATIONS = 50
MAX_FEEDBACK_ACTIONS_PER_RUN = 1
MAX_LLM_CLUSTER_SAMPLE = 20


@dataclass(frozen=True)
class SEOEvidence:
    evidence_id: str
    tenant_id: str
    project_id: str
    source_kind: str
    source_ref: str
    observed_at: str
    payload_normalized: dict
    trust_level: str
    freshness: str = "current"
    warnings: tuple[str, ...] = ()


@dataclass
class SemanticCore:
    core_id: str
    tenant_id: str
    site_id: str
    version: int
    keyword_ids: tuple[str, ...]
    cluster_ids: tuple[str, ...]
    language: str
    country: str
    search_engine: str
    source_period: str
    generated_at: str = field(default_factory=_utc)
    parent_version: int | None = None


@dataclass
class SEOContentBrief:
    brief_id: str
    tenant_id: str
    site_id: str
    target_page_id: str
    page_type: str
    primary_cluster_id: str
    primary_keyword: str
    supporting_keywords: tuple[str, ...]
    intent: str
    title_recommendation: str
    h1_recommendation: str
    meta_recommendation: str
    topics: tuple[str, ...]
    internal_link_suggestions: tuple[dict, ...]
    product_facts_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    constraints: tuple[str, ...]
    status: str = "DRAFT"
    created_at: str = field(default_factory=_utc)


@dataclass
class RankObservation:
    observation_id: str
    tenant_id: str
    site_id: str
    keyword: str
    page_url: str
    position: float | None
    search_engine: str
    country: str
    device: str
    observed_at: str
    provider: str
    status: str  # OBSERVED_RANK | NOT_AVAILABLE
    trust_level: str = TRUSTED_EXTERNAL


@dataclass
class SERPObservation:
    observation_id: str
    tenant_id: str
    query: str
    country: str
    language: str
    search_engine: str
    observed_at: str
    results: tuple[dict, ...]
    provider: str
    trust_level: str = TRUSTED_EXTERNAL


@dataclass
class CWVBudget:
    budget_id: str
    version: str
    measurement_type: str  # LAB | FIELD
    thresholds: dict  # metric -> limit
    effective_at: str = field(default_factory=_utc)


@dataclass
class InternalLinkRecommendation:
    recommendation_id: str
    tenant_id: str
    site_id: str
    source_url: str
    target_url: str
    suggested_anchor: str
    reason: str
    confidence: float
    status: str = "RECOMMENDATION_ONLY"


@dataclass
class StructuredDataFinding:
    finding_id: str
    url: str
    schema_type: str
    present: bool
    issues: tuple[str, ...]
    severity: str = SEVERITY_INFO


@dataclass
class SEOLearningSignal:
    signal_id: str
    tenant_id: str
    site_id: str
    what_changed: str
    metric_moved: str
    direction: str
    confidence: str
    limitations: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    applicable_scope: str
    created_at: str = field(default_factory=_utc)


@dataclass
class SEOChangeEvent:
    change_id: str
    tenant_id: str
    site_id: str
    page_id: str
    change_type: str
    before_ref: str
    after_ref: str
    approved_at: str
    applied_at: str
    source_tool: str
    idempotency_key: str
    experiment_ref: str = ""


@dataclass
class SEOActionPlan:
    plan_id: str
    tenant_id: str
    site_id: str
    version: int
    recommendations: tuple[dict, ...]
    status: str
    measurement_window_days: int
    created_at: str = field(default_factory=_utc)
