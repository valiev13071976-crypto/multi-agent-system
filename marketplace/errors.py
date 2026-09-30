"""Marketplace Platform error taxonomy."""

from __future__ import annotations


class MarketplaceError(Exception):
    def __init__(self, code: str, message: str = "", *, decision=None):
        self.code = code
        # Block 5.8: when raised from Price Protection, ``decision`` carries
        # the full structured ``PriceProtectionDecision`` (breakdown,
        # profitability, reason codes) for auditability -- optional and
        # unused by every pre-existing raise site (spec section 15).
        self.decision = decision
        super().__init__(message or code)


MARKETPLACE_AUTH = "MARKETPLACE_AUTH"
MARKETPLACE_RATE_LIMIT = "MARKETPLACE_RATE_LIMIT"
MARKETPLACE_UNAVAILABLE = "MARKETPLACE_UNAVAILABLE"
MARKETPLACE_INVALID_CARD = "MARKETPLACE_INVALID_CARD"
MARKETPLACE_CATEGORY_INVALID = "MARKETPLACE_CATEGORY_INVALID"
MARKETPLACE_ATTRIBUTE_INVALID = "MARKETPLACE_ATTRIBUTE_INVALID"
MARKETPLACE_CAPABILITY_UNSUPPORTED = "MARKETPLACE_CAPABILITY_UNSUPPORTED"
MARKETPLACE_PRICE_REJECTED = "MARKETPLACE_PRICE_REJECTED"
MARKETPLACE_STOCK_REJECTED = "MARKETPLACE_STOCK_REJECTED"
MARKETPLACE_ORDER_CONFLICT = "MARKETPLACE_ORDER_CONFLICT"
MARKETPLACE_REVIEW_CONFLICT = "MARKETPLACE_REVIEW_CONFLICT"
MARKETPLACE_SYNC_CONFLICT = "MARKETPLACE_SYNC_CONFLICT"

MARKETPLACE_SELECTION_REQUIRED = "MARKETPLACE_SELECTION_REQUIRED"
MARKETPLACE_SELECTION_INVALID = "MARKETPLACE_SELECTION_INVALID"
MARKETPLACE_LISTING_CONFLICT = "MARKETPLACE_LISTING_CONFLICT"
MARKETPLACE_MAPPING_AMBIGUOUS = "MARKETPLACE_MAPPING_AMBIGUOUS"
MARKETPLACE_PRICE_FLOOR = "MARKETPLACE_PRICE_FLOOR"
MARKETPLACE_LOSS_DETECTED = "MARKETPLACE_LOSS_DETECTED"
MARKETPLACE_ECONOMICS_UNKNOWN = "MARKETPLACE_ECONOMICS_UNKNOWN"
MARKETPLACE_PROMOTION_RISK = "MARKETPLACE_PROMOTION_RISK"
MARKETPLACE_AUTO_CORRECT_DENIED = "MARKETPLACE_AUTO_CORRECT_DENIED"
MARKETPLACE_STOCK_STALE = "MARKETPLACE_STOCK_STALE"
MARKETPLACE_CROSS_TENANT = "MARKETPLACE_CROSS_TENANT"
MARKETPLACE_APPROVAL_REQUIRED = "MARKETPLACE_APPROVAL_REQUIRED"
MARKETPLACE_BATCH_REQUIRED = "MARKETPLACE_BATCH_REQUIRED"
MARKETPLACE_CANCELLED = "MARKETPLACE_CANCELLED"
MARKETPLACE_SYNC_LOOP_TERMINATED = "MARKETPLACE_SYNC_LOOP_TERMINATED"
MARKETPLACE_NOT_FOUND = "MARKETPLACE_NOT_FOUND"
MARKETPLACE_ACCESS_DENIED = "MARKETPLACE_ACCESS_DENIED"

