"""Canonical Mistral error categories — no secrets in messages."""

from __future__ import annotations


MISTRAL_AUTH = "auth"
MISTRAL_RATE_LIMIT = "rate_limit"
MISTRAL_TIMEOUT = "timeout"
MISTRAL_INVALID_REQUEST = "invalid_request"
MISTRAL_CONTEXT_LENGTH = "context_length"
MISTRAL_PROVIDER_UNAVAILABLE = "provider_unavailable"
MISTRAL_MALFORMED_RESPONSE = "malformed_response"
MISTRAL_UNKNOWN = "unknown"
MISTRAL_DISABLED = "disabled"
MISTRAL_MISSING_KEY = "missing_key"
MISTRAL_MISSING_MODEL = "missing_model"
MISTRAL_ENDPOINT_DENIED = "endpoint_denied"


class MistralProviderError(RuntimeError):
    """Normalized Mistral failure. Message is category-only (no key/body leak)."""

    def __init__(self, category: str, *, retryable: bool = False, status_code: int | None = None):
        self.category = category
        self.retryable = bool(retryable)
        self.status_code = status_code
        super().__init__(category)

    def __repr__(self) -> str:
        return (
            f"MistralProviderError(category={self.category!r}, "
            f"retryable={self.retryable}, status_code={self.status_code})"
        )


def classify_http_status(status_code: int) -> tuple[str, bool]:
    """Return (category, retryable). Auth is non-retryable."""

    code = int(status_code)
    if code in {401, 403}:
        return MISTRAL_AUTH, False
    if code == 429:
        return MISTRAL_RATE_LIMIT, True
    if code == 408:
        return MISTRAL_TIMEOUT, True
    if code == 400:
        return MISTRAL_INVALID_REQUEST, False
    if code == 413:
        return MISTRAL_CONTEXT_LENGTH, False
    if code >= 500:
        return MISTRAL_PROVIDER_UNAVAILABLE, True
    return MISTRAL_UNKNOWN, False


def error_from_http_status(status_code: int) -> MistralProviderError:
    category, retryable = classify_http_status(status_code)
    return MistralProviderError(category, retryable=retryable, status_code=status_code)
