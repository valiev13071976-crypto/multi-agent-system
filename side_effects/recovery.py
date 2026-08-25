import uuid

from hitl.models import PERMIT_CONSUMED, PERMIT_ISSUED

from side_effects.errors import SideEffectAuthorizationError
from side_effects.models import (
    ADAPTER_RECON_FAILED,
    ADAPTER_RECON_NOT_FOUND,
    ADAPTER_RECON_SUCCEEDED,
    ADAPTER_RECON_UNKNOWN,
    DECISION_DENY_RETRY,
    DECISION_MANUAL_REVIEW_REQUIRED,
    DECISION_MARK_COMPLETED,
    DECISION_MARK_FAILED,
    DECISION_NO_ACTION,
    DECISION_REAUTHORIZATION_REQUIRED,
    DECISION_ROLLBACK_CANDIDATE,
    RecoveryLineage,
    RecoveryWorkflowReference,
)


class RecoveryPolicy:
    """Recovery decisions only. Never executes adapters or resurrects permits."""

    def decide(
        self,
        *,
        lookup_status: str | None,
        authoritative: bool,
        not_found_is_failure: bool,
        reversible: bool,
        supports_idempotency: bool,
        attempts: int,
        max_attempts: int,
        supports_reconciliation: bool,
    ) -> dict:
        if not supports_reconciliation:
            return {
                "decision": DECISION_MANUAL_REVIEW_REQUIRED,
                "retry_eligible": False,
                "reauthorization_required": True,
                "rollback_candidate": False,
                "manual_review_required": True,
                "reason_code": "reconciliation_unsupported",
            }
        if lookup_status == ADAPTER_RECON_SUCCEEDED:
            if not authoritative:
                return {
                    "decision": DECISION_MANUAL_REVIEW_REQUIRED,
                    "retry_eligible": False,
                    "reauthorization_required": True,
                    "rollback_candidate": False,
                    "manual_review_required": True,
                    "reason_code": "non_authoritative_success",
                }
            return {
                "decision": (
                    DECISION_ROLLBACK_CANDIDATE if reversible else DECISION_MARK_COMPLETED
                ),
                "retry_eligible": False,
                "reauthorization_required": True,
                "rollback_candidate": bool(reversible),
                "manual_review_required": False,
                "reason_code": "confirmed_succeeded",
            }
        if lookup_status == ADAPTER_RECON_FAILED:
            if not authoritative:
                return self._unknown(attempts, max_attempts, "non_authoritative_failure")
            return self._confirmed_failure(supports_idempotency)
        if lookup_status == ADAPTER_RECON_NOT_FOUND:
            if not_found_is_failure and authoritative:
                return self._confirmed_failure(supports_idempotency)
            return self._unknown(attempts, max_attempts, "not_found_not_authoritative")
        if lookup_status == ADAPTER_RECON_UNKNOWN or lookup_status is None:
            return self._unknown(attempts, max_attempts, "lookup_unknown")
        return self._unknown(attempts, max_attempts, "lookup_unrecognized")

    def _confirmed_failure(self, supports_idempotency: bool) -> dict:
        return {
            "decision": DECISION_REAUTHORIZATION_REQUIRED,
            "retry_eligible": bool(supports_idempotency),
            "reauthorization_required": True,
            "rollback_candidate": False,
            "manual_review_required": False,
            "reason_code": "confirmed_failed",
        }

    def _unknown(self, attempts: int, max_attempts: int, reason: str) -> dict:
        if attempts >= max_attempts:
            return {
                "decision": DECISION_MANUAL_REVIEW_REQUIRED,
                "retry_eligible": False,
                "reauthorization_required": True,
                "rollback_candidate": False,
                "manual_review_required": True,
                "reason_code": "max_reconciliation_attempts",
            }
        return {
            "decision": DECISION_NO_ACTION,
            "retry_eligible": False,
            "reauthorization_required": True,
            "rollback_candidate": False,
            "manual_review_required": False,
            "reason_code": reason,
        }

    def require_fresh_authorization(self, *, old_permit=None, permit=None) -> None:
        if old_permit is not None:
            raise SideEffectAuthorizationError("old_permit_cannot_be_reused")
        if permit is not None and getattr(permit, "status", None) == PERMIT_CONSUMED:
            raise SideEffectAuthorizationError("old_permit_cannot_be_reused")
        if permit is not None and getattr(permit, "status", None) != PERMIT_ISSUED:
            raise SideEffectAuthorizationError("recovery_permit_not_issued")

    def lineage(self, original_execution_id: str, reconciliation_id: str | None, attempt: int) -> RecoveryLineage:
        return RecoveryLineage(
            recovery_id=str(uuid.uuid4()),
            original_execution_id=original_execution_id,
            recovery_attempt=attempt,
            parent_execution_id=original_execution_id,
            reconciliation_id=reconciliation_id,
        )

    def workflow_reference(self, original_workflow_id: str, reason: str) -> RecoveryWorkflowReference:
        return RecoveryWorkflowReference(
            original_workflow_id=original_workflow_id,
            recovery_workflow_id=None,
            reason=reason,
        )