# --- Block 5.7A: normalized external-provider error taxonomy (spec section
# 19). Reuses ``integrations.production.errors.ProviderErrorCategory``
# (the taxonomy every provider adapter's ``client.py`` already raises)
# rather than inventing a second one -- this module only adds the few
# category *names* section 19 requires that ``ProviderErrorCategory``
# doesn't already spell out 1:1, plus a normalizer that maps one to the
# other. Never used to turn a real provider error into fake success.
MARKETPLACE_AUTHENTICATION_FAILED = "MARKETPLACE_AUTHENTICATION_FAILED"
MARKETPLACE_AUTHORIZATION_FAILED = "MARKETPLACE_AUTHORIZATION_FAILED"
MARKETPLACE_VALIDATION_FAILED = "MARKETPLACE_VALIDATION_FAILED"
MARKETPLACE_CONFLICT = "MARKETPLACE_CONFLICT"
MARKETPLACE_TIMEOUT = "MARKETPLACE_TIMEOUT"
MARKETPLACE_PROVIDER_UNAVAILABLE = "MARKETPLACE_PROVIDER_UNAVAILABLE"
MARKETPLACE_TRANSIENT_PROVIDER_ERROR = "MARKETPLACE_TRANSIENT_PROVIDER_ERROR"
MARKETPLACE_PERMANENT_PROVIDER_ERROR = "MARKETPLACE_PERMANENT_PROVIDER_ERROR"

PROVIDER_ERROR_CATEGORY_MAP = {
    "AUTHENTICATION_FAILED": MARKETPLACE_AUTHENTICATION_FAILED,
    "AUTHORIZATION_FAILED": MARKETPLACE_AUTHORIZATION_FAILED,
    "RATE_LIMITED": MARKETPLACE_RATE_LIMIT,
    "QUOTA_EXCEEDED": MARKETPLACE_RATE_LIMIT,
    "TIMEOUT": MARKETPLACE_TIMEOUT,
    "NETWORK_ERROR": MARKETPLACE_TRANSIENT_PROVIDER_ERROR,
    "BAD_REQUEST": MARKETPLACE_VALIDATION_FAILED,
    "PROVIDER_ERROR": MARKETPLACE_TRANSIENT_PROVIDER_ERROR,
    "PROVIDER_UNAVAILABLE": MARKETPLACE_PROVIDER_UNAVAILABLE,
    "INVALID_RESPONSE": MARKETPLACE_PERMANENT_PROVIDER_ERROR,
    "WEBHOOK_VERIFICATION_FAILED": MARKETPLACE_PERMANENT_PROVIDER_ERROR,
    "CONFIGURATION_ERROR": MARKETPLACE_PERMANENT_PROVIDER_ERROR,
}

# Adapter/governed-platform exception *type names*
# (integrations.activation.errors / integrations.{wildberries,ozon,
# yandex_market}.errors) that already carry an unambiguous classification.
# NOTE: these exceptions are raised with a call-site-specific reason string
# as their positional ``code`` arg (e.g. ``IntegrationWriteDeniedError(
# "approval_required")``), which *overrides* the class-level ``code``
# default -- so ``.code`` alone is not a reliable discriminator. The
# exception *class name* is stable regardless of the message passed, so
# classification here is name-based (exact match for the
# ``integrations.activation.errors`` hierarchy; suffix match for the three
# provider-specific hierarchies, whose class names are constructed
# identically per provider, e.g. ``OzonNotFoundError`` /
# ``WildberriesNotFoundError`` / ``YandexMarketNotFoundError``).
_EXACT_NAME_CATEGORY_MAP = {
    "IntegrationNotConfiguredError": MARKETPLACE_PERMANENT_PROVIDER_ERROR,
    "IntegrationNotActiveError": MARKETPLACE_PERMANENT_PROVIDER_ERROR,
    "IntegrationEnvironmentMismatchError": MARKETPLACE_VALIDATION_FAILED,
    "IntegrationCapabilityUnavailableError": MARKETPLACE_CAPABILITY_UNSUPPORTED,
    "IntegrationAuthFailedError": MARKETPLACE_AUTHENTICATION_FAILED,
    "IntegrationPermissionDeniedError": MARKETPLACE_AUTHORIZATION_FAILED,
    "IntegrationRateLimitedError": MARKETPLACE_RATE_LIMIT,
    "IntegrationTimeoutNormalizedError": MARKETPLACE_TIMEOUT,
    "IntegrationProviderUnavailableError": MARKETPLACE_PROVIDER_UNAVAILABLE,
    "IntegrationVerificationFailedError": MARKETPLACE_AUTHENTICATION_FAILED,
    "IntegrationPlaintextSecretRejectedError": MARKETPLACE_PERMANENT_PROVIDER_ERROR,
    "IntegrationCrossTenantError": MARKETPLACE_ACCESS_DENIED,
    "IntegrationWriteDeniedError": MARKETPLACE_APPROVAL_REQUIRED,
    "IntegrationLiveFallbackForbiddenError": MARKETPLACE_PERMANENT_PROVIDER_ERROR,
}

