"""Bitrix integration error normalization."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class BitrixIntegrationError(ActivationError):
    code = "BITRIX_INTEGRATION_ERROR"


class BitrixAmbiguousTargetError(BitrixIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class BitrixNotFoundError(BitrixIntegrationError):
    code = "INTEGRATION_NOT_FOUND"


class BitrixValidationError(BitrixIntegrationError):
    code = "INTEGRATION_VALIDATION_FAILED"


class BitrixWriteVerificationFailedError(BitrixIntegrationError):
    code = "INTEGRATION_WRITE_VERIFICATION_FAILED"


class BitrixUnsupportedCapabilityError(BitrixIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"


class BitrixUncertainWriteOutcomeError(BitrixIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"
