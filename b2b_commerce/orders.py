"""Order draft and confirmation binding."""

from __future__ import annotations

import uuid

from b2b_commerce.errors import B2B_ORDER_CONFIRMATION_REQUIRED, B2BCommerceError
from b2b_commerce.platform_models import B2BOrderDraft, CommercialQuoteVersion


def new_draft_id() -> str:
    return f"draft_{uuid.uuid4().hex[:12]}"


def new_confirmation_token() -> str:
    return f"confirm_{uuid.uuid4().hex[:16]}"


def bind_confirmation(*, quote: CommercialQuoteVersion, conversation_id: str, draft_id: str) -> dict:
    return {
        "quote_id": quote.quote_id,
        "quote_version_id": quote.version_id,
        "conversation_id": conversation_id,
        "draft_id": draft_id,
        "operation": "order_submit",
    }


def assert_confirmation_matches(
    payload: dict | None,
    *,
    quote: CommercialQuoteVersion,
    conversation_id: str,
    draft_id: str,
) -> None:
    if not payload:
        raise B2BCommerceError(B2B_ORDER_CONFIRMATION_REQUIRED)
    if payload.get("quote_version_id") != quote.version_id:
        raise B2BCommerceError(B2B_ORDER_CONFIRMATION_REQUIRED, "confirmation replay")
    if payload.get("conversation_id") != conversation_id:
        raise B2BCommerceError(B2B_ORDER_CONFIRMATION_REQUIRED)
    if payload.get("draft_id") != draft_id:
        raise B2BCommerceError(B2B_ORDER_CONFIRMATION_REQUIRED)
