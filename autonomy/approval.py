import uuid
from dataclasses import replace

from autonomy.models import (
    APPROVAL_APPROVED,
    APPROVAL_CANCELLED,
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ApprovalRecord,
    utc_now,
)
from autonomy.store import ApprovalStore, InMemoryApprovalStore


class ApprovalService:
    def __init__(self, store: ApprovalStore | None = None):
        self.store = store or InMemoryApprovalStore()

    def create_pending(
        self,
        *,
        workflow_id: str,
        task_id: str,
        action_id: str,
        decision_id: str,
        approved_by: str = "pending",
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            task_id=task_id,
            action_id=action_id,
            decision_id=decision_id,
            status=APPROVAL_PENDING,
            approved_by=approved_by,
            created_at=utc_now(),
        )
        self.store.put(record)
        return record

    def resolve(
        self,
        approval_id: str,
        status: str,
        *,
        approved_by: str,
        reason_code: str | None = None,
    ) -> ApprovalRecord:
        current = self.store.get(approval_id)
        if current is None:
            raise KeyError(approval_id)
        if status not in {
            APPROVAL_APPROVED,
            APPROVAL_REJECTED,
            APPROVAL_EXPIRED,
            APPROVAL_CANCELLED,
        }:
            raise ValueError(status)
        updated = replace(
            current,
            status=status,
            approved_by=approved_by,
            resolved_by=approved_by,
            resolved_at=utc_now(),
            reason_code=reason_code,
            version=current.version + 1,
        )
        self.store.put(updated)
        return updated

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self.store.get(approval_id)
