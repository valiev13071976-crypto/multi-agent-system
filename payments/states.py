"""Deterministic payment and refund state machines. LLM cannot set states directly."""

from __future__ import annotations

from payments.errors import InvalidTransitionError

# Payment states
PAY_CREATED = "CREATED"
PAY_PENDING = "PENDING"
PAY_AUTHORIZED = "AUTHORIZED"
PAY_PAID = "PAID"
PAY_CAPTURED = "CAPTURED"
PAY_PARTIALLY_PAID = "PARTIALLY_PAID"
PAY_OVERPAID = "OVERPAID"
PAY_UNDERPAID = "UNDERPAID"
PAY_CANCELLED = "CANCELLED"
PAY_FAILED = "FAILED"
PAY_REFUND_PENDING = "REFUND_PENDING"
PAY_PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
PAY_REFUNDED = "REFUNDED"
PAY_CHARGEBACK = "CHARGEBACK"
PAY_DISPUTED = "DISPUTED"
PAY_UNKNOWN = "UNKNOWN"
PAY_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

PAYMENT_STATES = frozenset(
    {
        PAY_CREATED,
        PAY_PENDING,
        PAY_AUTHORIZED,
        PAY_PAID,
        PAY_CAPTURED,
        PAY_PARTIALLY_PAID,
        PAY_OVERPAID,
        PAY_UNDERPAID,
        PAY_CANCELLED,
        PAY_FAILED,
        PAY_REFUND_PENDING,
        PAY_PARTIALLY_REFUNDED,
        PAY_REFUNDED,
        PAY_CHARGEBACK,
        PAY_DISPUTED,
        PAY_UNKNOWN,
        PAY_RECONCILIATION_REQUIRED,
    }
)

# Treat PAID and CAPTURED as equivalent success terminals for transitions
_PAYMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    PAY_CREATED: frozenset(
        {PAY_PENDING, PAY_AUTHORIZED, PAY_PAID, PAY_CAPTURED, PAY_FAILED, PAY_CANCELLED, PAY_UNKNOWN}
    ),
    PAY_PENDING: frozenset(
        {
            PAY_AUTHORIZED,
            PAY_PAID,
            PAY_CAPTURED,
            PAY_FAILED,
            PAY_CANCELLED,
            PAY_UNKNOWN,
            PAY_RECONCILIATION_REQUIRED,
        }
    ),
    PAY_AUTHORIZED: frozenset(
        {PAY_PAID, PAY_CAPTURED, PAY_CANCELLED, PAY_FAILED, PAY_UNKNOWN, PAY_RECONCILIATION_REQUIRED}
    ),
    PAY_PAID: frozenset(
        {
            PAY_CAPTURED,
            PAY_PARTIALLY_PAID,
            PAY_OVERPAID,
            PAY_UNDERPAID,
            PAY_REFUND_PENDING,
            PAY_PARTIALLY_REFUNDED,
            PAY_REFUNDED,
            PAY_CHARGEBACK,
            PAY_DISPUTED,
            PAY_RECONCILIATION_REQUIRED,
        }
    ),
    PAY_CAPTURED: frozenset(
        {
            PAY_PAID,
            PAY_REFUND_PENDING,
            PAY_PARTIALLY_REFUNDED,
            PAY_REFUNDED,
            PAY_CHARGEBACK,
            PAY_DISPUTED,
            PAY_RECONCILIATION_REQUIRED,
        }
    ),
    PAY_PARTIALLY_PAID: frozenset(
        {PAY_PAID, PAY_OVERPAID, PAY_UNDERPAID, PAY_REFUND_PENDING, PAY_RECONCILIATION_REQUIRED}
    ),
    PAY_OVERPAID: frozenset(
        {PAY_PAID, PAY_REFUND_PENDING, PAY_RECONCILIATION_REQUIRED, PAY_DISPUTED}
    ),
    PAY_UNDERPAID: frozenset(
        {PAY_PARTIALLY_PAID, PAY_PAID, PAY_CANCELLED, PAY_RECONCILIATION_REQUIRED}
    ),
    PAY_CANCELLED: frozenset({PAY_RECONCILIATION_REQUIRED}),
    PAY_FAILED: frozenset({PAY_PENDING, PAY_RECONCILIATION_REQUIRED, PAY_UNKNOWN}),
    PAY_REFUND_PENDING: frozenset(
        {
            PAY_PARTIALLY_REFUNDED,
            PAY_REFUNDED,
            PAY_PAID,
            PAY_CAPTURED,
            PAY_FAILED,
            PAY_UNKNOWN,
            PAY_RECONCILIATION_REQUIRED,
        }
    ),
    PAY_PARTIALLY_REFUNDED: frozenset(
        {PAY_REFUNDED, PAY_REFUND_PENDING, PAY_CHARGEBACK, PAY_RECONCILIATION_REQUIRED}
    ),
    PAY_REFUNDED: frozenset({PAY_CHARGEBACK, PAY_DISPUTED, PAY_RECONCILIATION_REQUIRED}),
    PAY_CHARGEBACK: frozenset({PAY_DISPUTED, PAY_RECONCILIATION_REQUIRED, PAY_REFUNDED}),
    PAY_DISPUTED: frozenset(
        {PAY_CHARGEBACK, PAY_REFUNDED, PAY_PAID, PAY_RECONCILIATION_REQUIRED}
    ),
    PAY_UNKNOWN: frozenset(
        {
            PAY_PENDING,
            PAY_PAID,
            PAY_CAPTURED,
            PAY_FAILED,
            PAY_CANCELLED,
            PAY_RECONCILIATION_REQUIRED,
        }
    ),
    PAY_RECONCILIATION_REQUIRED: frozenset(
        {
            PAY_PAID,
            PAY_CAPTURED,
            PAY_FAILED,
            PAY_CANCELLED,
            PAY_REFUND_PENDING,
            PAY_REFUNDED,
            PAY_DISPUTED,
            PAY_UNKNOWN,
        }
    ),
}

