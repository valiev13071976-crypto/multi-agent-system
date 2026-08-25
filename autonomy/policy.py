from autonomy.models import (
    ACTION_DELETE,
    ACTION_EXECUTE_CODE,
    ACTION_EXTERNAL_PUBLISH,
    ACTION_FINANCIAL_CHANGE,
    ACTION_PERMISSION_CHANGE,
    ACTION_PURCHASE,
    ACTION_READ,
    ACTION_SEND_MESSAGE,
    ACTION_TYPES,
    ACTION_WRITE,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    DECISION_REVIEW_AFTER,
    LEVEL_ADVISOR,
    LEVEL_ANALYST,
    LEVEL_EXECUTOR_BOUNDED,
    LEVEL_EXECUTOR_CONFIRMED,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_TYPES,
    APPROVAL_APPROVED,
    APPROVAL_CANCELLED,
    APPROVAL_EXPIRED,
    APPROVAL_REJECTED,
)
from tools.models import (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_LEVELS,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
)


NEVER_AUTO_ALLOW_TYPES = frozenset(
    {
        ACTION_PURCHASE,
        ACTION_FINANCIAL_CHANGE,
        ACTION_SEND_MESSAGE,
        ACTION_EXTERNAL_PUBLISH,
        ACTION_PERMISSION_CHANGE,
        ACTION_DELETE,
        ACTION_EXECUTE_CODE,
    }
)


class ApprovalPolicy:
    """Deterministic autonomy policy. No LLM. Deny-by-default."""

    def decide(self, ctx: dict) -> tuple[str, str]:
        action_type = ctx["action_type"]
        level = ctx["autonomy_level"]
        risk = ctx["risk_class"]
        trust = ctx["tool_trust_level"]
        reversible = bool(ctx.get("reversible", False))
        approval_status = ctx.get("approval_status")
        approved = approval_status == APPROVAL_APPROVED

        if action_type not in ACTION_TYPES:
            return DECISION_DENY, "unknown_action_type"
        if trust not in TOOL_TRUST_LEVELS:
            return DECISION_DENY, "unknown_tool_trust"
        if ctx.get("unknown_capability_requirement"):
            return DECISION_DENY, "unknown_capability_requirement"
        if ctx.get("token_reason"):
            return DECISION_DENY, ctx["token_reason"]
        if not ctx.get("capabilities_valid"):
            return DECISION_DENY, ctx.get("capability_reason") or "capability_missing"
        if not ctx.get("scope_valid", True):
            return DECISION_DENY, ctx.get("scope_reason") or "scope_mismatch"
        if approval_status in {
            APPROVAL_REJECTED,
            APPROVAL_EXPIRED,
            APPROVAL_CANCELLED,
        }:
            return DECISION_DENY, f"approval_{approval_status}"
        if action_type in SIDE_EFFECT_TYPES and not ctx.get("idempotency_ready"):
            return DECISION_DENY, "idempotency_required"
        if ctx.get("idempotency_conflict"):
            return DECISION_DENY, ctx.get("idempotency_reason") or "duplicate_execution"
        if level not in {
            LEVEL_ADVISOR,
            LEVEL_ANALYST,
            LEVEL_EXECUTOR_CONFIRMED,
            LEVEL_EXECUTOR_BOUNDED,
        }:
            return DECISION_DENY, "unknown_autonomy_level"

        if action_type in SIDE_EFFECT_TYPES and level == LEVEL_ADVISOR:
            return DECISION_DENY, "advisor_side_effect_denied"
        if action_type in SIDE_EFFECT_TYPES and level == LEVEL_ANALYST:
            return DECISION_DENY, "analyst_write_denied"

        if action_type == ACTION_DELETE and not reversible:
            if trust != TOOL_TRUST_PRIVILEGED:
                return DECISION_DENY, "irreversible_delete_denied"
        if ctx.get("unknown_destructive"):
            return DECISION_DENY, "unknown_destructive_denied"

        def after_approval_or_require(reason: str) -> tuple[str, str]:
            if approved:
                return DECISION_ALLOW, "approved_reevaluated"
            return DECISION_REQUIRE_APPROVAL, reason

        if action_type in NEVER_AUTO_ALLOW_TYPES:
            return after_approval_or_require("require_approval")

        if trust == TOOL_TRUST_PRIVILEGED or risk == RISK_CRITICAL:
            return after_approval_or_require("privileged_or_critical")

        if trust == TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE:
            return after_approval_or_require("irreversible_write")

        if action_type == ACTION_READ:
            if risk != RISK_LOW:
                return DECISION_DENY, "read_not_low"
            if trust in {TOOL_TRUST_INTERNAL_SAFE, TOOL_TRUST_READ_ONLY_EXTERNAL}:
                return DECISION_ALLOW, "read_allowed"
            return DECISION_DENY, "read_trust_denied"

        if action_type == ACTION_WRITE:
            if level == LEVEL_EXECUTOR_CONFIRMED:
                return after_approval_or_require("executor_confirmed_write")
            if level == LEVEL_EXECUTOR_BOUNDED:
                if risk in {RISK_HIGH, RISK_CRITICAL}:
                    return after_approval_or_require("bounded_high_risk")
                if (
                    risk in {RISK_LOW, RISK_MEDIUM}
                    and reversible
                    and trust
                    in {
                        TOOL_TRUST_INTERNAL_SAFE,
                        TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                    }
                    and ctx.get("idempotency_ready")
                ):
                    if trust == TOOL_TRUST_INTERNAL_SAFE and risk == RISK_LOW:
                        return DECISION_ALLOW, "bounded_low_internal"
                    return DECISION_REVIEW_AFTER, "bounded_reversible_write"
                return after_approval_or_require("bounded_write_review")
            return DECISION_DENY, "write_denied"

        return DECISION_DENY, "deny_by_default"
