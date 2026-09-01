"""Business / Digital Assistant canonical contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from security.tenant import require_tenant_id

PLATFORM_SCHEMA_VERSION = "1.0.0"
MAX_PLAN_STEPS = 32
BATCH_ROW_THRESHOLD = 100

# Intents
INTENT_ANALYZE = "ANALYZE"
INTENT_COMPARE = "COMPARE"
INTENT_RESEARCH = "RESEARCH"
INTENT_PREPARE = "PREPARE"
INTENT_GENERATE = "GENERATE"
INTENT_RECONCILE = "RECONCILE"
INTENT_OPTIMIZE = "OPTIMIZE"
INTENT_MONITOR = "MONITOR_REQUEST"
INTENT_IMPORT = "IMPORT"
INTENT_EXPORT = "EXPORT"
INTENT_PUBLISH = "PUBLISH"
INTENT_UPDATE = "UPDATE"
INTENT_COMMUNICATE = "COMMUNICATE"
INTENT_REPORT = "REPORT"
INTENT_MULTI_STEP = "MULTI_STEP_BUSINESS_TASK"

# Step classes
STEP_READ = "READ"
STEP_ANALYZE = "ANALYZE"
STEP_GENERATE = "GENERATE"
STEP_PREPARE_WRITE = "PREPARE_WRITE"
STEP_WRITE = "WRITE"
STEP_VERIFY = "VERIFY"

# Statuses
STATUS_PLANNING = "PLANNING"
STATUS_READY = "READY"
STATUS_RUNNING = "RUNNING"
STATUS_WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
STATUS_WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
STATUS_BATCH_RUNNING = "BATCH_RUNNING"
STATUS_VERIFYING = "VERIFYING"
STATUS_COMPLETED = "COMPLETED"
STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_BLOCKED = "BLOCKED"
STATUS_PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"

# Decision kinds
KIND_FACT = "FACT"
KIND_CALCULATION = "CALCULATION"
KIND_FINDING = "FINDING"
KIND_RECOMMENDATION = "RECOMMENDATION"
KIND_PROPOSED_ACTION = "PROPOSED_ACTION"
KIND_APPROVED_ACTION = "APPROVED_ACTION"
KIND_EXECUTED_ACTION = "EXECUTED_ACTION"

# Recipes
RECIPE_SUPPLIER_PRICE = "SUPPLIER_PRICE_ANALYSIS"
RECIPE_PRODUCT_LAUNCH = "PRODUCT_LAUNCH_PREPARATION"
RECIPE_MARKETPLACE_PROFIT = "MARKETPLACE_PROFITABILITY_REVIEW"
RECIPE_SEO_REVIEW = "SEO_SITE_REVIEW"
RECIPE_DOCUMENT_COMPARE = "DOCUMENT_COMPARISON"
RECIPE_COMMUNICATION = "CUSTOMER_FOLLOWUP_PREPARATION"
RECIPE_DAILY_REPORT = "BUSINESS_DAILY_REPORT"
RECIPE_GENERIC = "GENERIC_MULTI_STEP"
RECIPE_ONEC_PRICE = "ONEC_PRICE_UPDATE"
RECIPE_WB_PRICE = "WILDBERRIES_PRICE_UPDATE"
RECIPE_OZON_PRICE = "OZON_PRICE_UPDATE"
RECIPE_YANDEX_PRICE = "YANDEX_MARKET_PRICE_UPDATE"

# Canonical capability ids (Tool Platform names where known)
CAP_DATA_INGEST = "data.ingest"
CAP_DATA_NORMALIZE = "data.normalize"
CAP_DATA_COMPARE = "data.compare"
CAP_DATA_MATCH = "data.match"
CAP_DOC_COMPARE = "document.compare"
CAP_DOC_EXTRACT = "document.extract"
CAP_CONTENT = "content.research"
CAP_MEDIA = "product_media"
CAP_SEO = "seo"
CAP_COMMERCE = "commerce.product"
CAP_MARKETPLACE = "marketplace.product"
CAP_CMS_BITRIX = "cms.bitrix"
CAP_ERP_1C = "erp.1c"
CAP_EMAIL = "email"
CAP_CRM = "crm"
CAP_CALENDAR = "calendar"
CAP_ACQUISITION = "acquisition"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BusinessConstraint:
    brands: tuple[str, ...] = ()
    sku_ids: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    suppliers: tuple[str, ...] = ()
    marketplaces: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    margin_min_pct: Decimal | None = None
    top_n: int | None = None
    read_only: bool = False
    show_before_publication: bool = False
    currency: str = "RUB"
    unknown: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "brands", tuple(self.brands))
        object.__setattr__(self, "sku_ids", tuple(self.sku_ids))
        object.__setattr__(self, "categories", tuple(self.categories))
        object.__setattr__(self, "suppliers", tuple(self.suppliers))
        object.__setattr__(self, "marketplaces", tuple(self.marketplaces))
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "unknown", tuple(self.unknown))
        if self.margin_min_pct is not None:
            object.__setattr__(self, "margin_min_pct", Decimal(str(self.margin_min_pct)))


@dataclass(frozen=True)
class BusinessRequest:
    request_id: str
    tenant_id: str
    user_id: str
    text: str
    intent: str
    objective: str
    constraints: BusinessConstraint
    artifact_refs: tuple[str, ...] = ()
    budget_limit: Decimal | None = None
    deadline_at: datetime | None = None
    correlation_id: str = ""
    schema_version: str = PLATFORM_SCHEMA_VERSION
    created_at: datetime = field(default_factory=_now)
    read_only: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        if self.budget_limit is not None:
            object.__setattr__(self, "budget_limit", Decimal(str(self.budget_limit)))


@dataclass(frozen=True)
class BusinessPlanStep:
    step_id: str
    name: str
    capability: str
    step_class: str
    depends_on: tuple[str, ...] = ()
    risk_level: str = "LOW"
    requires_approval: bool = False
    workload: str = "interactive"  # interactive | batch
    expected_artifacts: tuple[str, ...] = ()
    recipe: str = ""
    args: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "expected_artifacts", tuple(self.expected_artifacts))
        object.__setattr__(self, "args", tuple(self.args))


@dataclass(frozen=True)
class BusinessPlan:
    plan_id: str
    tenant_id: str
    request_id: str
    version: int
    recipe: str
    steps: tuple[BusinessPlanStep, ...]
    fingerprint: str
    read_only: bool = False
    approval_boundaries: tuple[str, ...] = ()
    estimated_cost: Decimal = Decimal("0")
    schema_version: str = PLATFORM_SCHEMA_VERSION
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "approval_boundaries", tuple(self.approval_boundaries))
        object.__setattr__(self, "estimated_cost", Decimal(str(self.estimated_cost)))


@dataclass(frozen=True)
class BusinessFinding:
    finding_id: str
    kind: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    confidence: Decimal = Decimal("1.0")
    sku_id: str = ""
    numeric_value: str = ""

    def __post_init__(self):
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "confidence", Decimal(str(self.confidence)))


@dataclass(frozen=True)
class BusinessPreview:
    preview_id: str
    tenant_id: str
    execution_id: str
    plan_fingerprint: str
    artifact_checksum: str
    changes: tuple[dict, ...]
    warnings: tuple[str, ...]
    external_writes: tuple[dict, ...]
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "external_writes", tuple(self.external_writes))


@dataclass(frozen=True)
class BusinessApprovalRequest:
    approval_id: str
    tenant_id: str
    execution_id: str
    plan_fingerprint: str
    preview_id: str
    actor_id: str
    step_ids: tuple[str, ...]
    status: str = "PENDING"
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "step_ids", tuple(self.step_ids))


@dataclass
class BusinessExecutionStep:
    step_id: str
    status: str
    result: dict = field(default_factory=dict)
    error_code: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class BusinessExecution:
    execution_id: str
    tenant_id: str
    request_id: str
    plan_id: str
    plan_fingerprint: str
    status: str
    steps: dict[str, BusinessExecutionStep]
    findings: list[BusinessFinding] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    preview: BusinessPreview | None = None
    approval: BusinessApprovalRequest | None = None
    cost: Decimal = Decimal("0")
    correlation_id: str = ""
    workflow_id: str = ""
    checkpoint: int = 0
    cancelled: bool = False
    summary: str = ""
    mode: str = "FIXTURE"
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self):
        self.tenant_id = require_tenant_id(self.tenant_id)


def plan_fingerprint(plan_payload: dict[str, Any]) -> str:
    raw = json.dumps(plan_payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def artifact_checksum(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()
