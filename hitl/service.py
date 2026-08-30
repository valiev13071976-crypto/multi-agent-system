import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from autonomy.models import (
    APPROVAL_APPROVED,
    APPROVAL_CANCELLED,
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    DECISION_REVIEW_AFTER,
    IDEMPOTENCY_COMPLETED,
    ApprovalRecord,
    utc_now,
)
from hitl.audit import HITLAuditLog
from hitl.authority import ApprovalAuthority, InMemoryApprovalAuthority
from hitl.errors import (
    ActionIntegrityError,
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalInvalidStateError,
    ApprovalNotFoundError,
    ApprovalSelfApprovalError,
    ApprovalUnauthorizedResolverError,
)
from hitl.models import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    DEFAULT_PERMIT_TTL_SECONDS,
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_CANCELLED,
    EVENT_APPROVAL_EXPIRED,
    EVENT_APPROVAL_REJECTED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_PERMIT_CONSUMED,
    EVENT_PERMIT_ISSUED,
    EVENT_PERMIT_REVOKED,
    EVENT_REEVALUATION_FAILED,
    EVENT_REEVALUATION_PASSED,
    ExecutionPermit,
    action_fingerprint,
    approval_class_for,
    class_sufficient,
)
from hitl.permit import PermitService
from workflow.models import STATUS_WAITING_APPROVAL, TERMINAL_STATUSES


