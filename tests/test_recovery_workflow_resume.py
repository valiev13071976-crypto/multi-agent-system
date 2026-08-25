"""Workflow resume rules under recovery."""

from __future__ import annotations

import unittest

from recovery.models import (
    CASE_WORKFLOW_WAITING_RECOVERY,
    DECISION_RESUME,
    STATUS_BLOCKED,
)
from recovery.orchestrator import RecoveryOrchestrator
from recovery.policy import RecoveryPolicy
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_COMPLETED, STATUS_RUNNING


class RecoveryWorkflowResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_denied(self):
        engine = WorkflowEngine()
        wf = engine.create("t")
        engine.state_manager.plan(wf)
        engine.state_manager.start(wf)
        engine.state_manager.complete_workflow(wf)
        self.assertEqual(engine.state_manager.get(wf).status, STATUS_COMPLETED)
        orch = RecoveryOrchestrator(
            workflow_engine=engine, enqueue_reconcile_on_create=False
        )
        case = orch.create_case(
            execution_id="e1",
            case_type=CASE_WORKFLOW_WAITING_RECOVERY,
            workflow_id=wf,
            enqueue=False,
        )
        orch.record_decision(
            case.recovery_id, DECISION_RESUME, actor_id="op", reason_code="resume"
        )
        out = await orch.execute_safe_step(case.recovery_id)
        self.assertEqual(out["status"], STATUS_BLOCKED)
        self.assertEqual(out["reason_code"], "terminal_workflow_resume_denied")

    async def test_non_terminal_policy_allows_plan(self):
        engine = WorkflowEngine()
        wf = engine.create("t2")
        engine.state_manager.plan(wf)
        engine.state_manager.start(wf)
        self.assertEqual(engine.state_manager.get(wf).status, STATUS_RUNNING)
        orch = RecoveryOrchestrator(
            workflow_engine=engine, enqueue_reconcile_on_create=False
        )
        case = orch.create_case(
            execution_id="e2",
            case_type=CASE_WORKFLOW_WAITING_RECOVERY,
            workflow_id=wf,
            enqueue=False,
        )
        plan = RecoveryPolicy().plan(
            case, operator_decision=DECISION_RESUME, workflow_terminal=False
        )
        self.assertEqual(plan.steps[0].action_type, "resume_workflow")


if __name__ == "__main__":
    unittest.main()
