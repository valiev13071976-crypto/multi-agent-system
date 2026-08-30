"""Quote versioning and stale detection."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from b2b_commerce.errors import B2B_QUOTE_STALE, B2BCommerceError
from b2b_commerce.platform_models import CommercialQuoteVersion
from b2b_commerce.policy import DEFAULT_QUOTE_VALIDITY_DAYS


def new_quote_version_id(existing: list[CommercialQuoteVersion]) -> str:
    return f"v{len(existing) + 1}"


def mark_quote_stale(quote: CommercialQuoteVersion) -> CommercialQuoteVersion:
    quote.stale = True
    return quote


def assert_quote_fresh(quote: CommercialQuoteVersion) -> None:
    if quote.stale:
        raise B2BCommerceError(B2B_QUOTE_STALE)
    if quote.valid_until:
        expires = datetime.fromisoformat(quote.valid_until)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            raise B2BCommerceError(B2B_QUOTE_STALE)


def default_valid_until() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=DEFAULT_QUOTE_VALIDITY_DAYS)).isoformat()


def quote_send_idempotency_key(*, tenant_id: str, chat_id: str, quote_id: str, version_id: str) -> str:
    return f"{tenant_id}:{chat_id}:{quote_id}:{version_id}:send"