def _ttl_seconds(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw or not str(raw).strip():
        return default
    return int(raw)


class HITLService:
    """Single owner of approval lifecycle. Does not execute side effects."""

    def __init__(
        self,
        *,
        gate,
        state_manager=None,
        store=None,
        authority: ApprovalAuthority | None = None,
        permits: PermitService | None = None,
        audit: HITLAuditLog | None = None,
        approval_ttl_seconds: int | None = None,
        permit_ttl_seconds: int | None = None,
    ):
        self.gate = gate
        self.state_manager = state_manager
        self.store = store or gate.approvals.store
        self.authority = authority or InMemoryApprovalAuthority()
        self.permits = permits or PermitService()
        self.audit = audit or HITLAuditLog()
        self.observability = None
        self.approval_ttl_seconds = (
            DEFAULT_APPROVAL_TTL_SECONDS
            if approval_ttl_seconds is None
            else approval_ttl_seconds
        )
        if approval_ttl_seconds is None:
            self.approval_ttl_seconds = _ttl_seconds(
                "HITL_DEFAULT_APPROVAL_TTL_SECONDS", DEFAULT_APPROVAL_TTL_SECONDS
            )
        self.permit_ttl_seconds = (
            permit_ttl_seconds
            if permit_ttl_seconds is not None
            else _ttl_seconds(
                "HITL_EXECUTION_PERMIT_TTL_SECONDS", DEFAULT_PERMIT_TTL_SECONDS
            )
        )
        self.last_permit: ExecutionPermit | None = None
        self.last_reevaluation = None

    def _obs_emit(self, event_type: str, *, workflow_id="", task_id="", component="hitl", **kwargs):
        from observability.helpers import safe_emit

        obs = self.observability
        if obs is None:
            return
        parent = obs.context_for_workflow(workflow_id) if workflow_id else None
        span = obs.child_span(parent) if parent is not None else obs.create_context(
            workflow_id=workflow_id, task_id=task_id
        )
        safe_emit(
            obs,
            event_type,
            context=span,
            component=component,
            **kwargs,
        )

    def get(self, approval_id: str) -> ApprovalRecord:
        record = self.store.get(approval_id)
        if record is None:
            raise ApprovalNotFoundError(approval_id)
        return record

    def request_approval(
        self,
        action,
        decision,
        *,
        requested_by: str,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        if decision.decision == DECISION_ALLOW:
            raise ApprovalInvalidStateError("allow_not_approvable")
        if decision.decision == DECISION_DENY:
            raise ApprovalInvalidStateError("deny_not_approvable")
        if decision.decision != DECISION_REQUIRE_APPROVAL:
            raise ApprovalInvalidStateError("decision_not_require_approval")
        if decision.action_id != action.action_id:
            raise ApprovalInvalidStateError("decision_action_mismatch")
        existing = self.store.find_pending_by_action(action.action_id)
        if existing is not None:
            if existing.decision_id == decision.decision_id:
                return existing
            return existing
        stamp = now or utc_now()
        expires = None
        if self.approval_ttl_seconds:
            expires = stamp + timedelta(seconds=int(self.approval_ttl_seconds))
        fingerprint = action_fingerprint(action)
        record = ApprovalRecord(
            approval_id=str(uuid.uuid4()),
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            action_id=action.action_id,
            decision_id=decision.decision_id,
            status=APPROVAL_PENDING,
            approved_by="pending",
            created_at=stamp,
            approval_class=approval_class_for(action),
            requested_by=str(requested_by or ""),
            requested_at=stamp,
            expires_at=expires,
            version=1,
            action_fingerprint=fingerprint,
            metadata={"tool_id": action.tool_id, "operation": action.operation},
        )
        self.store.create(record)
        self._move_waiting(action.workflow_id)
        self._checkpoint(action, decision, record)
        self.audit.record(
            EVENT_APPROVAL_REQUESTED,
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            action_id=action.action_id,
            approval_id=record.approval_id,
            actor_id=requested_by,
            reason_code="require_approval",
            metadata={"approval_class": record.approval_class},
        )
        self._obs_emit(
            "hitl.requested",
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            status="requested",
            metadata={
                "approval_id": record.approval_id,
                "action_id": action.action_id,
            },
        )
        return record

    def approve(
        self,
        approval_id: str,
        *,
        resolved_by: str,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        record = self._require_pending(approval_id, expected_version, now=now)
        if not str(resolved_by or "").strip():
            raise ApprovalUnauthorizedResolverError()
        if not self.authority.can_resolve(resolved_by, record.approval_class):
            raise ApprovalUnauthorizedResolverError()
        if record.requested_by and record.requested_by == resolved_by:
            raise ApprovalSelfApprovalError()
        stamp = now or utc_now()
        if record.expires_at is not None and record.expires_at <= stamp:
            self.expire(approval_id, now=stamp, expected_version=record.version)
            raise ApprovalExpiredError()
        updated = replace(
            record,
            status=APPROVAL_APPROVED,
            approved_by=resolved_by,
            resolved_by=resolved_by,
            resolved_at=stamp,
            version=record.version + 1,
            reason_code="approved",
        )
        self.store.save(updated)
        self.audit.record(
            EVENT_APPROVAL_APPROVED,
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            action_id=updated.action_id,
            approval_id=updated.approval_id,
            actor_id=resolved_by,
            reason_code="approved",
        )
        self._obs_emit(
            "hitl.approved",
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            status="approved",
            metadata={
                "approval_id": updated.approval_id,
                "action_id": updated.action_id,
            },
        )
        return updated

    def reject(
        self,
        approval_id: str,
        *,
        resolved_by: str,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        record = self._require_pending(approval_id, expected_version, now=now)
        if not self.authority.can_resolve(resolved_by, record.approval_class):
            raise ApprovalUnauthorizedResolverError()
        updated = self._close(
            record,
            APPROVAL_REJECTED,
            resolved_by=resolved_by,
            reason_code="approval_rejected",
            now=now,
        )
        self._fail_workflow(updated.workflow_id, "approval_rejected")
        self.audit.record(
            EVENT_APPROVAL_REJECTED,
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            action_id=updated.action_id,
            approval_id=updated.approval_id,
            actor_id=resolved_by,
            reason_code="approval_rejected",
        )
        self._obs_emit(
            "hitl.rejected",
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            status="rejected",
            metadata={
                "approval_id": updated.approval_id,
                "action_id": updated.action_id,
            },
        )
        return updated

    def expire(
        self,
        approval_id: str,
        *,
        now: datetime | None = None,
        expected_version: int | None = None,
        resolved_by: str = "system",
    ) -> ApprovalRecord:
        record = self.get(approval_id)
        if record.status != APPROVAL_PENDING:
            raise ApprovalInvalidStateError("approval_not_pending")
        if expected_version is not None and record.version != expected_version:
            raise ApprovalConflictError()
        updated = self._close(
            record,
            APPROVAL_EXPIRED,
            resolved_by=resolved_by,
            reason_code="approval_expired",
            now=now,
        )
        self._fail_workflow(updated.workflow_id, "approval_expired")
        self.audit.record(
            EVENT_APPROVAL_EXPIRED,
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            action_id=updated.action_id,
            approval_id=updated.approval_id,
            actor_id=resolved_by,
            reason_code="approval_expired",
        )
        self._obs_emit(
            "hitl.expired",
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            status="expired",
            metadata={
                "approval_id": updated.approval_id,
                "action_id": updated.action_id,
            },
        )
        return updated

    def cancel(
        self,
        approval_id: str,
        *,
        resolved_by: str,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        record = self._require_pending(approval_id, expected_version, now=now)
        updated = self._close(
            record,
            APPROVAL_CANCELLED,
            resolved_by=resolved_by,
            reason_code="approval_cancelled",
            now=now,
        )
        self._cancel_workflow(updated.workflow_id)
        self.audit.record(
            EVENT_APPROVAL_CANCELLED,
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            action_id=updated.action_id,
            approval_id=updated.approval_id,
            actor_id=resolved_by,
            reason_code="approval_cancelled",
        )
        self._obs_emit(
            "hitl.cancelled",
            workflow_id=updated.workflow_id,
            task_id=updated.task_id,
            status="cancelled",
            metadata={
                "approval_id": updated.approval_id,
                "action_id": updated.action_id,
            },
        )
        return updated

    def expire_due(self, *, now: datetime | None = None) -> tuple[ApprovalRecord, ...]:
        stamp = now or utc_now()
        expired = []
        for record in self.store.list_pending():
            if record.expires_at is not None and record.expires_at <= stamp:
                expired.append(self.expire(record.approval_id, now=stamp))
        return tuple(expired)

    def reevaluate_and_issue_permit(
        self,
        approval_id: str,
        action,
        **evaluate_kwargs,
    ) -> ExecutionPermit | None:
        record = self.get(approval_id)
        if record.status != APPROVAL_APPROVED:
            raise ApprovalInvalidStateError("approval_not_approved")
        required_class = approval_class_for(action)
        if not class_sufficient(record.approval_class, required_class):
            self.last_reevaluation = self.gate.evaluate(
                action, approval=record, **evaluate_kwargs
            )
            self.audit.record(
                EVENT_REEVALUATION_FAILED,
                workflow_id=action.workflow_id,
                task_id=action.task_id,
                action_id=action.action_id,
                approval_id=record.approval_id,
                reason_code="approval_class_insufficient",
            )
            return None
        if action_fingerprint(action) != record.action_fingerprint:
            self.audit.record(
                EVENT_REEVALUATION_FAILED,
                workflow_id=action.workflow_id,
                task_id=action.task_id,
                action_id=action.action_id,
                approval_id=record.approval_id,
                reason_code="action_changed_after_approval",
            )
            raise ActionIntegrityError()
        if action.idempotency_key:
            existing = self.gate.idempotency.get(action.idempotency_key)
            if existing is not None and existing.state == IDEMPOTENCY_COMPLETED:
                self.audit.record(
                    EVENT_REEVALUATION_FAILED,
                    workflow_id=action.workflow_id,
                    task_id=action.task_id,
                    action_id=action.action_id,
                    approval_id=record.approval_id,
                    reason_code="duplicate_completed",
                )
                return None
        decision = self.gate.evaluate(action, approval=record, **evaluate_kwargs)
        self.last_reevaluation = decision
        if decision.decision not in {DECISION_ALLOW, DECISION_REVIEW_AFTER}:
            self.audit.record(
                EVENT_REEVALUATION_FAILED,
                workflow_id=action.workflow_id,
                task_id=action.task_id,
                action_id=action.action_id,
                approval_id=record.approval_id,
                reason_code=decision.reason_code,
            )
            return None
        existing_permit = self.permits.store.find_active_by_approval(record.approval_id)
        if existing_permit is not None:
            self.last_permit = existing_permit
            return existing_permit
        stamp = evaluate_kwargs.get("now") or utc_now()
        action_tenant = str(getattr(action, "tenant_id", "") or "")
        action_actor = str(getattr(action, "actor_ref", "") or "")
        permit = ExecutionPermit(
            permit_id=str(uuid.uuid4()),
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            action_id=action.action_id,
            approval_id=record.approval_id,
            decision_id=decision.decision_id,
            action_fingerprint=record.action_fingerprint,
            issued_at=stamp,
            expires_at=stamp + timedelta(seconds=int(self.permit_ttl_seconds)),
            capabilities=decision.capabilities_checked,
            tool_id=action.tool_id,
            operation=action.operation,
            idempotency_key=action.idempotency_key,
            tenant_id=action_tenant,
            actor_ref=action_actor,
            metadata={
                "approval_class": record.approval_class,
                # Persist ownership in metadata for durable stores without columns.
                "tenant_id": action_tenant,
                "actor_ref": action_actor,
            },
        )
        self.permits.store.create(permit)
        self.last_permit = permit
        self.audit.record(
            EVENT_REEVALUATION_PASSED,
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            action_id=action.action_id,
            approval_id=record.approval_id,
            permit_id=permit.permit_id,
            reason_code=decision.reason_code,
        )
        self.audit.record(
            EVENT_PERMIT_ISSUED,
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            action_id=action.action_id,
            approval_id=record.approval_id,
            permit_id=permit.permit_id,
            reason_code="permit_issued",
            metadata={"permit_id": permit.permit_id},
        )
        self._obs_emit(
            "permit.issued",
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            component="permit",
            status="issued",
            metadata={
                "permit_id": permit.permit_id,
                "approval_id": record.approval_id,
                "action_id": action.action_id,
            },
        )
        if (
            self.state_manager is not None
            and self.state_manager.get(action.workflow_id).status == STATUS_WAITING_APPROVAL
        ):
            self.state_manager.approve(action.workflow_id)
        return permit

    def consume_for_execution(self, permit_id: str, *, action=None, now=None):
        consumed = self.permits.consume_for_execution(
            permit_id, action=action, now=now
        )
        self.audit.record(
            EVENT_PERMIT_CONSUMED,
            workflow_id=consumed.workflow_id,
            task_id=consumed.task_id,
            action_id=consumed.action_id,
            approval_id=consumed.approval_id,
            permit_id=consumed.permit_id,
            reason_code="permit_consumed",
        )
        self._obs_emit(
            "permit.consumed",
            workflow_id=consumed.workflow_id,
            task_id=consumed.task_id,
            component="permit",
            status="consumed",
            metadata={
                "permit_id": consumed.permit_id,
                "approval_id": consumed.approval_id,
                "action_id": consumed.action_id,
            },
        )
        return consumed

    def revoke_permit(self, permit_id: str):
        revoked = self.permits.revoke(permit_id)
        self.audit.record(
            EVENT_PERMIT_REVOKED,
            workflow_id=revoked.workflow_id,
            task_id=revoked.task_id,
            action_id=revoked.action_id,
            approval_id=revoked.approval_id,
            permit_id=revoked.permit_id,
            reason_code="permit_revoked",
        )
        return revoked

    def _require_pending(
        self,
        approval_id: str,
        expected_version: int | None,
        *,
        now: datetime | None,
    ) -> ApprovalRecord:
        record = self.get(approval_id)
        if expected_version is not None and record.version != expected_version:
            raise ApprovalConflictError()
        if record.status != APPROVAL_PENDING:
            raise ApprovalInvalidStateError("approval_not_pending")
        stamp = now or utc_now()
        if record.expires_at is not None and record.expires_at <= stamp:
            if record.status == APPROVAL_PENDING:
                self.expire(approval_id, now=stamp, expected_version=record.version)
            raise ApprovalExpiredError()
        return self.get(approval_id)

    def _close(self, record, status, *, resolved_by, reason_code, now):
        stamp = now or utc_now()
        updated = replace(
            record,
            status=status,
            approved_by=resolved_by,
            resolved_by=resolved_by,
            resolved_at=stamp,
            reason_code=reason_code,
            version=record.version + 1,
        )
        self.store.save(updated)
        return updated

    def _move_waiting(self, workflow_id: str) -> None:
        if self.state_manager is None:
            return
        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return
        if state.status != STATUS_WAITING_APPROVAL:
            self.state_manager.wait_for_approval(workflow_id)

    def _checkpoint(self, action, decision, record) -> None:
        if self.state_manager is None:
            return
        self.state_manager.checkpoint(
            action.workflow_id,
            extra_payload={
                "action_id": action.action_id,
                "decision_id": decision.decision_id,
                "required_approval": True,
                "approval_id": record.approval_id,
                "action_fingerprint": record.action_fingerprint,
                "approval_class": record.approval_class,
            },
        )

    def _fail_workflow(self, workflow_id: str, error_code: str) -> None:
        if self.state_manager is None:
            return
        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return
        self.state_manager.fail_workflow(workflow_id, error_code)

    def _cancel_workflow(self, workflow_id: str) -> None:
        if self.state_manager is None:
            return
        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return
        self.state_manager.cancel(workflow_id)
