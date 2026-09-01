"""Ozon integration errors."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class OzonIntegrationError(ActivationError):
    code = "OZON_INTEGRATION_ERROR"


class OzonAmbiguousTargetError(OzonIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class OzonNotFoundError(OzonIntegrationError):
    code = "INTEGRATION_NOT_FOUND"


class OzonPriceFloorError(OzonIntegrationError):
    code = "MARKETPLACE_PRICE_FLOOR"


class OzonWriteVerificationFailedError(OzonIntegrationError):
    code = "INTEGRATION_WRITE_VERIFICATION_FAILED"


class OzonUncertainWriteOutcomeError(OzonIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"


class OzonUnsupportedCapabilityError(OzonIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"


class OzonImportRejectedError(OzonIntegrationError):
    code = "IMPORT_REJECTED"


class OzonImportPendingError(OzonIntegrationError):
    code = "IMPORT_PENDING"


class OzonFulfillmentBoundaryError(OzonIntegrationError):
    code = "UNSUPPORTED_CAPABILITY"
