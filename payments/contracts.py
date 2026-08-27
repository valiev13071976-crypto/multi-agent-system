"""Canonical immutable payment contracts — no card data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from payments.errors import CardDataForbiddenError
from payments.states import PAY_CREATED, REF_REQUESTED
from security.tenant import normalize_tenant_id

SCHEMA_VERSION = "1.0.0"

_CARD_FORBIDDEN = frozenset(
    {
        "pan",
        "card_number",
        "cardnumber",
        "cvv",
        "cvc",
        "cvv2",
        "pin",
        "magnetic_stripe",
        "track_data",
        "track1",
        "track2",
        "card_auth_data",
        "full_card_token",
        "raw_card_token",
        "bank_password",
        "acquiring_password",
        "acquiring_secret",
        "merchant_secret",
    }
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _meta(data: Mapping[str, object] | None) -> MappingProxyType:
    return MappingProxyType(dict(sanitize_metadata(dict(data or {}))))


def assert_no_card_data(payload: Mapping[str, object] | None) -> None:
    if not payload:
        return
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, Mapping):
            for k, v in cur.items():
                key = str(k).strip().lower().replace("-", "_").replace(" ", "_")
                if key in _CARD_FORBIDDEN:
                    raise CardDataForbiddenError("card_data_forbidden")
                if isinstance(v, (Mapping, list, tuple)):
                    stack.append(v)
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)


@dataclass(frozen=True)
class PaymentRecord:
    payment_id: str
    tenant_id: str
    provider: str
    amount: float
    currency: str
    status: str = PAY_CREATED
    payment_method_type: str = ""
    external_transaction_id: str = ""
    order_refs: tuple[str, ...] = ()
    invoice_refs: tuple[str, ...] = ()
    payer_ref: str = ""
    payer_inn: str = ""
    payer_name: str = ""
    authorized_amount: float = 0.0
    captured_amount: float = 0.0
    refunded_amount: float = 0.0
    occurred_at: datetime | None = None
    received_at: datetime = field(default_factory=_utc)
    source: str = "gateway"
    provenance: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self):
        assert_no_card_data(self.metadata)
        assert_no_card_data(self.provenance)
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "order_refs", tuple(self.order_refs))
        object.__setattr__(self, "invoice_refs", tuple(self.invoice_refs))
        object.__setattr__(self, "currency", str(self.currency or "RUB").upper())
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class PaymentAllocation:
    allocation_id: str
    payment_id: str
    tenant_id: str
    allocated_amount: float
    currency: str
    order_id: str = ""
    invoice_id: str = ""
    allocation_method: str = "manual"
    evidence: Mapping[str, object] = field(default_factory=dict)
    confidence: float = 0.0
    status: str = "CONFIRMED"
    created_at: datetime = field(default_factory=_utc)
    superseded_by: str = ""

    def __post_init__(self):
        assert_no_card_data(self.evidence)
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "currency", str(self.currency or "RUB").upper())
        object.__setattr__(self, "evidence", _meta(self.evidence))


@dataclass(frozen=True)
class BankTransaction:
    transaction_id: str
    tenant_id: str
    account_ref: str
    amount: float
    currency: str
    direction: str = "incoming"
    external_bank_id: str = ""
    booked_at: datetime | None = None
    value_date: datetime | None = None
    payer_ref: str = ""
    payee_ref: str = ""
    payer_inn: str = ""
    payer_name: str = ""
    purpose: str = ""
    document_ref: str = ""
    invoice_ref: str = ""
    order_ref: str = ""
    source_statement_ref: str = ""
    provenance: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        assert_no_card_data(self.metadata)
        assert_no_card_data(self.provenance)
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "currency", str(self.currency or "RUB").upper())
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class PaymentMatchResult:
    match_id: str
    tenant_id: str
    payment_id: str = ""
    bank_transaction_id: str = ""
    candidate_order_refs: tuple[str, ...] = ()
    candidate_invoice_refs: tuple[str, ...] = ()
    selected_order_id: str = ""
    selected_invoice_id: str = ""
    evidence: Mapping[str, object] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()
    confidence: float = 0.0
    review_required: bool = False
    status: str = "MATCHED"
    created_at: datetime = field(default_factory=_utc)

    def __post_init__(self):
        assert_no_card_data(self.evidence)
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "candidate_order_refs", tuple(self.candidate_order_refs))
        object.__setattr__(
            self, "candidate_invoice_refs", tuple(self.candidate_invoice_refs)
        )
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
        object.__setattr__(self, "evidence", _meta(self.evidence))


@dataclass(frozen=True)
class RefundRecord:
    refund_id: str
    payment_id: str
    tenant_id: str
    amount: float
    currency: str
    status: str = REF_REQUESTED
    order_id: str = ""
    reason: str = ""
    external_ref: str = ""
    prepared_by: str = ""
    approved_by: str = ""
    executed_at: datetime | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    idempotency_key: str = ""

    def __post_init__(self):
        assert_no_card_data(self.metadata)
        assert_no_card_data(self.provenance)
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "currency", str(self.currency or "RUB").upper())
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ReconciliationFinding:
    finding_id: str
    tenant_id: str
    finding_type: str
    severity: str
    status: str
    refs: Mapping[str, object] = field(default_factory=dict)
    expected: Mapping[str, object] = field(default_factory=dict)
    actual: Mapping[str, object] = field(default_factory=dict)
    evidence: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc)
    resolved_at: datetime | None = None
    workflow_ref: str = ""

    def __post_init__(self):
        assert_no_card_data(self.refs)
        assert_no_card_data(self.evidence)
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "refs", _meta(self.refs))
        object.__setattr__(self, "expected", _meta(self.expected))
        object.__setattr__(self, "actual", _meta(self.actual))
        object.__setattr__(self, "evidence", _meta(self.evidence))


@dataclass(frozen=True)
class FulfillmentUnlockResult:
    code: str
    tenant_id: str
    order_id: str
    required_amount: float
    allocated_amount: float
    remaining_amount: float
    currency: str
    payment_ids: tuple[str, ...] = ()
    review_required: bool = False
    evidence: Mapping[str, object] = field(default_factory=dict)
    policy_version: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "payment_ids", tuple(self.payment_ids))
        object.__setattr__(self, "evidence", _meta(self.evidence))


@dataclass(frozen=True)
class OrderPaymentTarget:
    """Safe view of an order/invoice for matching — not a commerce mutation."""

    order_id: str
    tenant_id: str
    amount: float
    currency: str
    invoice_number: str = ""
    buyer_inn: str = ""
    buyer_name: str = ""
    buyer_ref: str = ""
    payment_reference: str = ""
    fulfillment_state: str = ""
    shipment_started: bool = False
    marking_incomplete: bool = False
    fiscal_receipt_ref: str = ""
    fiscal_amount: float | None = None
    cancelled: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "currency", str(self.currency or "RUB").upper())
