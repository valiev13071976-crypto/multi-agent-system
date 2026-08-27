"""Canonical immutable commerce contracts — no secrets, no card data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from commerce.errors import CardDataForbiddenError
from commerce.states import ORDER_NEW
from security.tenant import normalize_tenant_id

_CARD_KEYS = frozenset(
    {
        "pan",
        "card_number",
        "cvv",
        "cvc",
        "track1",
        "track2",
        "magnetic_track",
        "card_auth_data",
        "full_card",
    }
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    raw = dict(value or {})
    for key in list(raw):
        if str(key).lower() in _CARD_KEYS:
            raise CardDataForbiddenError("card_data_forbidden")
    return MappingProxyType(sanitize_metadata(raw))


@dataclass(frozen=True)
class CommerceOrderLine:
    product_ref: str
    quantity: float
    sku: str = ""
    ean: str = ""
    mpn: str = ""
    warehouse: str = ""
    lot_ref: str = ""
    serial_ref: str = ""
    marking_code_refs: tuple[str, ...] = ()
    unit_price: float | None = None
    tax_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "marking_code_refs", tuple(self.marking_code_refs))
        object.__setattr__(self, "tax_metadata", _meta(self.tax_metadata))
        if self.quantity <= 0:
            raise ValueError("line_quantity_invalid")


@dataclass(frozen=True)
class CommerceOrder:
    order_id: str
    tenant_id: str
    buyer_type: str  # B2C | B2B
    buyer_ref: str = ""
    purpose_declaration_ref: str = ""
    lines: tuple[CommerceOrderLine, ...] = ()
    totals: Mapping[str, object] = field(default_factory=dict)
    payment_state_ref: str = ""
    payment_status: str = "unconfirmed"  # reference only — no payments engine
    fulfillment_state: str = ORDER_NEW
    compliance_state: str = "none"
    external_order_refs: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc)
    updated_at: datetime = field(default_factory=_utc)
    rule_version: str = ""
    scenario: str = ""

    def __post_init__(self):
        if self.buyer_type not in {"B2C", "B2B"}:
            raise ValueError("buyer_type_invalid")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "totals", _meta(self.totals))
        object.__setattr__(self, "external_order_refs", MappingProxyType(dict(self.external_order_refs or {})))
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class InventoryPosition:
    product_ref: str
    warehouse: str
    available: float
    reserved: float = 0.0
    in_transit: float = 0.0
    expected: float = 0.0
    blocked: float = 0.0
    lot: str = ""
    serial: str = ""
    marking: str = ""
    cost_ref: str = ""
    fetched_at: datetime | None = None
    external_updated_at: datetime | None = None
    stale_after_seconds: float = 120.0
    source: str = "inventory"
    external_id: str = ""

    def is_stale(self, *, now: datetime | None = None) -> bool:
        if self.fetched_at is None:
            return True
        stamp = now or _utc()
        return (stamp - self.fetched_at).total_seconds() > float(self.stale_after_seconds)


@dataclass(frozen=True)
class Shipment:
    shipment_id: str
    order_id: str
    tenant_id: str
    warehouse: str
    lines: tuple[CommerceOrderLine, ...] = ()
    marking_refs: tuple[str, ...] = ()
    external_docs: Mapping[str, str] = field(default_factory=dict)
    status: str = "pending"
    created_at: datetime = field(default_factory=_utc)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "marking_refs", tuple(self.marking_refs))
        object.__setattr__(self, "external_docs", MappingProxyType(dict(self.external_docs or {})))


@dataclass(frozen=True)
class ComplianceDecision:
    scenario: str
    rule_version: str
    evidence: Mapping[str, object] = field(default_factory=dict)
    required_actions: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    requires_hitl: bool = False
    status: str = "evaluated"
    jurisdiction: str = "RU"

    def __post_init__(self):
        object.__setattr__(self, "evidence", _meta(self.evidence))
        object.__setattr__(self, "required_actions", tuple(self.required_actions))
        object.__setattr__(self, "forbidden_actions", tuple(self.forbidden_actions))
        object.__setattr__(self, "required_capabilities", tuple(self.required_capabilities))


@dataclass(frozen=True)
class CommerceOperationResult:
    operation_id: str
    workflow_id: str
    status: str
    external_refs: Mapping[str, str] = field(default_factory=dict)
    before_state: Mapping[str, object] = field(default_factory=dict)
    after_state: Mapping[str, object] = field(default_factory=dict)
    error: str = ""
    reconciliation_state: str = "pending"
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "external_refs", MappingProxyType(dict(self.external_refs or {})))
        object.__setattr__(self, "before_state", _meta(self.before_state))
        object.__setattr__(self, "after_state", _meta(self.after_state))
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class BuyerPurposeDeclaration:
    declaration_id: str
    tenant_id: str
    buyer_inn: str
    buyer_name: str
    order_id: str
    declaration_version: str
    exact_text: str
    selected_option: int  # 1 own-use, 2 resale
    representative: str = ""
    timestamp: datetime = field(default_factory=_utc)
    session_ref: str = ""
    source_ip: str = ""
    source_channel: str = ""
    linked_docs: tuple[str, ...] = ()
    workflow_status: str = "active"

    def __post_init__(self):
        if self.selected_option not in {1, 2}:
            raise ValueError("declaration_option_invalid")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "linked_docs", tuple(self.linked_docs))


DECLARATION_OWN_USE_V1 = (
    "Товар приобретается для собственных нужд и не предназначен для дальнейшей реализации"
)
DECLARATION_RESALE_V1 = "Товар приобретается для дальнейшей реализации"
DECLARATION_TEXTS = {
    1: DECLARATION_OWN_USE_V1,
    2: DECLARATION_RESALE_V1,
}


@dataclass(frozen=True)
class SupplierRecord:
    supplier_id: str
    tenant_id: str
    name: str = ""
    identifiers: Mapping[str, str] = field(default_factory=dict)
    currency: str = "RUB"
    moq: float = 0.0
    lead_time_days: float = 0.0
    payment_terms: str = ""
    reliability_score: float = 0.0
    error_rate: float = 0.0
    return_rate: float = 0.0
    price_history_refs: tuple[str, ...] = ()
    delivery_history_refs: tuple[str, ...] = ()
    active: bool = True
    contract_refs: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "identifiers", MappingProxyType(dict(self.identifiers or {})))
        object.__setattr__(self, "price_history_refs", tuple(self.price_history_refs))
        object.__setattr__(self, "delivery_history_refs", tuple(self.delivery_history_refs))
        object.__setattr__(self, "contract_refs", tuple(self.contract_refs))


@dataclass(frozen=True)
class ExternalConfirmation:
    """Proof that external Source of Truth confirmed an operation."""

    system: str
    external_id: str
    status: str
    timestamp: datetime
    version: str = ""
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "provenance", _meta(self.provenance))
        if not self.system or not self.external_id:
            raise ValueError("external_confirmation_incomplete")
