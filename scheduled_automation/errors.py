"""Scheduled automation errors."""

from __future__ import annotations


class ScheduledAutomationError(Exception):
    code = "SCHEDULED_AUTOMATION_ERROR"
    http_status = 400

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


INVALID_SCHEDULE = "INVALID_SCHEDULE"
INVALID_TIMEZONE = "INVALID_TIMEZONE"
INVALID_RECURRENCE = "INVALID_RECURRENCE"
UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
FORBIDDEN = "FORBIDDEN"
TENANT_SCOPE_VIOLATION = "TENANT_SCOPE_VIOLATION"
SCHEDULE_NOT_FOUND = "SCHEDULE_NOT_FOUND"
STALE_VERSION = "STALE_VERSION"
CAPABILITY_DENIED = "CAPABILITY_DENIED"
BUDGET_DENIED = "BUDGET_DENIED"
OVERLAP_BLOCKED = "OVERLAP_BLOCKED"
LIVE_FALLBACK_FORBIDDEN = "LIVE_FALLBACK_FORBIDDEN"
