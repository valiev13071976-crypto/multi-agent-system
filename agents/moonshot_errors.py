"""Canonical Moonshot / Kimi error categories — no secrets in messages."""

from __future__ import annotations


MOONSHOT_AUTH = "auth"
MOONSHOT_RATE_LIMIT = "rate_limit"
MOONSHOT_TIMEOUT = "timeout"
MOONSHOT_INVALID_REQUEST = "invalid_request"
MOONSHOT_CONTEXT_LENGTH = "context_length"
MOONSHOT_PROVIDER_UNAVAILABLE = "provider_unavailable"
MOONSHOT_MALFORMED_RESPONSE = "malformed_response"
MOONSHOT_UNKNOWN = "unknown"
MOONSHOT_DISABLED = "disabled"
MOONSHOT_MISSING_KEY = "missing_key"
MOONSHOT_MISSING_MODEL = "missing_model"
MOONSHOT_ENDPOINT_DENIED = "endpoint_denied"


class MoonshotProviderError(RuntimeError):
    """Normalized Moonshot failure. Message is category-only (no key/body leak)."""

    def __init__(self, category: str, *, retryable: bool = False, status_code: int | None = None):
        self.category = category
        self.retryable = bool(retryable)
        self.status_code = status_code
        super().__init__(category)

    def __repr__(self) -> str:
        return (
            f"MoonshotProviderError(category={self.category!r}, "
            f"retryable={self.retryable}, status_code={self.status_code})"
        )


def classify_http_status(status_code: int) -> tuple[str, bool]:
    """Return (category, retryable). Auth is non-retryable."""

    code = int(status_code)
    if code in {401, 403}:
        return MOONSHOT_AUTH, False
    if code == 429:
        return MOONSHOT_RATE_LIMIT, True
    if code == 408:
        return MOONSHOT_TIMEOUT, True
    if code == 400:
        return MOONSHOT_INVALID_REQUEST, False
    if code == 413:
        return MOONSHOT_CONTEXT_LENGTH, False
    if code >= 500:
        return MOONSHOT_PROVIDER_UNAVAILABLE, True
    return MOONSHOT_UNKNOWN, False


def error_from_http_status(status_code: int) -> MoonshotProviderError:
    category, retryable = classify_http_status(status_code)
    return MoonshotProviderError(category, retryable=retryable, status_code=status_code)
