import unittest

from autonomy.errors import IdempotencyTransitionError
from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_FAILED,
    IDEMPOTENCY_RESERVED,
    IDEMPOTENCY_UNCERTAIN,
)
from side_effects.errors import SideEffectIdempotencyError
from side_effects.models import OUTCOME_KNOWN_SUCCESS, STATUS_SUCCEEDED
from tests.side_effect_fixtures import (
    T0,
    allow_execute,
    make_uncertain,
    recon_runtime,
    se_action,
)


class ReconciliationIdempotencyTests(unittest.IsolatedAsyncioTestCase):

    async def test_o_uncertain_to_completed(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="id-o")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(engine._gate().idempotency.get("id-o").state, IDEMPOTENCY_COMPLETED)

    async def test_p_uncertain_to_failed(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.reconcile_override = "failed"
        action = se_action(workflow_id, idempotency_key="id-p")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(engine._gate().idempotency.get("id-p").state, IDEMPOTENCY_FAILED)

    def test_q_completed_cannot_move_to_failed(self):
        registry = IdempotencyRegistry()
        registry.reserve("k-q", "a1")
        registry.mark_started("k-q")
        registry.mark_completed("k-q")
        with self.assertRaises(IdempotencyTransitionError):
            registry.mark_failed("k-q")

    def test_r_uncertain_cannot_reset_to_reserved(self):
        registry = IdempotencyRegistry()
        registry.reserve("k-r", "a1")
        registry.mark_started("k-r")
        registry.mark_uncertain("k-r")
        with self.assertRaises(Exception):
            registry.reserve("k-r", "a1")
        with self.assertRaises(IdempotencyTransitionError):
            registry.reconcile_transition("k-r", IDEMPOTENCY_RESERVED)

    async def test_s_confirmed_success_blocks_duplicate_execute(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="id-s")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        replay = await allow_execute(executor, action, engine, "again")
        self.assertEqual(replay.status, STATUS_SUCCEEDED)
        self.assertEqual(replay.outcome, OUTCOME_KNOWN_SUCCESS)
        self.assertEqual(adapter.calls, 1)

    async def test_t_confirmed_failure_does_not_reset_key(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.reconcile_override = "failed"
        action = se_action(workflow_id, idempotency_key="id-t")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        with self.assertRaises(SideEffectIdempotencyError):
            await allow_execute(executor, action, engine, "retry")
        self.assertEqual(adapter.calls, 1)


if __name__ == "__main__":
    unittest.main()