# Refund states
REF_REQUESTED = "REQUESTED"
REF_PREPARED = "PREPARED"
REF_AWAITING_APPROVAL = "AWAITING_APPROVAL"
REF_SUBMITTED = "SUBMITTED"
REF_CONFIRMED = "CONFIRMED"
REF_REJECTED = "REJECTED"
REF_FAILED = "FAILED"
REF_PARTIAL = "PARTIAL"
REF_UNKNOWN_EXTERNAL = "UNKNOWN_EXTERNAL_STATE"

REFUND_STATES = frozenset(
    {
        REF_REQUESTED,
        REF_PREPARED,
        REF_AWAITING_APPROVAL,
        REF_SUBMITTED,
        REF_CONFIRMED,
        REF_REJECTED,
        REF_FAILED,
        REF_PARTIAL,
        REF_UNKNOWN_EXTERNAL,
    }
)

_REFUND_TRANSITIONS: dict[str, frozenset[str]] = {
    REF_REQUESTED: frozenset({REF_PREPARED, REF_REJECTED, REF_FAILED}),
    REF_PREPARED: frozenset({REF_AWAITING_APPROVAL, REF_REJECTED, REF_FAILED}),
    REF_AWAITING_APPROVAL: frozenset(
        {REF_SUBMITTED, REF_REJECTED, REF_FAILED, REF_PREPARED}
    ),
    REF_SUBMITTED: frozenset(
        {REF_CONFIRMED, REF_FAILED, REF_PARTIAL, REF_UNKNOWN_EXTERNAL}
    ),
    REF_UNKNOWN_EXTERNAL: frozenset(
        {REF_CONFIRMED, REF_FAILED, REF_PARTIAL, REF_SUBMITTED}
    ),
    REF_PARTIAL: frozenset({REF_CONFIRMED, REF_FAILED, REF_SUBMITTED}),
    REF_CONFIRMED: frozenset(),
    REF_REJECTED: frozenset(),
    REF_FAILED: frozenset({REF_REQUESTED, REF_UNKNOWN_EXTERNAL}),
}

# Fulfillment unlock codes (deterministic)
UNLOCK_NOT_CONFIRMED = "PAYMENT_NOT_CONFIRMED"
UNLOCK_PARTIAL = "PAYMENT_PARTIAL"
UNLOCK_CONFIRMED = "PAYMENT_CONFIRMED"
UNLOCK_REVIEW = "PAYMENT_REVIEW_REQUIRED"
UNLOCK_BLOCKED = "PAYMENT_BLOCKED"

UNLOCK_CODES = frozenset(
    {
        UNLOCK_NOT_CONFIRMED,
        UNLOCK_PARTIAL,
        UNLOCK_CONFIRMED,
        UNLOCK_REVIEW,
        UNLOCK_BLOCKED,
    }
)

# Allocation statuses
ALLOC_PENDING = "PENDING"
ALLOC_CONFIRMED = "CONFIRMED"
ALLOC_SUPERSEDED = "SUPERSEDED"
ALLOC_REVIEW = "REVIEW_REQUIRED"


def can_transition(kind: str, current: str, target: str) -> bool:
    table = _PAYMENT_TRANSITIONS if kind == "payment" else _REFUND_TRANSITIONS
    allowed = table.get(current, frozenset())
    return target in allowed


def assert_transition(kind: str, current: str, target: str) -> None:
    if current == target:
        return
    if not can_transition(kind, current, target):
        raise InvalidTransitionError("invalid_transition")
