"""Read-only reconciliation through RecoveryOrchestrator."""

from __future__ import annotations

import unittest

from recovery.models import CASE_UNCERTAIN_SIDE_EFFECT, STATUS_RESOLVED, STATUS_WAITING_OPERATOR
from recovery.orchestrator import RecoveryOrchestrator
from side_effects.models import ADAPTER_RECON_UNKNOWN
from tests.side_effect_fixtures import make_uncertain, recon_runtime, se_action, T0


class RecoveryReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_success_resolves(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        orch = RecoveryOrchestrator(
            reconciliation_service=service,
            enqueue_reconcile_on_create=False,
        )
        action = se_action(workflow_id, idempotency_key="rec-orch-ok")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        case = orch.create_case(
            execution_id=result.execution_id,
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            workflow_id=workflow_id,
            reconciliation_id=record.reconciliation_id,
            enqueue=False,
        )
        calls_before = adapter.reconcile_calls
        out = await orch.execute_safe_step(case.recovery_id, now=T0)
        self.assertEqual(out["status"], STATUS_RESOLVED)
        self.assertFalse(out.get("mutated"))
        self.assertEqual(adapter.reconcile_calls, calls_before + 1)
        self.assertEqual(adapter.rollback_calls, 0)

    async def test_unknown_waits_operator(self):
        engine, workflow_id, adapter, executor, service = recon_runtime(max_attempts=1)
        adapter.reconcile_override = ADAPTER_RECON_UNKNOWN
        orch = RecoveryOrchestrator(
            reconciliation_service=service,
            enqueue_reconcile_on_create=False,
            max_read_checks=1,
        )
        action = se_action(workflow_id, idempotency_key="rec-orch-unk")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        case = orch.create_case(
            execution_id=result.execution_id,
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            reconciliation_id=record.reconciliation_id,
            enqueue=False,
        )
        writes_before = getattr(adapter, "mutate_calls", 0)
        out = await orch.execute_safe_step(case.recovery_id, now=T0)
        self.assertEqual(out["status"], STATUS_WAITING_OPERATOR)
        self.assertEqual(orch.mutation_calls, 0)


if __name__ == "__main__":
    unittest.main()
