"""Payments error taxonomy — safe codes only."""

from __future__ import annotations


class PaymentsError(Exception):
    code = "payments_error"

    def __init__(self, code: str | None = None):
        self.code = code or type(self).code
        super().__init__(self.code)


class InvalidTransitionError(PaymentsError):
    code = "invalid_transition"


class CapabilityDeniedError(PaymentsError):
    code = "capability_denied"


class CardDataForbiddenError(PaymentsError):
    code = "card_data_forbidden"


class TenantAccessDeniedError(PaymentsError):
    code = "tenant_access_denied"


class IdempotencyError(PaymentsError):
    code = "idempotency_conflict"


class MatchConflictError(PaymentsError):
    code = "match_conflict"


class ExternalUnconfirmedError(PaymentsError):
    code = "external_unconfirmed"


class RefundNotPreparedError(PaymentsError):
    code = "refund_not_prepared"


class PolicyDeniedError(PaymentsError):
    code = "policy_denied"


class CurrencyMismatchError(PaymentsError):
    code = "currency_mismatch"
