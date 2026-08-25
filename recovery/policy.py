"""Deterministic recovery orchestration policy (no AI, no mutation)."""

from __future__ import annotations

from recovery.models import (
    ACTION_CANCEL,
    ACTION_DEFER,
    ACTION_MARK_BLOCKED,
    ACTION_MARK_RESOLVED,
    ACTION_RECONCILE_READ_ONLY,
    ACTION_REQUEST_NEW_AUTHORIZATION,
    ACTION_RESUME_WORKFLOW,
    ACTION_ROLLBACK,
    CASE_BUDGET_UNCERTAIN_COST,
    CASE_MANUAL_REVIEW,
    CASE_PERMIT_CONSUMED_BEFORE_MUTATION,
    CASE_UNCERTAIN_SIDE_EFFECT,
    DECISION_BLOCK,
    DECISION_CANCEL,
    DECISION_DEFER,
    DECISION_RECONCILE,
    DECISION_RESUME,
    DECISION_ROLLBACK,
    RECOVERY_POLICY_VERSION,
    RecoveryAction,
    RecoveryCase,
    RecoveryPlan,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_NORMAL,
)
from side_effects.models import (
    RECON_CONFIRMED_FAILED,
    RECON_CONFIRMED_SUCCEEDED,
    RECON_MANUAL_REVIEW,
    RECON_STILL_UNCERTAIN,
)
from tools.models import (
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
)


NEVER_AUTO_TRUST = frozenset(
    {
        TOOL_TRUST_PRIVILEGED,
        TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    }
)


class RecoveryPolicy:
    """Orchestration policy. Complements side_effects.recovery.RecoveryPolicy."""

    policy_version = RECOVERY_POLICY_VERSION

    def classify_severity(
        self,
        *,
        case_type: str,
        tool_trust_level: str = "",
        reversible: bool = False,
    ) -> str:
        if tool_trust_level in NEVER_AUTO_TRUST or case_type in {
            CASE_UNCERTAIN_SIDE_EFFECT,
            CASE_PERMIT_CONSUMED_BEFORE_MUTATION,
            CASE_BUDGET_UNCERTAIN_COST,
        }:
            return SEVERITY_CRITICAL if tool_trust_level in NEVER_AUTO_TRUST else SEVERITY_HIGH
        if case_type == CASE_MANUAL_REVIEW:
            return SEVERITY_HIGH
        if not reversible:
            return SEVERITY_HIGH
        if case_type.endswith("started"):
            return SEVERITY_NORMAL
        return SEVERITY_LOW

    def plan(
        self,
        case: RecoveryCase,
        *,
        reconciliation_status: str | None = None,
        operator_decision: str | None = None,
        workflow_terminal: bool = False,
        supports_authoritative_reconcile: bool = True,
    ) -> RecoveryPlan:
        decision = operator_decision or case.operator_decision

        if decision == DECISION_BLOCK:
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(RecoveryAction(ACTION_MARK_BLOCKED, "operator_block"),),
                reason_code="operator_block",
                waiting_operator=False,
            )
        if decision == DECISION_CANCEL:
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(RecoveryAction(ACTION_CANCEL, "operator_cancel_case_only"),),
                reason_code="operator_cancel",
                waiting_operator=False,
                metadata_safe={"cancel_case_only": True},
            )
        if decision == DECISION_DEFER:
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(RecoveryAction(ACTION_DEFER, "operator_defer"),),
                reason_code="operator_defer",
                waiting_operator=True,
            )
        if decision == DECISION_ROLLBACK:
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(
                    RecoveryAction(
                        ACTION_ROLLBACK,
                        "operator_rollback_requires_authorization",
                        requires_authorization=True,
                        mutates=True,
                    ),
                ),
                reason_code="operator_rollback",
            )
        if decision == DECISION_RESUME:
            if workflow_terminal:
                return RecoveryPlan(
                    recovery_id=case.recovery_id,
                    steps=(RecoveryAction(ACTION_MARK_BLOCKED, "terminal_workflow_resume_denied"),),
                    reason_code="terminal_workflow_resume_denied",
                )
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(
                    RecoveryAction(
                        ACTION_RESUME_WORKFLOW,
                        "operator_resume",
                        requires_authorization=True,
                        mutates=False,
                    ),
                ),
                reason_code="operator_resume",
            )
        if decision == DECISION_RECONCILE:
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(
                    RecoveryAction(ACTION_RECONCILE_READ_ONLY, "operator_reconcile"),
                ),
                reason_code="operator_reconcile",
            )

        # No operator decision — default safe plan
        if case.tool_trust_level in NEVER_AUTO_TRUST and case.case_type in {
            CASE_UNCERTAIN_SIDE_EFFECT,
            CASE_MANUAL_REVIEW,
            CASE_PERMIT_CONSUMED_BEFORE_MUTATION,
        }:
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(),
                reason_code="waiting_operator_high_risk",
                waiting_operator=True,
            )

        if reconciliation_status == RECON_CONFIRMED_SUCCEEDED:
            steps = [RecoveryAction(ACTION_MARK_RESOLVED, "confirmed_succeeded")]
            if not workflow_terminal:
                steps.append(
                    RecoveryAction(
                        ACTION_RESUME_WORKFLOW,
                        "resume_after_confirmed_success",
                        requires_authorization=True,
                    )
                )
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=tuple(steps),
                reason_code="confirmed_succeeded",
            )

        if reconciliation_status == RECON_CONFIRMED_FAILED:
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(
                    RecoveryAction(
                        ACTION_REQUEST_NEW_AUTHORIZATION,
                        "confirmed_failed_requires_new_authorization",
                        requires_authorization=True,
                    ),
                ),
                reason_code="confirmed_failed",
                waiting_operator=True,
            )

        if reconciliation_status in {RECON_STILL_UNCERTAIN, RECON_MANUAL_REVIEW, None}:
            if supports_authoritative_reconcile and case.attempt < case.max_attempts:
                return RecoveryPlan(
                    recovery_id=case.recovery_id,
                    steps=(
                        RecoveryAction(
                            ACTION_RECONCILE_READ_ONLY,
                            "bounded_read_only_reconcile",
                        ),
                    ),
                    reason_code="reconcile_read_only_first",
                )
            return RecoveryPlan(
                recovery_id=case.recovery_id,
                steps=(),
                reason_code="waiting_operator",
                waiting_operator=True,
            )

        return RecoveryPlan(
            recovery_id=case.recovery_id,
            steps=(),
            reason_code="waiting_operator_unclassified",
            waiting_operator=True,
        )
