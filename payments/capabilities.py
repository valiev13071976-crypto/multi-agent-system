"""Domain capabilities for payments — separate from LLM default grants."""

from __future__ import annotations

CAP_PAYMENTS_READ = "payments.read"
CAP_PAYMENTS_RECONCILE = "payments.reconcile"
CAP_PAYMENTS_ALLOCATE = "payments.allocate"
CAP_PAYMENTS_PREPARE_REFUND = "payments.prepare_refund"
CAP_PAYMENTS_EXECUTE_REFUND = "payments.execute_refund"
CAP_BANK_READ = "bank.read"
CAP_BANK_STATEMENT_READ = "bank.statement.read"
CAP_BANK_TRANSACTION_READ = "bank.transaction.read"

PAYMENTS_CAPABILITIES = (
    CAP_PAYMENTS_READ,
    CAP_PAYMENTS_RECONCILE,
    CAP_PAYMENTS_ALLOCATE,
    CAP_PAYMENTS_PREPARE_REFUND,
    CAP_PAYMENTS_EXECUTE_REFUND,
    CAP_BANK_READ,
    CAP_BANK_STATEMENT_READ,
    CAP_BANK_TRANSACTION_READ,
)

LLM_DEFAULT_DENY = frozenset(
    {
        CAP_PAYMENTS_EXECUTE_REFUND,
    }
)
