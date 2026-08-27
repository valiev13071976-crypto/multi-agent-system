"""Commerce error taxonomy — safe codes only."""

from __future__ import annotations


class CommerceError(Exception):
    code = "commerce_error"

    def __init__(self, code: str | None = None):
        self.code = code or type(self).code
        super().__init__(self.code)


class InvalidTransitionError(CommerceError):
    code = "invalid_transition"


class DeclarationRequiredError(CommerceError):
    code = "declaration_required"


class DeclarationImmutableError(CommerceError):
    code = "declaration_immutable"


class InsufficientStockError(CommerceError):
    code = "insufficient_stock"


class StaleStateError(CommerceError):
    code = "stale_state"


class OversellError(CommerceError):
    code = "oversell_prevented"


class CapabilityDeniedError(CommerceError):
    code = "capability_denied"


class ExternalUnconfirmedError(CommerceError):
    code = "external_unconfirmed"


class IdempotencyError(CommerceError):
    code = "idempotency_conflict"


class ComplianceForbiddenError(CommerceError):
    code = "compliance_forbidden"


class NeedsReviewError(CommerceError):
    code = "needs_review"


class TenantAccessDeniedError(CommerceError):
    code = "tenant_access_denied"


class CardDataForbiddenError(CommerceError):
    code = "card_data_forbidden"
