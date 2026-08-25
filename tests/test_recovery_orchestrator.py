"""RecoveryOrchestrator core lifecycle."""

from __future__ import annotations

import unittest

from recovery.models import CASE_UNCERTAIN_SIDE_EFFECT, STATUS_WAITING_OPERATOR
from recovery.orchestrator import RecoveryAuthorizationRequired, RecoveryOrchestrator
from recovery.models import DECISION_ROLLBACK


class RecoveryOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def test_create_and_list(self):
        orch = RecoveryOrchestrator(enqueue_reconcile_on_create=False)
        case = orch.create_case(
            execution_id="e1",
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            enqueue=False,
        )
        self.assertEqual(len(orch.list_open_cases()), 1)
        self.assertEqual(orch.get_case(case.recovery_id).execution_id, "e1")

    async def test_rollback_decision_cannot_mutate(self):
        orch = RecoveryOrchestrator(enqueue_reconcile_on_create=False)
        case = orch.create_case(
            execution_id="e2",
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            enqueue=False,
        )
        orch.record_decision(
            case.recovery_id,
            DECISION_ROLLBACK,
            actor_id="op",
            reason_code="want_rollback",
        )
        with self.assertRaises(RecoveryAuthorizationRequired):
            await orch.execute_safe_step(case.recovery_id)
        self.assertEqual(orch.mutation_calls, 0)

    async def test_plan_waiting_operator(self):
        orch = RecoveryOrchestrator(enqueue_reconcile_on_create=False, max_read_checks=0)
        case = orch.create_case(
            execution_id="e3",
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            enqueue=False,
        )
        # bump attempts
        current = orch.get_case(case.recovery_id)
        orch.store.update(
            RecoveryOrchestrator._clone_case(current, attempt=current.max_attempts),
            expected_version=current.version,
        )
        plan = orch.plan(case.recovery_id, reconciliation_status="still_uncertain")
        self.assertTrue(plan.waiting_operator)


if __name__ == "__main__":
    unittest.main()
