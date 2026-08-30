from dataclasses import replace
from datetime import datetime

from autonomy.models import utc_now
from hitl.errors import (
    ExecutionPermitConsumedError,
    ExecutionPermitExpiredError,
    ExecutionPermitMismatchError,
    ExecutionPermitNotFoundError,
    ExecutionPermitRevokedError,
)
from hitl.models import (
    PERMIT_CONSUMED,
    PERMIT_EXPIRED,
    PERMIT_ISSUED,
    PERMIT_REVOKED,
    ExecutionPermit,
    action_fingerprint,
)
from hitl.store import ExecutionPermitStore, InMemoryExecutionPermitStore


class PermitService:
    def __init__(self, store: ExecutionPermitStore | None = None):
        self.store = store or InMemoryExecutionPermitStore()
        self.observability = None

    def _obs_emit(self, event_type: str, permit, **kwargs):
        from observability.helpers import safe_emit

        if self.observability is None:
            return
        parent = self.observability.context_for_workflow(permit.workflow_id)
        span = (
            self.observability.child_span(parent)
            if parent is not None
            else self.observability.create_context(
                workflow_id=permit.workflow_id, task_id=permit.task_id
            )
        )
        safe_emit(
            self.observability,
            event_type,
            context=span,
            component="permit",
            metadata={
                "permit_id": permit.permit_id,
                "action_id": permit.action_id,
            },
            **kwargs,
        )

    def get(self, permit_id: str) -> ExecutionPermit:
        permit = self.store.get(permit_id)
        if permit is None:
            raise ExecutionPermitNotFoundError(permit_id)
        return permit

    def validate(
        self,
        permit: ExecutionPermit,
        *,
        action=None,
        now: datetime | None = None,
    ) -> ExecutionPermit:
        stamp = now or utc_now()
        if permit.status == PERMIT_REVOKED:
            raise ExecutionPermitRevokedError()
        if permit.status == PERMIT_CONSUMED:
            raise ExecutionPermitConsumedError()
        if permit.status == PERMIT_EXPIRED or permit.expires_at <= stamp:
            if permit.status == PERMIT_ISSUED:
                expired = replace(
                    permit,
                    status=PERMIT_EXPIRED,
                    version=int(permit.version) + 1,
                )
                self.store.save(expired)
            self._obs_emit("permit.expired", permit, status="expired", error_code="permit_expired")
            raise ExecutionPermitExpiredError()
        if permit.status != PERMIT_ISSUED:
            self._obs_emit("permit.denied", permit, status="denied", error_code="permit_not_issued")
            raise ExecutionPermitMismatchError("permit_not_issued")
        if action is not None:
            if permit.action_id != action.action_id:
                raise ExecutionPermitMismatchError("permit_action_mismatch")
            if permit.workflow_id != action.workflow_id:
                raise ExecutionPermitMismatchError("permit_workflow_mismatch")
            if permit.task_id != action.task_id:
                raise ExecutionPermitMismatchError("permit_task_mismatch")
            if permit.action_fingerprint != action_fingerprint(action):
                raise ExecutionPermitMismatchError("permit_fingerprint_mismatch")
            if permit.idempotency_key != action.idempotency_key:
                raise ExecutionPermitMismatchError("permit_idempotency_mismatch")
            permit_tenant = str(getattr(permit, "tenant_id", "") or "")
            action_tenant = str(getattr(action, "tenant_id", "") or "")
            permit_actor = str(getattr(permit, "actor_ref", "") or "")
            action_actor = str(getattr(action, "actor_ref", "") or "")
            # Permit-bound tenant/actor must match. Empty permit fields stay
            # legacy-compatible (workflow/task checks above still apply).
            if permit_tenant and permit_tenant != action_tenant:
                raise ExecutionPermitMismatchError("permit_tenant_mismatch")
            if permit_actor and permit_actor != action_actor:
                raise ExecutionPermitMismatchError("permit_actor_mismatch")
        return permit

    def consume_for_execution(
        self,
        permit_id: str,
        *,
        action=None,
        now: datetime | None = None,
    ) -> ExecutionPermit:
        stamp = now or utc_now()
        permit = self.validate(self.get(permit_id), action=action, now=stamp)
        consumed = replace(
            permit,
            status=PERMIT_CONSUMED,
            consumed_at=stamp,
            version=int(permit.version) + 1,
        )
        self.store.save(consumed)
        return consumed

    def revoke(self, permit_id: str) -> ExecutionPermit:
        permit = self.get(permit_id)
        revoked = replace(
            permit,
            status=PERMIT_REVOKED,
            version=int(permit.version) + 1,
        )
        self.store.save(revoked)
        return revoked
