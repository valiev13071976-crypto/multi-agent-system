"""Structured security errors — safe public responses."""

from __future__ import annotations


class SecurityError(Exception):
    error_code: str = "security_error"

    def __init__(self, message: str = "", *, error_code: str | None = None):
        self.error_code = error_code or self.__class__.error_code
        super().__init__(message or self.error_code)


class UnauthenticatedError(SecurityError):
    error_code = "unauthenticated"


class UnauthorizedError(SecurityError):
    error_code = "unauthorized"


class TenantMismatchError(SecurityError):
    error_code = "tenant_mismatch"


class ResourceNotFoundError(SecurityError):
    """Safe not-found for cross-tenant IDOR — no metadata leak."""
    error_code = "not_found"


class DisabledAccountError(SecurityError):
    error_code = "disabled_account"


class RateLimitedError(SecurityError):
    error_code = "rate_limited"

    def __init__(self, *, retry_after_seconds: float | None = None):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("rate_limited")
