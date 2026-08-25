from datetime import datetime, timedelta, timezone
import unittest

from autonomy.capabilities import CAP_EXTERNAL_WRITE, CapabilitySet
from autonomy.gate import AutonomyGate, build_proposed_action
from autonomy.models import (
    APPROVAL_PENDING,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
)
from hitl.authority import InMemoryApprovalAuthority, ROLE_STANDARD_APPROVER
from hitl.errors import (
    ApprovalConflictError,
    ApprovalInvalidStateError,
    ApprovalSelfApprovalError,
    ApprovalUnauthorizedResolverError,
)
from hitl.service import HITLService
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_CANCELLED, STATUS_FAILED, STATUS_WAITING_APPROVAL


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def caps(*names):
    return CapabilitySet(subject_id="agent-1", capabilities=tuple(names), issued_at=T0)


def write_action(workflow_id, task_id="task-h", **kwargs):
    fields = {
        "action_type": "write",
        "workflow_id": workflow_id,
        "task_id": task_id,
        "idempotency_key": kwargs.pop("idempotency_key", "idem-h"),
        "metadata": {"reversible": True},
        "tool_trust_level": TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        "requested_capabilities": (CAP_EXTERNAL_WRITE,),
        "tool_id": "notes",
        "operation": "patch",
        "resource": "internal",
    }
    fields.update(kwargs)
    return build_proposed_action(**fields)


def running_engine():
    engine = WorkflowEngine()
    workflow_id = engine.create("task-h")
    engine.state_manager.plan(workflow_id)
    engine.state_manager.start(workflow_id)
    authority = InMemoryApprovalAuthority()
    authority.grant("reviewer-1", ROLE_STANDARD_APPROVER)
    engine.hitl_service = HITLService(
        gate=engine._gate(),
        state_manager=engine.state_manager,
        store=engine._gate().approvals.store,
        authority=authority,
        approval_ttl_seconds=3600,
        permit_ttl_seconds=300,
    )
    return engine, workflow_id


class HITLServiceTests(unittest.TestCase):

    def test_a_require_approval_creates_one_pending(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        decision = engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        self.assertEqual(decision.decision, DECISION_REQUIRE_APPROVAL)
        record = engine._hitl().get(engine.last_approval_id)
        self.assertEqual(record.status, APPROVAL_PENDING)
        self.assertEqual(len(engine._hitl().store.list_pending()), 1)

    def test_b_duplicate_request_returns_existing(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        first = engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        second = engine._hitl().request_approval(
            action, first, requested_by="agent-1", now=T0
        )
        self.assertEqual(second.approval_id, engine.last_approval_id)
        self.assertEqual(len(engine._hitl().store.list_pending()), 1)

    def test_c_allow_decision_rejected(self):
        from autonomy.capabilities import CAP_EXTERNAL_READ

        engine, workflow_id = running_engine()
        action = build_proposed_action(
            action_type="read",
            workflow_id=workflow_id,
            task_id="task-h",
        )
        decision = engine._gate().evaluate(
            action,
            capabilities=caps(CAP_EXTERNAL_READ),
            autonomy_level="analyst",
        )
        self.assertEqual(decision.decision, DECISION_ALLOW)
        with self.assertRaises(ApprovalInvalidStateError):
            engine._hitl().request_approval(
                action, decision, requested_by="agent-1", now=T0
            )

    def test_d_deny_creates_no_approval(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        decision = engine.evaluate_action(
            action,
            capabilities=caps(),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(engine._hitl().store.list_pending(), ())

    def test_e_workflow_waiting_approval(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        self.assertEqual(
            engine.state_manager.get(workflow_id).status, STATUS_WAITING_APPROVAL
        )

    def test_f_checkpoint_safe_metadata(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        decision = engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        point = engine.state_manager.get_checkpoint(workflow_id)
        self.assertEqual(point.payload["action_id"], action.action_id)
        self.assertEqual(point.payload["decision_id"], decision.decision_id)
        self.assertTrue(point.payload["required_approval"])
        self.assertIn("action_fingerprint", point.payload)
        self.assertNotIn("prompt", point.payload)
        self.assertNotIn("signature", point.payload)

    def test_g_authorized_approver(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        record = engine._hitl().approve(
            engine.last_approval_id, resolved_by="reviewer-1", now=T0
        )
        self.assertEqual(record.status, "approved")
        self.assertEqual(record.version, 2)

    def test_h_unauthorized_stays_pending(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        with self.assertRaises(ApprovalUnauthorizedResolverError):
            engine._hitl().approve(
                engine.last_approval_id, resolved_by="stranger", now=T0
            )
        self.assertEqual(
            engine._hitl().get(engine.last_approval_id).status, APPROVAL_PENDING
        )

    def test_i_self_approval_denied(self):
        engine, workflow_id = running_engine()
        engine._hitl().authority.grant("agent-1", ROLE_STANDARD_APPROVER)
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        with self.assertRaises(ApprovalSelfApprovalError):
            engine._hitl().approve(
                engine.last_approval_id, resolved_by="agent-1", now=T0
            )

    def test_j_double_approve_conflict(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().approve(
            engine.last_approval_id, resolved_by="reviewer-1", now=T0
        )
        with self.assertRaises(ApprovalInvalidStateError):
            engine._hitl().approve(
                engine.last_approval_id, resolved_by="reviewer-1", now=T0
            )
        with self.assertRaises(ApprovalConflictError):
            engine._hitl().approve(
                engine.last_approval_id,
                resolved_by="reviewer-1",
                expected_version=1,
                now=T0,
            )

    def test_k_approve_after_reject_invalid(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().reject(
            engine.last_approval_id, resolved_by="reviewer-1", now=T0
        )
        with self.assertRaises(ApprovalInvalidStateError):
            engine._hitl().approve(
                engine.last_approval_id, resolved_by="reviewer-1", now=T0
            )

    def test_l_reject_fails_workflow(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        record = engine._hitl().reject(
            engine.last_approval_id, resolved_by="reviewer-1", now=T0
        )
        self.assertEqual(record.status, "rejected")
        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_FAILED)
        self.assertEqual(
            engine.state_manager.get(workflow_id).error_code, "approval_rejected"
        )
        with self.assertRaises(ApprovalInvalidStateError):
            engine._hitl().reevaluate_and_issue_permit(
                engine.last_approval_id,
                action,
                capabilities=caps(CAP_EXTERNAL_WRITE),
                autonomy_level="executor_confirmed",
            )

    def test_m_expire_fails_workflow(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        record = engine._hitl().expire(engine.last_approval_id, now=T0)
        self.assertEqual(record.status, "expired")
        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_FAILED)
        self.assertEqual(
            engine.state_manager.get(workflow_id).error_code, "approval_expired"
        )

    def test_n_cancel_cancels_workflow(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        record = engine._hitl().cancel(
            engine.last_approval_id, resolved_by="reviewer-1", now=T0
        )
        self.assertEqual(record.status, "cancelled")
        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_CANCELLED)


if __name__ == "__main__":
    unittest.main()
