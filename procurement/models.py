"""P16 Procurement MVP models — Decimal money, scoped entities, provenance."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from memory.models import MemoryScope, utc_now

PROCUREMENT_SCHEMA_VERSION = 1
PROCUREMENT_POLICY_VERSION = "1.0.0"
PROCUREMENT_SCORING_VERSION = "1.0.0"
PROCUREMENT_WORKFLOW_VERSION = "1.0.0"

STATUS_CREATED = "created"
STATUS_REQUIREMENTS_READY = "requirements_ready"
STATUS_RESEARCHING = "researching"
STATUS_OFFERS_READY = "offers_ready"
STATUS_EVALUATING = "evaluating"
STATUS_RECOMMENDATION_READY = "recommendation_ready"
STATUS_WAITING_APPROVAL = "waiting_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"

REQUEST_STATUSES = (
    STATUS_CREATED,
    STATUS_REQUIREMENTS_READY,
    STATUS_RESEARCHING,
    STATUS_OFFERS_READY,
    STATUS_EVALUATING,
    STATUS_RECOMMENDATION_READY,
    STATUS_WAITING_APPROVAL,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_NEEDS_CLARIFICATION,
)

SUPPLIER_CANDIDATE = "candidate"
SUPPLIER_KNOWN = "known"
SUPPLIER_PREFERRED = "preferred"
SUPPLIER_RESTRICTED = "restricted"
SUPPLIER_REJECTED = "rejected"
SUPPLIER_INACTIVE = "inactive"
SUPPLIER_STATUSES = (
    SUPPLIER_CANDIDATE,
    SUPPLIER_KNOWN,
    SUPPLIER_PREFERRED,
    SUPPLIER_RESTRICTED,
    SUPPLIER_REJECTED,
    SUPPLIER_INACTIVE,
)

OFFER_DISCOVERED = "discovered"
OFFER_NORMALIZED = "normalized"
OFFER_VALIDATED = "validated"
OFFER_EXPIRED = "expired"
OFFER_REJECTED = "rejected"
OFFER_SELECTED = "selected"
OFFER_STATUSES = (
    OFFER_DISCOVERED,
    OFFER_NORMALIZED,
    OFFER_VALIDATED,
    OFFER_EXPIRED,
    OFFER_REJECTED,
    OFFER_SELECTED,
)

TRUST_KNOWN = "known_internal"
TRUST_DOCUMENT = "document_sourced"
TRUST_EXTERNAL = "read_only_external"
TRUST_UNVERIFIED = "unverified_external"
SUPPLIER_TRUST_LEVELS = (TRUST_KNOWN, TRUST_DOCUMENT, TRUST_EXTERNAL, TRUST_UNVERIFIED)

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"
RISK_LEVELS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL)

ACTION_PREPARE_RFQ = "prepare_rfq"
ACTION_PREPARE_SUPPLIER_CONTACT = "prepare_supplier_contact"
ACTION_PREPARE_PURCHASE_REQUEST = "prepare_purchase_request"
ACTION_EXPORT_COMPARISON = "export_comparison"
ALLOWED_PROPOSED_ACTIONS = (
    ACTION_PREPARE_RFQ,
    ACTION_PREPARE_SUPPLIER_CONTACT,
    ACTION_PREPARE_PURCHASE_REQUEST,
    ACTION_EXPORT_COMPARISON,
)

FORBIDDEN_EXECUTION_ACTIONS = frozenset(
    {"place_order", "pay_supplier", "charge_card", "bank_transfer", "send_purchase_order"}
)

_WHITESPACE_RE = re.compile(r"\s+")


def content_hash_text(text: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", str(text or "").strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _ensure_utc(stamp: datetime | None) -> datetime | None:
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def parse_money_amount(raw) -> Decimal:
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, float):
        raise ValueError("float_money_forbidden")
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"invalid_money_amount:{raw!r}") from exc


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if isinstance(self.amount, float):
            raise ValueError("float_money_forbidden")
        object.__setattr__(self, "amount", parse_money_amount(self.amount))
        cur = str(self.currency or "").strip().upper()
        if not cur or len(cur) != 3:
            raise ValueError("currency_required")
        object.__setattr__(self, "currency", cur)

    def as_dict(self) -> dict:
        return {"amount": str(self.amount), "currency": self.currency}


@dataclass(frozen=True)
class OfferProvenance:
    source_id: str
    source_ref: str
    retrieved_at: datetime
    content_hash: str
    trust: str = TRUST_UNVERIFIED
    freshness: str = "unknown"
    document_id: str | None = None
    chunk_id: str | None = None

    def __post_init__(self):
        if not str(self.source_id or "").strip():
            raise ValueError("source_id_required")
        if not str(self.source_ref or "").strip():
            raise ValueError("source_ref_required")
        if not str(self.content_hash or "").strip():
            raise ValueError("content_hash_required")
        if self.trust not in SUPPLIER_TRUST_LEVELS:
            raise ValueError(f"invalid_trust:{self.trust}")
        object.__setattr__(self, "retrieved_at", _ensure_utc(self.retrieved_at) or utc_now())


@dataclass(frozen=True)
class ProcurementRequest:
    request_id: str
    scope: MemoryScope
    requested_by: str
    item_name: str
    quantity: Decimal | None
    unit: str | None
    specifications: Mapping[str, object] = field(default_factory=dict)
    description: str | None = None
    target_budget: Money | None = None
    currency: str | None = None
    required_by: datetime | None = None
    delivery_location: str | None = None
    preferred_suppliers: tuple[str, ...] = ()
    excluded_suppliers: tuple[str, ...] = ()
    constraints: Mapping[str, object] = field(default_factory=dict)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    status: str = STATUS_CREATED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    version: int = 1

    def __post_init__(self):
        if not str(self.request_id or "").strip():
            raise ValueError("request_id_required")
        if not str(self.item_name or "").strip():
            raise ValueError("item_name_required")
        if self.status not in REQUEST_STATUSES:
            raise ValueError(f"invalid_status:{self.status}")
        if self.quantity is not None:
            if isinstance(self.quantity, float):
                raise ValueError("float_money_forbidden")
            object.__setattr__(self, "quantity", parse_money_amount(self.quantity))
        if self.currency is not None:
            object.__setattr__(self, "currency", str(self.currency).strip().upper())
        object.__setattr__(self, "preferred_suppliers", tuple(self.preferred_suppliers or ()))
        object.__setattr__(self, "excluded_suppliers", tuple(self.excluded_suppliers or ()))
        object.__setattr__(self, "specifications", _meta(self.specifications))
        object.__setattr__(self, "constraints", _meta(self.constraints))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "updated_at", _ensure_utc(self.updated_at) or utc_now())
        object.__setattr__(self, "required_by", _ensure_utc(self.required_by))


@dataclass(frozen=True)
class ProcurementRequirement:
    category: str
    normalized_item: str
    quantity: Decimal | None
    unit: str | None
    mandatory_specs: Mapping[str, object] = field(default_factory=dict)
    preferred_specs: Mapping[str, object] = field(default_factory=dict)
    budget_constraint: Money | None = None
    currency: str | None = None
    delivery_deadline: datetime | None = None
    delivery_location: str | None = None
    supplier_constraints: Mapping[str, object] = field(default_factory=dict)
    compliance_constraints: Mapping[str, object] = field(default_factory=dict)
    notes: str | None = None
    incomplete: bool = False
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self):
        if self.quantity is not None and isinstance(self.quantity, float):
            raise ValueError("float_money_forbidden")
        if self.quantity is not None:
            object.__setattr__(self, "quantity", parse_money_amount(self.quantity))
        object.__setattr__(self, "mandatory_specs", _meta(self.mandatory_specs))
        object.__setattr__(self, "preferred_specs", _meta(self.preferred_specs))
        object.__setattr__(self, "supplier_constraints", _meta(self.supplier_constraints))
        object.__setattr__(self, "compliance_constraints", _meta(self.compliance_constraints))
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields or ()))
        object.__setattr__(self, "delivery_deadline", _ensure_utc(self.delivery_deadline))
        if self.currency is not None:
            object.__setattr__(self, "currency", str(self.currency).strip().upper())


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    scope: MemoryScope
    name: str
    source: str
    source_ref: str
    categories: tuple[str, ...] = ()
    trust_level: str = TRUST_UNVERIFIED
    status: str = SUPPLIER_CANDIDATE
    risk_flags: tuple[str, ...] = ()
    provenance: Mapping[str, object] = field(default_factory=dict)
    country: str | None = None
    website_ref: str | None = None
    contact_ref: str | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        if not str(self.supplier_id or "").strip():
            raise ValueError("supplier_id_required")
        if not str(self.name or "").strip():
            raise ValueError("name_required")
        if self.status not in SUPPLIER_STATUSES:
            raise ValueError(f"invalid_supplier_status:{self.status}")
        if self.trust_level not in SUPPLIER_TRUST_LEVELS:
            raise ValueError(f"invalid_trust:{self.trust_level}")
        object.__setattr__(self, "categories", tuple(self.categories or ()))
        object.__setattr__(self, "risk_flags", tuple(self.risk_flags or ()))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "updated_at", _ensure_utc(self.updated_at) or utc_now())


@dataclass(frozen=True)
class SupplierOffer:
    offer_id: str
    request_id: str
    supplier_id: str
    scope: MemoryScope
    source_type: str
    source_ref: str
    currency: str
    unit_price: Money | None
    quantity: Decimal | None
    provenance: OfferProvenance
    subtotal: Money | None = None
    shipping_cost: Money | None = None
    tax: Money | None = None
    total_cost: Money | None = None
    lead_time_days: int | None = None
    minimum_order_quantity: Decimal | None = None
    payment_terms: str | None = None
    delivery_terms: str | None = None
    valid_until: datetime | None = None
    availability: str | None = None
    warranty: str | None = None
    specifications: Mapping[str, object] = field(default_factory=dict)
    compliance: Mapping[str, object] = field(default_factory=dict)
    confidence: float | None = None
    status: str = OFFER_DISCOVERED
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        if self.status not in OFFER_STATUSES:
            raise ValueError(f"invalid_offer_status:{self.status}")
        cur = str(self.currency or "").strip().upper()
        if not cur:
            raise ValueError("currency_required")
        object.__setattr__(self, "currency", cur)
        if self.quantity is not None:
            if isinstance(self.quantity, float):
                raise ValueError("float_money_forbidden")
            object.__setattr__(self, "quantity", parse_money_amount(self.quantity))
        if self.minimum_order_quantity is not None:
            if isinstance(self.minimum_order_quantity, float):
                raise ValueError("float_money_forbidden")
            object.__setattr__(
                self, "minimum_order_quantity", parse_money_amount(self.minimum_order_quantity)
            )
        if self.confidence is not None:
            c = float(self.confidence)
            if c < 0.0 or c > 1.0:
                raise ValueError("invalid_confidence")
            object.__setattr__(self, "confidence", c)
        object.__setattr__(self, "specifications", _meta(self.specifications))
        object.__setattr__(self, "compliance", _meta(self.compliance))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "valid_until", _ensure_utc(self.valid_until))
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "updated_at", _ensure_utc(self.updated_at) or utc_now())


@dataclass(frozen=True)
class RiskFinding:
    category: str
    level: str
    code: str
    message: str
    offer_id: str | None = None
    supplier_id: str | None = None

    def __post_init__(self):
        if self.level not in RISK_LEVELS:
            raise ValueError(f"invalid_risk_level:{self.level}")


@dataclass(frozen=True)
class ComparisonRow:
    offer_id: str
    supplier_id: str
    score: Decimal
    rank: int
    currency: str
    total_or_unit: str | None
    mandatory_spec_failed: bool = False
    flags: tuple[str, ...] = ()
    breakdown: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.score, float):
            raise ValueError("float_money_forbidden")
        object.__setattr__(self, "score", parse_money_amount(self.score))
        object.__setattr__(self, "flags", tuple(self.flags or ()))
        object.__setattr__(self, "breakdown", _meta(self.breakdown))


@dataclass(frozen=True)
class ProcurementRecommendation:
    recommendation_id: str
    request_id: str
    scope: MemoryScope
    recommended_supplier_id: str | None
    recommended_offer_id: str | None
    alternatives: tuple[str, ...]
    reasoning_summary: str
    comparison: tuple[ComparisonRow, ...]
    risks: tuple[RiskFinding, ...]
    assumptions: tuple[str, ...]
    missing_information: tuple[str, ...]
    confidence: float
    citations: tuple[str, ...]
    requires_approval: bool
    status: str
    single_source_procurement: bool = False
    currency_conversion_required: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        object.__setattr__(self, "alternatives", tuple(self.alternatives or ()))
        object.__setattr__(self, "comparison", tuple(self.comparison or ()))
        object.__setattr__(self, "risks", tuple(self.risks or ()))
        object.__setattr__(self, "assumptions", tuple(self.assumptions or ()))
        object.__setattr__(self, "missing_information", tuple(self.missing_information or ()))
        object.__setattr__(self, "citations", tuple(self.citations or ()))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        c = float(self.confidence)
        if c < 0.0 or c > 1.0:
            raise ValueError("invalid_confidence")
        object.__setattr__(self, "confidence", c)


@dataclass(frozen=True)
class ProcurementProposedAction:
    action_id: str
    request_id: str
    action_type: str
    payload_safe: Mapping[str, object]
    requires_approval: bool = True
    status: str = "draft"
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        if self.action_type not in ALLOWED_PROPOSED_ACTIONS:
            raise ValueError(f"invalid_proposed_action:{self.action_type}")
        if self.action_type in FORBIDDEN_EXECUTION_ACTIONS:
            raise ValueError("forbidden_execution_action")
        object.__setattr__(self, "payload_safe", _meta(self.payload_safe))
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
