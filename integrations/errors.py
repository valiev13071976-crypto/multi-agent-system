"""Integration error taxonomy — safe public codes, no secret leakage."""

from __future__ import annotations


class IntegrationError(Exception):
    code = "integration_error"

    def __init__(self, code: str | None = None, *, message: str | None = None):
        self.code = code or type(self).code
        # Never put secrets into message; keep generic.
        super().__init__(message or self.code)


class IntegrationNotFoundError(IntegrationError):
    code = "integration_not_found"


class IntegrationDisabledError(IntegrationError):
    code = "integration_disabled"


class IntegrationAccessDeniedError(IntegrationError):
    code = "integration_access_denied"


class CredentialMissingError(IntegrationError):
    code = "credential_missing"


class CredentialInvalidError(IntegrationError):
    code = "credential_invalid"


class CredentialExpiredError(IntegrationError):
    code = "credential_expired"


class SecretBackendUnavailableError(IntegrationError):
    code = "secret_backend_unavailable"


class AuthenticationFailedError(IntegrationError):
    code = "authentication_failed"


class ScopeInsufficientError(IntegrationError):
    code = "scope_insufficient"


class IntegrationUnavailableError(IntegrationError):
    code = "integration_unavailable"


class CircuitOpenError(IntegrationError):
    code = "circuit_open"


class IntegrationTimeoutError(IntegrationError):
    code = "integration_timeout"


class ExternalRateLimitedError(IntegrationError):
    code = "external_rate_limited"


class ExternalTransientFailureError(IntegrationError):
    code = "external_transient_failure"


class ExternalPermanentFailureError(IntegrationError):
    code = "external_permanent_failure"


class WebhookSignatureInvalidError(IntegrationError):
    code = "webhook_signature_invalid"


class WebhookReplayError(IntegrationError):
    code = "webhook_replay"


class IdempotencyConflictError(IntegrationError):
    code = "idempotency_conflict"


class HostNotAllowedError(IntegrationError):
    code = "host_not_allowed"


class IpAllowlistDeniedError(IntegrationError):
    code = "ip_allowlist_denied"
