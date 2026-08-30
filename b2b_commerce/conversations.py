"""Conversation state machine."""

from __future__ import annotations

import re
import uuid

from b2b_commerce.platform_models import (
    CONV_AWAITING_CONFIRMATION,
    CONV_HUMAN_HANDOFF,
    CONV_ORDER_DRAFTED,
    CONV_PRODUCT_SEARCH,
    CONV_QUALIFYING,
    CONV_QUANTITY_REQUIRED,
    CONV_QUOTE_PREPARATION,
    CONV_QUOTE_READY,
    B2BConversation,
    B2BConversationMessage,
    B2BInquiry,
    B2BInquiryItem,
    MATCH_UNMATCHED,
)


def new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"


def new_inquiry_id() -> str:
    return f"inq_{uuid.uuid4().hex[:12]}"


_QTY_RE = re.compile(r"(\d+)\s*(?:шт|единиц|units|pcs)?", re.I)


def extract_inquiry_items(text: str, *, tenant_id: str, inquiry_id: str) -> list[B2BInquiryItem]:
    quantity = None
    m = _QTY_RE.search(text)
    if m:
        quantity = int(m.group(1))
    product_query = text.strip()
    for token in ("нужно", "need", "хочу", "want"):
        product_query = re.sub(rf"\b{token}\b", "", product_query, flags=re.I).strip()
    if m:
        product_query = _QTY_RE.sub("", product_query).strip(" ,.")
    return [
        B2BInquiryItem(
            item_id=f"item_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            inquiry_id=inquiry_id,
            product_query=product_query or text,
            quantity=quantity,
            match_state=MATCH_UNMATCHED,
        )
    ]


def transition_after_inquiry(conversation: B2BConversation, items: list[B2BInquiryItem]) -> str:
    if any(i.quantity is None for i in items):
        return CONV_QUANTITY_REQUIRED
    if any(i.match_state in {"AMBIGUOUS", "UNMATCHED"} for i in items):
        return CONV_PRODUCT_SEARCH
    return CONV_QUOTE_PREPARATION


def record_inbound_message(
    *,
    tenant_id: str,
    conversation_id: str,
    text: str,
) -> B2BConversationMessage:
    return B2BConversationMessage(
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        direction="inbound",
        text=text,
    )
