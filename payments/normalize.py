"""Provider event normalization into canonical payment event types."""

from __future__ import annotations

from typing import Mapping

CANONICAL_EVENTS = frozenset(
    {
        "payment.created",
        "payment.pending",
        "payment.authorized",
        "payment.succeeded",
        "payment.failed",
        "payment.cancelled",
        "refund.created",
        "refund.succeeded",
        "refund.failed",
        "chargeback.created",
        "dispute.updated",
        "bank.transaction.created",
        "bank.statement.ready",
    }
)

_PROVIDER_ALIASES = {
    "payment_intent.created": "payment.created",
    "payment_intent.processing": "payment.pending",
    "payment_intent.succeeded": "payment.succeeded",
    "payment_intent.payment_failed": "payment.failed",
    "payment_intent.canceled": "payment.cancelled",
    "charge.succeeded": "payment.succeeded",
    "charge.failed": "payment.failed",
    "charge.refunded": "refund.succeeded",
    "refund.updated": "refund.succeeded",
    "charge.dispute.created": "chargeback.created",
    "charge.dispute.updated": "dispute.updated",
    "bank.incoming": "bank.transaction.created",
    "statement.ready": "bank.statement.ready",
    "payment.captured": "payment.succeeded",
    "payment.paid": "payment.succeeded",
}


def normalize_event_type(provider_event_type: str) -> str:
    raw = str(provider_event_type or "").strip()
    if raw in CANONICAL_EVENTS:
        return raw
    aliased = _PROVIDER_ALIASES.get(raw) or _PROVIDER_ALIASES.get(raw.lower())
    if aliased:
        return aliased
    lower = raw.lower().replace(" ", ".")
    if lower in CANONICAL_EVENTS:
        return lower
    return "payment.pending"


def extract_safe_payment_fields(payload: Mapping[str, object] | None) -> dict:
    """Pull only safe refs from provider payload — never card data."""
    data = dict(payload or {})
    return {
        "external_transaction_id": str(
            data.get("external_transaction_id")
            or data.get("transaction_id")
            or data.get("id")
            or ""
        ),
        "payment_id": str(data.get("payment_id") or data.get("id") or ""),
        "amount": float(data.get("amount") or 0),
        "currency": str(data.get("currency") or "RUB").upper(),
        "status": str(data.get("status") or ""),
        "order_ref": str(data.get("order_ref") or data.get("order_id") or ""),
        "invoice_ref": str(data.get("invoice_ref") or data.get("invoice_id") or ""),
        "payer_ref": str(data.get("payer_ref") or ""),
        "payer_inn": str(data.get("payer_inn") or data.get("inn") or ""),
        "payer_name": str(data.get("payer_name") or data.get("legal_name") or ""),
        "refund_id": str(data.get("refund_id") or ""),
        "refund_amount": float(data.get("refund_amount") or data.get("amount") or 0)
        if "refund" in str(data.get("type") or "").lower()
        or data.get("refund_id")
        else 0.0,
        "event_id": str(data.get("event_id") or data.get("id") or ""),
        "payment_method_type": str(
            data.get("payment_method_type") or data.get("method") or ""
        ),
        "masked_method": str(data.get("masked_method") or data.get("last4_hint") or ""),
    }


def event_to_payment_status(event_type: str) -> str | None:
    from payments.states import (
        PAY_AUTHORIZED,
        PAY_CANCELLED,
        PAY_CHARGEBACK,
        PAY_CREATED,
        PAY_DISPUTED,
        PAY_FAILED,
        PAY_PAID,
        PAY_PENDING,
        PAY_REFUNDED,
        PAY_REFUND_PENDING,
    )

    mapping = {
        "payment.created": PAY_CREATED,
        "payment.pending": PAY_PENDING,
        "payment.authorized": PAY_AUTHORIZED,
        "payment.succeeded": PAY_PAID,
        "payment.failed": PAY_FAILED,
        "payment.cancelled": PAY_CANCELLED,
        "refund.created": PAY_REFUND_PENDING,
        "refund.succeeded": PAY_REFUNDED,
        "refund.failed": PAY_PAID,
        "chargeback.created": PAY_CHARGEBACK,
        "dispute.updated": PAY_DISPUTED,
    }
    return mapping.get(event_type)
