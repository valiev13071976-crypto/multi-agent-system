"""B2B assistant orchestration — structured actions only."""

from __future__ import annotations

import uuid
from typing import Any

from b2b_commerce.errors import B2B_ACTION_DENIED, B2BCommerceError
from b2b_commerce.platform_models import (
    ACTION_ANSWER,
    ACTION_ASK_CLARIFICATION,
    ACTION_COMPARE_WHOLESALE,
    ACTION_CREATE_QUOTE,
    ACTION_HANDOFF,
    ACTION_SEARCH_PRODUCT,
    ALLOWED_ASSISTANT_ACTIONS,
    SCOPE_CUSTOMER,
    SCOPE_INTERNAL,
    AssistantProposal,
)
from b2b_commerce.pricing import customer_safe_projection


def validate_action(action: str) -> None:
    if action not in ALLOWED_ASSISTANT_ACTIONS:
        raise B2BCommerceError(B2B_ACTION_DENIED, f"unknown action {action}")


def build_customer_context(*, evidence: dict[str, Any]) -> dict[str, Any]:
    return customer_safe_projection(evidence)


def propose_from_message(
    *,
    tenant_id: str,
    conversation_id: str,
    text: str,
    resolved_items: list[dict[str, Any]] | None = None,
    data_scope: str = SCOPE_CUSTOMER,
) -> AssistantProposal:
    lowered = text.lower()
    if "ignore" in lowered and ("discount" in lowered or "supplier" in lowered or "token" in lowered):
        return AssistantProposal(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action=ACTION_ANSWER,
            payload={"text": "I can help with product availability and official quotes using verified catalog data."},
            data_scope=data_scope,
        )
    if resolved_items and any(i.get("match_state") == "AMBIGUOUS" for i in resolved_items):
        return AssistantProposal(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action=ACTION_ASK_CLARIFICATION,
            payload={"candidates": [i.get("candidates", ()) for i in resolved_items]},
            data_scope=data_scope,
        )
    if resolved_items and any(i.get("quantity") is None for i in resolved_items):
        return AssistantProposal(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action=ACTION_ASK_CLARIFICATION,
            payload={"missing": ["quantity"]},
            data_scope=data_scope,
        )
    if "compare" in lowered or "лучш" in lowered:
        return AssistantProposal(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action=ACTION_COMPARE_WHOLESALE,
            payload={},
            data_scope=SCOPE_INTERNAL if data_scope == SCOPE_INTERNAL else SCOPE_CUSTOMER,
        )
    if resolved_items and all(i.get("match_state") == "CONFIRMED" for i in resolved_items):
        return AssistantProposal(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action=ACTION_CREATE_QUOTE,
            payload={"items": resolved_items},
            data_scope=data_scope,
        )
    if "payment" in lowered or "credit" in lowered or "скидк" in lowered:
        return AssistantProposal(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action=ACTION_HANDOFF,
            payload={"reason": "unsupported_commercial_terms"},
            data_scope=data_scope,
        )
    return AssistantProposal(
        proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        action=ACTION_SEARCH_PRODUCT,
        payload={"query": text},
        data_scope=data_scope,
    )
