"""SaaS product structured errors."""

from __future__ import annotations


class SaaSError(Exception):
    def __init__(self, code: str, *, message: str = "", retryable: bool = False):
        self.code = code
        self.message = message or code
        self.retryable = retryable
        super().__init__(self.code)


SAAS_UNAUTHORIZED = "saas_unauthorized"
SAAS_FORBIDDEN = "saas_forbidden"
SAAS_SCOPE_DENIED = "saas_scope_denied"
SAAS_NOT_FOUND = "saas_not_found"
SAAS_CONFLICT = "saas_conflict"
SAAS_STALE_STATE = "saas_stale_state"
SAAS_CONFIRMATION_REQUIRED = "saas_confirmation_required"
SAAS_CONFIRMATION_INVALID = "saas_confirmation_invalid"
SAAS_ENTITLEMENT_DENIED = "saas_entitlement_denied"
SAAS_QUOTA_EXCEEDED = "saas_quota_exceeded"
SAAS_BILLING_DENIED = "saas_billing_denied"
SAAS_WEBHOOK_INVALID = "saas_webhook_invalid"
SAAS_LAST_OWNER = "saas_last_owner"
SAAS_SELF_ESCALATION = "saas_self_escalation"
SAAS_INVALID_FILTER = "saas_invalid_filter"
SAAS_INVALID_PAGE = "saas_invalid_page"
