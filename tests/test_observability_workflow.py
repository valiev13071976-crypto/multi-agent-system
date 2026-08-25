import unittest
from unittest import mock

from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_WAITING_APPROVAL
from autonomy.capabilities import CAP_EXTERNAL_WRITE
from tests.side_effect_fixtures import T0, caps, se_action
from hitl.authority import InMemoryApprovalAuthority, ROLE_PRIVILEGED_APPROVER
from hitl.service import HITLService
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


class ObservabilityWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_lifecycle_events(self):
        obs = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
        engine = WorkflowEngine(observability=obs)
        authority = InMemoryApprovalAuthority()
        authority.grant("reviewer-1", ROLE_PRIVILEGED_APPROVER)
        gate = engine._gate()
        gate.observability = obs
        hitl = HITLService(
            gate=gate,
            state_manager=engine.state_manager,
            store=gate.approvals.store,
            authority=authority,
        )
        hitl.observability = obs
        engine.hitl_service = hitl
        engine.autonomy_gate = gate

        workflow_id = engine.create("task-obs")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        ctx = obs.context_for_workflow(workflow_id)
        self.assertIsNotNone(ctx)

        # Force waiting_approval via evaluate
        action = se_action(
            workflow_id,
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        )
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
        hitl.approve(engine.last_approval_id, resolved_by="reviewer-1", now=T0)
        permit = hitl.reevaluate_and_issue_permit(
            engine.last_approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        with mock.patch("hitl.permit.utc_now", return_value=T0):
            await engine.resume(workflow_id, execution_permit=permit, action=action)

        types = [e.event_type for e in obs.list_events()]
        self.assertIn("workflow.created", types)
        self.assertIn("workflow.waiting_approval", types)
        self.assertIn("workflow.resumed", types)
        self.assertIn("hitl.requested", types)
        self.assertIn("hitl.approved", types)
        self.assertIn("permit.issued", types)
        ids = {
            (e.correlation_id, e.trace_id)
            for e in obs.list_events()
            if e.workflow_id == workflow_id
        }
        self.assertEqual(len(ids), 1, ids)


if __name__ == "__main__":
    unittest.main()
