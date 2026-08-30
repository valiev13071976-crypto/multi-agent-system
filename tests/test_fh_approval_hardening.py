"""FH.12 — approval / permit tenant·actor·action binding hardening."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from autonomy.gate import build_proposed_action
from hitl.errors import ExecutionPermitExpiredError, ExecutionPermitMismatchError
from hitl.models import PERMIT_ISSUED, ExecutionPermit, action_fingerprint
from hitl.permit import PermitService
from hitl.store import InMemoryExecutionPermitStore
from tools.models import TOOL_TRUST_INTERNAL_SAFE


T0 = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _action(**kw):
    fields = dict(
        action_type="write",
        workflow_id="wf-1",
        task_id="task-1",
        tool_id="test.tool",
        operation="set_value",
        resource="r",
        idempotency_key="idem-1",
        tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
        risk_class="low",
        tenant_id="tenant-a",
        actor_ref="tenant-a:user-a",
    )
    fields.update(kw)
    return build_proposed_action(**fields)


def _permit(action, **kw):
    base = dict(
        permit_id="p1",
        workflow_id=action.workflow_id,
        task_id=action.task_id,
        action_id=action.action_id,
        approval_id="appr-1",
        decision_id="dec-1",
        action_fingerprint=action_fingerprint(action),
        issued_at=T0,
        expires_at=T0 + timedelta(minutes=5),
        capabilities=("external_write",),
        tool_id=action.tool_id,
        operation=action.operation,
        idempotency_key=action.idempotency_key,
        status=PERMIT_ISSUED,
        tenant_id=action.tenant_id,
        actor_ref=action.actor_ref,
    )
    base.update(kw)
    return ExecutionPermit(**base)


class FHApprovalHardeningTests(unittest.TestCase):
    def setUp(self):
        self.svc = PermitService(InMemoryExecutionPermitStore())

    def test_correct_execution_approved(self):
        action = _action()
        permit = _permit(action)
        self.svc.store.create(permit)
        validated = self.svc.validate(permit, action=action, now=T0)
        self.assertEqual(validated.permit_id, "p1")

    def test_wrong_tenant_reject(self):
        action = _action(tenant_id="tenant-a")
        permit = _permit(action, tenant_id="tenant-b")
        with self.assertRaises(ExecutionPermitMismatchError) as ctx:
            self.svc.validate(permit, action=action, now=T0)
        self.assertIn("tenant", str(ctx.exception))

    def test_wrong_actor_reject(self):
        action = _action(actor_ref="tenant-a:user-a")
        permit = _permit(action, actor_ref="tenant-a:user-b")
        with self.assertRaises(ExecutionPermitMismatchError):
            self.svc.validate(permit, action=action, now=T0)

    def test_wrong_action_reject(self):
        action = _action()
        other = _action(idempotency_key="idem-2")
        permit = _permit(action)
        with self.assertRaises(ExecutionPermitMismatchError):
            self.svc.validate(permit, action=other, now=T0)

    def test_replay_fingerprint_reject(self):
        action = _action()
        permit = _permit(action, action_fingerprint="deadbeef")
        with self.assertRaises(ExecutionPermitMismatchError):
            self.svc.validate(permit, action=action, now=T0)

    def test_expired_reject(self):
        action = _action()
        permit = _permit(action, expires_at=T0 - timedelta(seconds=1))
        self.svc.store.create(permit)
        with self.assertRaises(ExecutionPermitExpiredError):
            self.svc.validate(permit, action=action, now=T0)

    def test_fingerprint_includes_tenant_actor(self):
        a = _action(tenant_id="t1", actor_ref="a1")
        b = _action(tenant_id="t2", actor_ref="a1")
        self.assertNotEqual(action_fingerprint(a), action_fingerprint(b))


if __name__ == "__main__":
    unittest.main()
