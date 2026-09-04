"""Canonical order envelope for Block 20. Reuses commerce status vocabulary; does not invent facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from commerce.states import ORDER_CANCELLED as COMMERCE_CANCELLED

SOURCE_SITE = "SITE"
SOURCE_WB = "WILDBERRIES"
SOURCE_OZON = "OZON"
SOURCE_YM = "YANDEX_MARKET"
SOURCE_CUSTOM = "CUSTOM"
SUPPORTED_SOURCES = frozenset({SOURCE_SITE, SOURCE_WB, SOURCE_OZON, SOURCE_YM, SOURCE_CUSTOM})

MAPPED = "MAPPED"
AMBIGUOUS = "AMBIGUOUS"
MISSING = "MISSING"

STATUS_RECEIVED = "RECEIVED"
STATUS_VALIDATED = "VALIDATED"
STATUS_REQUIRES_REVIEW = "REQUIRES_REVIEW"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_CANCELLED = "CANCELLED"
STATUS_FULFILLMENT_PENDING = "FULFILLMENT_PENDING"
STATUS_FULFILLED = "FULFILLED"
STATUS_FAILED = "FAILED"

INGEST_NEW = "NEW"
INGEST_DUPLICATE = "DUPLICATE"
INGEST_REPLAY = "REPLAY"
INGEST_UPDATED_VERSION = "UPDATED_VERSION"
INGEST_CONFLICT = "CONFLICT"

AGG_COMPLETE = "COMPLETE"
AGG_PARTIAL = "PARTIAL"
AGG_BLOCKED = "BLOCKED"
AGG_REQUIRES_REVIEW = "REQUIRES_REVIEW"

PAY_UNKNOWN = "UNKNOWN"
FULFILL_UNKNOWN = "UNKNOWN"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OrderLine:
    line_id: str
    sku: str = ""
    article: str = ""
    barcode: str = ""
    product_id: str = ""
    source_offer_id: str = ""
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    currency: str = ""
    mapping_status: str = MISSING
    mapping_product_id: str = ""


@dataclass(frozen=True)
class CanonicalOrder:
    tenant_id: str
    order_id: str
    external_order_id: str
    source: str
    source_status: str
    canonical_status: str
    source_order_version: str
    currency: str
    lines: tuple[OrderLine, ...]
    order_total: Decimal | None
    discount: Decimal | None
    payment_state: str
    fulfillment_state: str
    customer_ref: str
    external_customer_ref: str
    provenance: str
    raw_hash: str
    ingestion_mode: str
    idempotency_key: str
    validation_status: str
    ingest_result: str
    created_at: str
    updated_at: str
    economics_reference: dict[str, Any] = field(default_factory=dict)
    sale_price: Decimal | None = None
    list_price: Decimal | None = None
    marketplace_subsidy: Decimal | None = None
    contribution_estimate: Decimal | None = None
    cancelled: bool = False
    event_seq: int = 0

    def public_audit_ref(self) -> dict:
        return {
            "order_id": self.order_id,
            "external_order_id": self.external_order_id,
            "source": self.source,
            "customer_ref": self.customer_ref,
            "tenant_id": self.tenant_id,
        }
