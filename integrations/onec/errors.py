"""1C integration error normalization."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class OneCIntegrationError(ActivationError):
    code = "ONEC_INTEGRATION_ERROR"


class OneCAmbiguousTargetError(OneCIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class OneCNotFoundError(OneCIntegrationError):
    code = "INTEGRATION_NOT_FOUND"


class OneCValidationError(OneCIntegrationError):
    code = "INTEGRATION_VALIDATION_FAILED"


class OneCWriteVerificationFailedError(OneCIntegrationError):
    code = "INTEGRATION_WRITE_VERIFICATION_FAILED"


class OneCUncertainWriteOutcomeError(OneCIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"


class OneCUnsupportedCapabilityError(OneCIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"
