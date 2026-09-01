"""Wildberries integration errors."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class WildberriesIntegrationError(ActivationError):
    code = "WILDBERRIES_INTEGRATION_ERROR"


class WildberriesAmbiguousTargetError(WildberriesIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class WildberriesNotFoundError(WildberriesIntegrationError):
    code = "INTEGRATION_NOT_FOUND"


class WildberriesPriceFloorError(WildberriesIntegrationError):
    code = "MARKETPLACE_PRICE_FLOOR"


class WildberriesWriteVerificationFailedError(WildberriesIntegrationError):
    code = "INTEGRATION_WRITE_VERIFICATION_FAILED"


class WildberriesUncertainWriteOutcomeError(WildberriesIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"


class WildberriesUnsupportedCapabilityError(WildberriesIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"
