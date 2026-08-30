from datetime import timedelta
import unittest

from autonomy.models import IDEMPOTENCY_STARTED
from side_effects.errors import ReconciliationNotEligibleError
from side_effects.models import (
    OUTCOME_KNOWN_SUCCESS,
    OUTCOME_UNCERTAIN,
    RECON_PENDING,
    STATUS_UNKNOWN,
    SideEffectExecutionRecord,
    hash_idempotency_key,
)
from tests.side_effect_fixtures import (
    T0,
    allow_execute,
    eval_kwargs,
    make_uncertain,
    recon_runtime,
    se_action,
)


class ReconciliationServiceTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_uncertain_creates_record(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="rec-a")
        result = await make_uncertain(executor, action, engine)
        self.assertEqual(result.outcome, OUTCOME_UNCERTAIN)
        rows = service.store.find_by_execution(result.execution_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, RECON_PENDING)

    async def test_b_stale_started_is_candidate(self):
        engine, workflow_id, adapter, executor, service = recon_runtime(
            stale_after_seconds=300
        )
        action = se_action(workflow_id, idempotency_key="stale-b")
        engine._gate().evaluate(action, **eval_kwargs())
        engine._gate().idempotency.mark_started("stale-b")
        record = SideEffectExecutionRecord(
            execution_id="exec-stale",
            action_id=action.action_id,
            workflow_id=workflow_id,
            task_id=action.task_id,
            tool_id=action.tool_id,
            operation=action.operation,
            status=STATUS_UNKNOWN,
            authorization_type="autonomy_decision",
            authorization_id="d1",
            idempotency_key_hash=hash_idempotency_key("stale-b"),
            attempt=1,
            started_at=T0 - timedelta(seconds=400),
            completed_at=None,
            outcome=OUTCOME_UNCERTAIN,
            tenant_id="tenant-se",
            metadata={"adapter_started": True},
        )
        executor.store.create(record)
        found = service.find_stale_started(now=T0)
        self.assertTrue(any(row.execution_id == "exec-stale" for row in found))
        created = service.create_for_execution("exec-stale")
        self.assertEqual(created.execution_id, "exec-stale")

    async def test_c_succeeded_not_eligible(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="ok-c")
        result = await allow_execute(executor, action, engine)
        self.assertEqual(result.outcome, OUTCOME_KNOWN_SUCCESS)
        with self.assertRaises(ReconciliationNotEligibleError):
            service.create_for_execution(result.execution_id)

    async def test_d_failed_before_adapter_not_required(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key=None)
        from side_effects.errors import SideEffectIdempotencyError

        with self.assertRaises(SideEffectIdempotencyError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(service.list_pending(), ())

    async def test_e_duplicate_returns_existing(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="dup-e")
        result = await make_uncertain(executor, action, engine)
        first = service.store.find_by_execution(result.execution_id)[0]
        second = service.create_for_execution(result.execution_id)
        self.assertEqual(first.reconciliation_id, second.reconciliation_id)

    async def test_av_version_increments(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="ver-av")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        self.assertEqual(record.version, 1)
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        updated = service.get(record.reconciliation_id)
        self.assertGreater(updated.version, 1)

    async def test_aw_stale_version_conflicts(self):
        from side_effects.errors import ReconciliationConflictError

        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="ver-aw")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        with self.assertRaises(ReconciliationConflictError):
            await service.reconcile(
                record.reconciliation_id, action=action, now=T0, expected_version=1
            )

    async def test_ax_double_resolution_conflicts(self):
        from side_effects.errors import ReconciliationConflictError
        from side_effects.models import RECONCILIATION_TERMINAL

        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="ver-ax")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        first = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertIn(first.status, RECONCILIATION_TERMINAL | {"still_uncertain", "confirmed_succeeded"})
        if first.status in RECONCILIATION_TERMINAL:
            with self.assertRaises(ReconciliationConflictError):
                await service.reconcile(record.reconciliation_id, action=action, now=T0)


if __name__ == "__main__":
    unittest.main()
