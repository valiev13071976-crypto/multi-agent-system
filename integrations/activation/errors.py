"""Real Integration Activation — error taxonomy."""

from __future__ import annotations

from integrations.errors import IntegrationError


class ActivationError(IntegrationError):
    code = "integration_activation_error"


class IntegrationNotConfiguredError(ActivationError):
    code = "INTEGRATION_NOT_CONFIGURED"


class IntegrationNotActiveError(ActivationError):
    code = "INTEGRATION_NOT_ACTIVE"


class IntegrationEnvironmentMismatchError(ActivationError):
    code = "INTEGRATION_ENVIRONMENT_MISMATCH"


class IntegrationCapabilityUnavailableError(ActivationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"


class IntegrationAuthFailedError(ActivationError):
    code = "INTEGRATION_AUTH_FAILED"


class IntegrationPermissionDeniedError(ActivationError):
    code = "INTEGRATION_PERMISSION_DENIED"


class IntegrationRateLimitedError(ActivationError):
    code = "INTEGRATION_RATE_LIMITED"


class IntegrationTimeoutNormalizedError(ActivationError):
    code = "INTEGRATION_TIMEOUT"


class IntegrationProviderUnavailableError(ActivationError):
    code = "INTEGRATION_PROVIDER_UNAVAILABLE"


class IntegrationVerificationFailedError(ActivationError):
    code = "INTEGRATION_VERIFICATION_FAILED"


class IntegrationPlaintextSecretRejectedError(ActivationError):
    code = "INTEGRATION_PLAINTEXT_SECRET_REJECTED"


class IntegrationCrossTenantError(ActivationError):
    code = "INTEGRATION_CROSS_TENANT"


class IntegrationWriteDeniedError(ActivationError):
    code = "INTEGRATION_WRITE_DENIED"


class IntegrationLiveFallbackForbiddenError(ActivationError):
    code = "INTEGRATION_LIVE_FALLBACK_FORBIDDEN"
