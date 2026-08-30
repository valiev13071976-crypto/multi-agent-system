"""Normalized production provider error taxonomy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProviderErrorCategory(str, Enum):
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    WEBHOOK_VERIFICATION_FAILED = "WEBHOOK_VERIFICATION_FAILED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


@dataclass
class ProductionProviderError(Exception):
    category: ProviderErrorCategory
    message: str = ""
    provider_id: str = ""
    retryable: bool = False
    retry_after_seconds: float | None = None
    metadata: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.category.value}:{self.provider_id}:{self.message}"


def classify_http_status(status: int) -> ProviderErrorCategory:
    if status in {401, 403}:
        return ProviderErrorCategory.AUTHENTICATION_FAILED if status == 401 else ProviderErrorCategory.AUTHORIZATION_FAILED
    if status == 429:
        return ProviderErrorCategory.RATE_LIMITED
    if status in {408, 504}:
        return ProviderErrorCategory.TIMEOUT
    if 400 <= status < 500:
        return ProviderErrorCategory.BAD_REQUEST
    if status >= 500:
        return ProviderErrorCategory.PROVIDER_UNAVAILABLE
    return ProviderErrorCategory.PROVIDER_ERROR