_SUFFIX_NAME_CATEGORY_MAP = {
    "AmbiguousTargetError": MARKETPLACE_VALIDATION_FAILED,
    "NotFoundError": MARKETPLACE_NOT_FOUND,
    "PriceFloorError": MARKETPLACE_VALIDATION_FAILED,
    "WriteVerificationFailedError": MARKETPLACE_TRANSIENT_PROVIDER_ERROR,
    "UncertainWriteOutcomeError": MARKETPLACE_TRANSIENT_PROVIDER_ERROR,
    "UnsupportedCapabilityError": MARKETPLACE_CAPABILITY_UNSUPPORTED,
    "ImportRejectedError": MARKETPLACE_VALIDATION_FAILED,
    "ImportPendingError": MARKETPLACE_TRANSIENT_PROVIDER_ERROR,
    "FulfillmentBoundaryError": MARKETPLACE_VALIDATION_FAILED,
}


def classify_exception_type(exc: Exception) -> str | None:
    name = type(exc).__name__
    if name in _EXACT_NAME_CATEGORY_MAP:
        return _EXACT_NAME_CATEGORY_MAP[name]
    for suffix, category in _SUFFIX_NAME_CATEGORY_MAP.items():
        if name.endswith(suffix):
            return category
    return None


def classify_provider_error(exc: Exception) -> str:
    """Normalize any adapter/provider-layer exception into one of the
    section-19 categories. Never retries/masks the original exception --
    callers still re-raise/propagate ``exc`` as-is; this only labels it."""
    category = getattr(exc, "category", None)
    category_name = getattr(category, "value", None) or getattr(category, "name", None) or str(category or "")
    if category_name in PROVIDER_ERROR_CATEGORY_MAP:
        return PROVIDER_ERROR_CATEGORY_MAP[category_name]
    by_type = classify_exception_type(exc)
    if by_type is not None:
        return by_type
    code = getattr(exc, "code", "") or ""
    if code == MARKETPLACE_PRICE_FLOOR:
        return MARKETPLACE_VALIDATION_FAILED
    if code in {MARKETPLACE_NOT_FOUND}:
        return MARKETPLACE_NOT_FOUND
    if code in {MARKETPLACE_ORDER_CONFLICT, MARKETPLACE_LISTING_CONFLICT, MARKETPLACE_SYNC_CONFLICT}:
        return MARKETPLACE_CONFLICT
    if code in {MARKETPLACE_RATE_LIMIT, MARKETPLACE_UNAVAILABLE, MARKETPLACE_AUTH}:
        return {
            MARKETPLACE_RATE_LIMIT: MARKETPLACE_RATE_LIMIT,
            MARKETPLACE_UNAVAILABLE: MARKETPLACE_PROVIDER_UNAVAILABLE,
            MARKETPLACE_AUTH: MARKETPLACE_AUTHENTICATION_FAILED,
        }[code]
    if code in {MARKETPLACE_INVALID_CARD, MARKETPLACE_CATEGORY_INVALID, MARKETPLACE_ATTRIBUTE_INVALID}:
        return MARKETPLACE_VALIDATION_FAILED
    return MARKETPLACE_PERMANENT_PROVIDER_ERROR
