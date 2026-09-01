"""Yandex Market integration errors."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class YandexMarketIntegrationError(ActivationError):
    code = "YANDEX_MARKET_INTEGRATION_ERROR"


class YandexMarketAmbiguousTargetError(YandexMarketIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class YandexMarketNotFoundError(YandexMarketIntegrationError):
    code = "INTEGRATION_NOT_FOUND"


class YandexMarketPriceFloorError(YandexMarketIntegrationError):
    code = "MARKETPLACE_PRICE_FLOOR"


class YandexMarketWriteVerificationFailedError(YandexMarketIntegrationError):
    code = "INTEGRATION_WRITE_VERIFICATION_FAILED"


class YandexMarketUncertainWriteOutcomeError(YandexMarketIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"


class YandexMarketUnsupportedCapabilityError(YandexMarketIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"


class YandexMarketSubmissionRejectedError(YandexMarketIntegrationError):
    code = "SUBMISSION_REJECTED"


class YandexMarketFulfillmentBoundaryError(YandexMarketIntegrationError):
    code = "UNSUPPORTED_CAPABILITY"


class YandexMarketScopeError(YandexMarketIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"
