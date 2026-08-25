"""Rollback requires full authorization path."""

from __future__ import annotations

import unittest

from recovery.models import CASE_UNCERTAIN_SIDE_EFFECT, DECISION_ROLLBACK
from recovery.orchestrator import RecoveryAuthorizationRequired, RecoveryOrchestrator
from side_effects.errors import SideEffectAuthorizationError
from side_effects.recovery import RecoveryPolicy as SERecoveryPolicy
from hitl.models import PERMIT_CONSUMED
from tests.side_effect_fixtures import hitl_runtime, issue_permit, se_action, T0, eval_kwargs, ctx
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


class RecoveryRollbackAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_decision_alone_no_adapter_rollback(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        orch = RecoveryOrchestrator(
            side_effect_executor=executor,
            gate=engine._gate(),
            hitl=engine._hitl(),
            enqueue_reconcile_on_create=False,
        )
        case = orch.create_case(
            execution_id="exec-fake",
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            workflow_id=workflow_id,
            enqueue=False,
        )
        orch.record_decision(
            case.recovery_id, DECISION_ROLLBACK, actor_id="op", reason_code="rb"
        )
        with self.assertRaises(RecoveryAuthorizationRequired):
            await orch.execute_safe_step(case.recovery_id)
        self.assertEqual(adapter.rollback_calls, 0)
        self.assertEqual(orch.mutation_calls, 0)

    async def test_consumed_permit_rejected_for_recovery(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
        consumed = engine._hitl().permits.get(permit.permit_id)
        self.assertEqual(consumed.status, PERMIT_CONSUMED)
        with self.assertRaises(SideEffectAuthorizationError):
            SERecoveryPolicy().require_fresh_authorization(permit=consumed)


if __name__ == "__main__":
    unittest.main()
