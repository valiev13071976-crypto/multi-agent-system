"""Tool failure reason taxonomy with retryable flags."""

from __future__ import annotations

from dataclasses import dataclass

from tools.errors import (
    ToolArgumentInvalidError,
    ToolApprovalRequiredError,
    ToolCancelledError,
    ToolConflictError,
    ToolError,
    ToolPermissionDeniedError,
    ToolPermanentFailureError,
    ToolPolicyDeniedError,
    ToolRateLimitedError,
    ToolTimeoutError,
    ToolTransientFailureError,
    ToolUnavailableError,
)

REASON_VALIDATION_ERROR = "validation_error"
REASON_UNAUTHORIZED = "unauthorized"
REASON_APPROVAL_REQUIRED = "approval_required"
REASON_UNAVAILABLE = "unavailable"
REASON_TIMEOUT = "timeout"
REASON_RATE_LIMITED = "rate_limited"
REASON_TRANSIENT_UPSTREAM = "transient_upstream"
REASON_PERMANENT_UPSTREAM = "permanent_upstream"
REASON_CONFLICT = "conflict"
REASON_CANCELLED = "cancelled"
REASON_INTERNAL_ADAPTER_FAILURE = "internal_adapter_failure"

FAILURE_REASONS = (
    REASON_VALIDATION_ERROR,
    REASON_UNAUTHORIZED,
    REASON_APPROVAL_REQUIRED,
    REASON_UNAVAILABLE,
    REASON_TIMEOUT,
    REASON_RATE_LIMITED,
    REASON_TRANSIENT_UPSTREAM,
    REASON_PERMANENT_UPSTREAM,
    REASON_CONFLICT,
    REASON_CANCELLED,
    REASON_INTERNAL_ADAPTER_FAILURE,
)

_RETRYABLE = frozenset(
    {
        REASON_UNAVAILABLE,
        REASON_TIMEOUT,
        REASON_RATE_LIMITED,
        REASON_TRANSIENT_UPSTREAM,
    }
)


@dataclass(frozen=True)
class FailureInfo:
    reason_code: str
    retryable: bool
    error_code: str = ""

    def as_dict(self) -> dict:
        return {
            "reason_code": self.reason_code,
            "retryable": self.retryable,
            "error_code": self.error_code or self.reason_code,
        }


def is_retryable(reason_code: str) -> bool:
    return str(reason_code or "") in _RETRYABLE


def failure_info(reason_code: str, *, error_code: str = "") -> FailureInfo:
    code = str(reason_code or REASON_INTERNAL_ADAPTER_FAILURE)
    if code not in FAILURE_REASONS:
        code = REASON_INTERNAL_ADAPTER_FAILURE
    return FailureInfo(
        reason_code=code,
        retryable=is_retryable(code),
        error_code=error_code or code,
    )


def classify_exception(exc: BaseException) -> FailureInfo:
    """Map known tool exceptions → taxonomy."""
    if isinstance(exc, ToolArgumentInvalidError):
        return failure_info(REASON_VALIDATION_ERROR, error_code=exc.error_code)
    if isinstance(exc, (ToolPermissionDeniedError, ToolPolicyDeniedError)):
        return failure_info(REASON_UNAUTHORIZED, error_code=getattr(exc, "error_code", ""))
    if isinstance(exc, ToolApprovalRequiredError):
        return failure_info(REASON_APPROVAL_REQUIRED, error_code=exc.error_code)
    if isinstance(exc, ToolUnavailableError):
        return failure_info(REASON_UNAVAILABLE, error_code=exc.error_code)
    if isinstance(exc, ToolTimeoutError):
        return failure_info(REASON_TIMEOUT, error_code=exc.error_code)
    if isinstance(exc, ToolRateLimitedError):
        return failure_info(REASON_RATE_LIMITED, error_code=exc.error_code)
    if isinstance(exc, ToolTransientFailureError):
        return failure_info(REASON_TRANSIENT_UPSTREAM, error_code=exc.error_code)
    if isinstance(exc, ToolPermanentFailureError):
        return failure_info(REASON_PERMANENT_UPSTREAM, error_code=exc.error_code)
    if isinstance(exc, ToolConflictError):
        return failure_info(REASON_CONFLICT, error_code=exc.error_code)
    if isinstance(exc, ToolCancelledError):
        return failure_info(REASON_CANCELLED, error_code=exc.error_code)
    if isinstance(exc, ToolError):
        code = str(getattr(exc, "error_code", "") or "")
        if "timeout" in code:
            return failure_info(REASON_TIMEOUT, error_code=code)
        if "rate" in code:
            return failure_info(REASON_RATE_LIMITED, error_code=code)
        if "denied" in code or "permission" in code or "unauthorized" in code:
            return failure_info(REASON_UNAUTHORIZED, error_code=code)
        if "approval" in code:
            return failure_info(REASON_APPROVAL_REQUIRED, error_code=code)
        if "unavailable" in code or "disabled" in code:
            return failure_info(REASON_UNAVAILABLE, error_code=code)
        return failure_info(REASON_INTERNAL_ADAPTER_FAILURE, error_code=code or "tool_execution_failed")
    return failure_info(REASON_INTERNAL_ADAPTER_FAILURE, error_code="tool_execution_failed")
