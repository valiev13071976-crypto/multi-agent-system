"""Operations admin errors — safe structured codes."""

from __future__ import annotations


class AdminError(Exception):
    def __init__(self, code: str, *, message: str = "", retryable: bool = False):
        self.code = code
        self.message = message or code
        self.retryable = retryable
        super().__init__(self.code)


ADMIN_UNAUTHORIZED = "admin_unauthorized"
ADMIN_FORBIDDEN = "admin_forbidden"
ADMIN_SCOPE_DENIED = "admin_scope_denied"
ADMIN_TARGET_NOT_FOUND = "admin_target_not_found"
ADMIN_STALE_STATE = "admin_stale_state"
ADMIN_CONFIRMATION_REQUIRED = "admin_confirmation_required"
ADMIN_CONFIRMATION_INVALID = "admin_confirmation_invalid"
ADMIN_ACTION_NOT_ALLOWED = "admin_action_not_allowed"
ADMIN_RETRY_NOT_ALLOWED = "admin_retry_not_allowed"
ADMIN_REDRIVE_NOT_ALLOWED = "admin_redrive_not_allowed"
ADMIN_AUDIT_REQUIRED = "admin_audit_required"
ADMIN_AUDIT_FAILED = "admin_audit_failed"
ADMIN_INVALID_FILTER = "admin_invalid_filter"
ADMIN_INVALID_PAGE = "admin_invalid_page"
ADMIN_CONFLICT = "admin_conflict"
