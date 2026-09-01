"""Canonical Marketplace Platform contracts (one shared core)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from security.tenant import require_tenant_id

PLATFORM_SCHEMA_VERSION = "1.0.0"
MIN_PRICE_POLICY_VERSION = "1.0.0"
EXPORT_PROFILE_VERSION = "1.0.0"

PROVIDER_WILDBERRIES = "WILDBERRIES"
PROVIDER_OZON = "OZON"
PROVIDER_YANDEX_MARKET = "YANDEX_MARKET"

CAP_CATALOG_READ = "CATALOG_READ"
CAP_CARD_CREATE = "CARD_CREATE"
CAP_CARD_UPDATE = "CARD_UPDATE"
CAP_CARD_ARCHIVE = "CARD_ARCHIVE"
CAP_PRICE_READ = "PRICE_READ"
CAP_PRICE_WRITE = "PRICE_WRITE"
CAP_STOCK_READ = "STOCK_READ"
CAP_STOCK_WRITE = "STOCK_WRITE"
CAP_PROMOTION_READ = "PROMOTION_READ"
CAP_PROMOTION_WRITE = "PROMOTION_WRITE"
CAP_MIN_PRICE_WRITE = "MIN_PRICE_WRITE"
CAP_ORDER_READ = "ORDER_READ"
CAP_ORDER_STATUS_WRITE = "ORDER_STATUS_WRITE"
CAP_REVIEW_READ = "REVIEW_READ"
CAP_REVIEW_REPLY = "REVIEW_REPLY"
CAP_ANALYTICS_READ = "ANALYTICS_READ"
CAP_COMMISSION_READ = "COMMISSION_READ"
CAP_COMPETITOR_READ = "COMPETITOR_READ"

MODE_MONITOR_ONLY = "MONITOR_ONLY"
MODE_RECOMMEND = "RECOMMEND"
MODE_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
MODE_AUTO_CORRECT = "AUTO_CORRECT"

PROFIT_PROFITABLE = "PROFITABLE"
PROFIT_BELOW_TARGET = "BELOW_TARGET"
PROFIT_LOSS = "LOSS"
PROFIT_UNKNOWN = "UNKNOWN"

PROMO_SELLER = "SELLER_CONTROLLED"
PROMO_PLATFORM = "PLATFORM_CONTROLLED"
PROMO_MIXED = "MIXED"
PROMO_UNKNOWN = "UNKNOWN"

PROMO_SAFE = "SAFE"
PROMO_WARNING = "WARNING"
PROMO_LOSS = "LOSS"
PROMO_RISK_UNKNOWN = "UNKNOWN"

STOCK_MATCHED = "MATCHED"
STOCK_DRIFT = "DRIFT"
STOCK_STALE = "STALE"
STOCK_UNKNOWN = "UNKNOWN"
STOCK_CONFLICT = "CONFLICT"

LISTING_NOT_SELECTED = "NOT_SELECTED"
LISTING_SELECTED = "SELECTED"
LISTING_READY = "READY"
LISTING_PUBLISHED = "PUBLISHED"
LISTING_PAUSED = "PAUSED"
LISTING_BLOCKED = "BLOCKED"
LISTING_ARCHIVED = "ARCHIVED"

MAP_MATCHED = "MATCHED"
MAP_CANDIDATE = "CANDIDATE"
MAP_UNMAPPED = "UNMAPPED"
MAP_CONFLICT = "CONFLICT"

COMP_MATCHED = "MATCHED"
COMP_CANDIDATE = "CANDIDATE"
COMP_AMBIGUOUS = "AMBIGUOUS"
COMP_REJECTED = "REJECTED"

ALERT_OPEN = "OPEN"
ALERT_ACK = "ACKNOWLEDGED"
ALERT_AUTO = "AUTO_CORRECTED"
ALERT_RESOLVED = "OPERATOR_RESOLVED"
ALERT_STALE = "STALE"
ALERT_CLOSED = "CLOSED"


@dataclass(frozen=True)
class MoneyAmount:
    amount: Decimal
    currency: str = "RUB"

    def __post_init__(self):
        object.__setattr__(self, "amount", Decimal(str(self.amount)))


@dataclass(frozen=True)
class MarketplaceProject:
    project_id: str
    tenant_id: str
    owner_id: str = ""
    commerce_project_ref: str = ""
    default_currency: str = "RUB"
    pricing_policy_ref: str = ""
    version: str = PLATFORM_SCHEMA_VERSION
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MarketplaceAccount:
    account_id: str
    tenant_id: str
    provider: str
    credential_ref: str
    external_shop_ref: str = ""
    status: str = "ACTIVE"
    capabilities: tuple[str, ...] = ()
    profile_version: str = "1.0.0"
    live: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True)
class MarketplaceSelection:
    selection_id: str
    tenant_id: str
    product_ids: tuple[str, ...] = ()
    sku_ids: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    filters: tuple[tuple[str, str], ...] = ()
    allow_all_catalog: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "product_ids", tuple(self.product_ids))
        object.__setattr__(self, "sku_ids", tuple(self.sku_ids))
        object.__setattr__(self, "category_ids", tuple(self.category_ids))
        object.__setattr__(self, "brands", tuple(self.brands))
        object.__setattr__(self, "filters", tuple(self.filters))


@dataclass(frozen=True)
class MarketplaceListing:
    listing_id: str
    tenant_id: str
    provider: str
    account_id: str
    product_id: str
    sku_id: str
    external_listing_id: str = ""
    external_category: str = ""
    status: str = LISTING_SELECTED
    content_refs: tuple[str, ...] = ()
    media_refs: tuple[str, ...] = ()
    version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "content_refs", tuple(self.content_refs))
        object.__setattr__(self, "media_refs", tuple(self.media_refs))


@dataclass(frozen=True)
class CategoryMappingResult:
    status: str
    canonical_category_id: str
    marketplace_category_id: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttributeMappingResult:
    attribute_id: str
    provider_key: str
    status: str
    value: str = ""
    required: bool = False


@dataclass(frozen=True)
class MarketplacePublicationPlan:
    plan_id: str
    tenant_id: str
    provider: str
    account_id: str
    dry_run: bool
    selected: tuple[str, ...]
    creates: tuple[dict, ...]
    updates: tuple[dict, ...]
    skips: tuple[dict, ...]
    conflicts: tuple[dict, ...]
    invalid: tuple[dict, ...]
    estimated_api_calls: int
    approval_required: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class MarketplaceMinPricePolicy:
    policy_id: str
    version: str = MIN_PRICE_POLICY_VERSION
    required_margin_pct: Decimal = Decimal("10")
    include_commission: bool = True
    include_logistics: bool = True
    include_acquiring: bool = True
    currency: str = "RUB"

    def __post_init__(self):
        object.__setattr__(self, "required_margin_pct", Decimal(str(self.required_margin_pct)))


@dataclass(frozen=True)
class MarketplaceCommissionObservation:
    observation_id: str
    provider: str
    category: str
    rate: Decimal
    fixed_fee: Decimal = Decimal("0")
    fulfillment_scheme: str = "marketplace"
    source: str = "fixture"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: str = "HIGH"
    effective_from: datetime | None = None

    def __post_init__(self):
        object.__setattr__(self, "rate", Decimal(str(self.rate)))
        object.__setattr__(self, "fixed_fee", Decimal(str(self.fixed_fee)))


@dataclass(frozen=True)
class MarketplaceProfitabilityResult:
    sku_id: str
    provider: str
    selling_price: MoneyAmount
    estimated_proceeds: MoneyAmount
    known_costs: MoneyAmount
    unknown_costs: tuple[str, ...]
    contribution: MoneyAmount
    margin_pct: Decimal | None
    minimum_allowed: MoneyAmount | None
    status: str
    evidence: tuple[str, ...]
    policy_version: str

    def __post_init__(self):
        object.__setattr__(self, "unknown_costs", tuple(self.unknown_costs))
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class MarketplacePromotionObservation:
    promotion_id: str
    provider: str
    sku_id: str
    ownership: str
    displayed_price: MoneyAmount
    seller_price: MoneyAmount | None
    platform_discount: MoneyAmount | None
    seller_discount: MoneyAmount | None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketplaceOperatorAlert:
    alert_id: str
    tenant_id: str
    provider: str
    account_id: str
    severity: str
    alert_type: str
    sku_id: str
    summary: str
    evidence: tuple[str, ...]
    financial_impact: str = ""
    recommended_action: str = ""
    auto_correction_available: bool = False
    status: str = ALERT_OPEN
    issue_key: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class MarketplaceReview:
    review_id: str
    tenant_id: str
    provider: str
    account_id: str
    external_review_id: str
    sku_id: str
    rating: int
    text: str
    answer_status: str = "UNANSWERED"
    topics: tuple[str, ...] = ()
    sentiment: str = "NEUTRAL"

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "topics", tuple(self.topics))


@dataclass(frozen=True)
class CompetitorPriceObservation:
    observation_id: str
    provider: str
    sku_id: str
    competitor_price: MoneyAmount
    match_status: str
    confidence: Decimal
    source: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seller: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketplaceChannelPrice:
    sku_id: str
    provider: str
    account_id: str
    base_canonical: MoneyAmount
    channel_price: MoneyAmount
    observed_price: MoneyAmount | None = None
    seller_discount: MoneyAmount | None = None
    platform_discount: MoneyAmount | None = None
    minimum_allowed: MoneyAmount | None = None
    profitability_status: str = PROFIT_UNKNOWN
    observed_at: datetime | None = None


@dataclass(frozen=True)
class MarketplaceJob:
    job_id: str
    tenant_id: str
    job_type: str
    status: str
    provider: str = ""
    checkpoint: int = 0
    processed: int = 0
    failed: int = 0
    cancelled: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class ProviderMediaProfile:
    provider: str
    max_images: int
    min_width: int
    min_height: int
    aspect_ratio: str
    version: str = "1.0.0"
