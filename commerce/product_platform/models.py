"""Block 11 canonical product/e-commerce domain contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import require_tenant_id

TRUST_TRUSTED = "TRUSTED_SOURCE"
TRUST_NORMALIZED = "NORMALIZED"
TRUST_INFERRED = "INFERRED"
TRUST_GENERATED = "GENERATED"
TRUST_CONFLICTING = "CONFLICTING"
TRUST_MISSING = "MISSING"

MATCH_MATCHED = "MATCHED"
MATCH_NEW = "NEW"
MATCH_AMBIGUOUS = "AMBIGUOUS"
MATCH_CONFLICT = "CONFLICT"
MATCH_INVALID = "INVALID"
MATCH_UNCHANGED = "UNCHANGED"

READINESS_READY = "READY"
READINESS_NOT_READY = "NOT_READY"
READINESS_NEEDS_REVIEW = "NEEDS_REVIEW"
READINESS_BLOCKED = "BLOCKED"

PRICE_ALLOW = "ALLOW"
PRICE_DENY = "DENY"
PRICE_REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
PRICE_NO_CHANGE = "NO_CHANGE"
PRICE_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

AVAIL_IN_STOCK = "IN_STOCK"
AVAIL_LOW_STOCK = "LOW_STOCK"
AVAIL_OUT_OF_STOCK = "OUT_OF_STOCK"
AVAIL_UNKNOWN = "UNKNOWN"
AVAIL_STALE = "STALE"

ORDER_NEW = "NEW"
ORDER_CONFIRMED = "CONFIRMED"
ORDER_PROCESSING = "PROCESSING"
ORDER_FULFILLED = "FULFILLED"
ORDER_CANCELLED = "CANCELLED"
ORDER_RETURNED = "RETURNED"
ORDER_FAILED = "FAILED"

ORDER_TRANSITIONS = {
    ORDER_NEW: {ORDER_CONFIRMED, ORDER_CANCELLED, ORDER_FAILED},
    ORDER_CONFIRMED: {ORDER_PROCESSING, ORDER_CANCELLED},
    ORDER_PROCESSING: {ORDER_FULFILLED, ORDER_CANCELLED, ORDER_FAILED},
    ORDER_FULFILLED: set(),
    ORDER_CANCELLED: set(),
    ORDER_FAILED: set(),
}

CATALOG_PROFILE_VERSION = "1.0.0"
IMPORT_PROFILE_VERSION = "1.0.0"
PRICE_POLICY_VERSION = "1.0.0"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def money(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class MoneyAmount:
    amount: Decimal
    currency: str

    def __post_init__(self):
        object.__setattr__(self, "amount", money(self.amount))
        if not self.currency:
            raise ValueError("currency_required")


@dataclass(frozen=True)
class ProductIdentifier:
    identifier_type: str
    value: str
    trust: str = TRUST_TRUSTED


@dataclass(frozen=True)
class ProductVariant:
    variant_id: str
    sku: str
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes or {})))


@dataclass(frozen=True)
class ProductVersion:
    product_id: str
    version_id: str
    tenant_id: str
    title: str
    brand: str = ""
    description: str = ""
    sku: str = ""
    status: str = "active"
    parent_version_id: str | None = None
    field_trust: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)
    variants: tuple[ProductVariant, ...] = ()
    category_id: str = ""
    publication_readiness: str = READINESS_NOT_READY
    created_at: datetime = field(default_factory=_utc)
    profile_version: str = "1.0.0"

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "field_trust", _meta(self.field_trust))
        object.__setattr__(self, "attributes", _meta(self.attributes))
        object.__setattr__(self, "variants", tuple(self.variants))


@dataclass(frozen=True)
class CatalogQualityIssue:
    code: str
    message: str
    product_id: str = ""
    severity: str = "warning"


@dataclass(frozen=True)
class CatalogQualityReport:
    report_id: str
    tenant_id: str
    profile_version: str
    issues: tuple[CatalogQualityIssue, ...]
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "counts", _meta(self.counts))


@dataclass(frozen=True)
class ProductImportResult:
    import_id: str
    tenant_id: str
    dry_run: bool
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: int = 0
    invalid: int = 0
    ambiguous: int = 0
    details: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class PriceObservation:
    observation_id: str
    tenant_id: str
    product_id: str
    variant_id: str
    source: str
    price: MoneyAmount
    observed_at: datetime
    freshness_status: str = "CURRENT"
    content_hash: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class PricePolicy:
    policy_id: str
    tenant_id: str
    version: str
    currency: str
    minimum_price: Decimal
    maximum_price: Decimal
    minimum_margin_pct: Decimal
    max_change_pct: Decimal
    max_change_abs: Decimal
    freshness_max_age_sec: int = 86400
    auto_apply_max_change_pct: Decimal = Decimal("5")
    rounding_scale: int = 2

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "minimum_price", money(self.minimum_price))
        object.__setattr__(self, "maximum_price", money(self.maximum_price))
        object.__setattr__(self, "minimum_margin_pct", money(self.minimum_margin_pct))
        object.__setattr__(self, "max_change_pct", money(self.max_change_pct))
        object.__setattr__(self, "max_change_abs", money(self.max_change_abs))
        object.__setattr__(self, "auto_apply_max_change_pct", money(self.auto_apply_max_change_pct))


@dataclass(frozen=True)
class PriceDecision:
    decision_id: str
    tenant_id: str
    product_id: str
    policy_version: str
    current_price: MoneyAmount
    proposed_price: MoneyAmount
    trusted_cost: MoneyAmount | None
    outcome: str
    reasons: tuple[str, ...] = ()
    price_version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True)
class PriceChangeReceipt:
    receipt_id: str
    tenant_id: str
    decision_id: str
    previous_price: MoneyAmount
    applied_price: MoneyAmount
    external_ref: str = ""
    status: str = "applied"

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class InventoryPositionRecord:
    tenant_id: str
    product_id: str
    location_id: str
    on_hand: Decimal
    reserved: Decimal
    incoming: Decimal
    source: str
    version: int
    observed_at: datetime

    @property
    def available(self) -> Decimal:
        return self.on_hand - self.reserved

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "on_hand", money(self.on_hand))
        object.__setattr__(self, "reserved", money(self.reserved))
        object.__setattr__(self, "incoming", money(self.incoming))


@dataclass(frozen=True)
class PlatformOrderItem:
    line_id: str
    product_id: str
    variant_id: str
    sku: str
    quantity: Decimal
    unit_price: MoneyAmount
    line_total: MoneyAmount

    def __post_init__(self):
        object.__setattr__(self, "quantity", money(self.quantity))


@dataclass(frozen=True)
class PlatformOrder:
    order_id: str
    tenant_id: str
    external_ref: str
    source: str
    currency: str
    status: str
    items: tuple[PlatformOrderItem, ...]
    order_total: MoneyAmount
    version: int = 1
    created_at: datetime = field(default_factory=_utc)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class OrderEvent:
    event_id: str
    tenant_id: str
    order_id: str
    prior_status: str
    new_status: str
    source: str
    external_event_id: str = ""
    created_at: datetime = field(default_factory=_utc)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class ExternalProductBinding:
    binding_id: str
    tenant_id: str
    product_id: str
    version_id: str
    system: str
    external_product_id: str
    external_version: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class CmsOperationResult:
    operation_id: str
    tenant_id: str
    operation: str
    status: str
    external_id: str = ""
    verified: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "verified", _meta(self.verified))


@dataclass(frozen=True)
class CommerceJob:
    job_id: str
    tenant_id: str
    operation: str
    status: str
    checkpoint: int = 0
    total: int = 0

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


def observation_hash(*, product_id: str, source: str, price: Decimal, currency: str, observed_at: datetime) -> str:
    raw = f"{product_id}|{source}|{price}|{currency}|{observed_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()
