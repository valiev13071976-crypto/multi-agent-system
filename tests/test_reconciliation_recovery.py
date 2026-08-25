from datetime import timedelta
import unittest

from fastapi.testclient import TestClient

from side_effects.errors import SideEffectAuthorizationError
from side_effects.models import (
    OUTCOME_KNOWN_FAILURE,
    OUTCOME_KNOWN_SUCCESS,
    OUTCOME_UNCERTAIN,
    RECON_STILL_UNCERTAIN,
    STATUS_SUCCEEDED,
)
from task_queue.models import STATUS_DEAD_LETTERED, STATUS_RETRY_WAIT
from task_queue.queue import TaskQueue
from task_queue.retry import RetryPolicy
from task_queue.store import InMemoryTaskQueueStore
from task_queue.worker import TaskWorker
from tests.side_effect_fixtures import (
    T0,
    allow_execute,
    ctx,
    eval_kwargs,
    hitl_runtime,
    issue_permit,
    make_uncertain,
    recon_runtime,
    se_action,
)
from tests.test_mode_auto import STRATEGY_TEXT, load_auto_app
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_READ_ONLY_EXTERNAL, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.models import TERMINAL_STATUSES


class ReconciliationRecoveryTests(unittest.IsolatedAsyncioTestCase):

    async def test_u_confirmed_failure_retry_eligible_no_execute(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.reconcile_override = "failed"
        action = se_action(workflow_id, idempotency_key="rec-u")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertTrue(outcome.retry_eligible)
        self.assertEqual(adapter.calls, 1)

    async def test_v_retry_requires_reauthorization(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.reconcile_override = "failed"
        action = se_action(workflow_id, idempotency_key="rec-v")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertTrue(outcome.reauthorization_required)

    async def test_w_old_consumed_permit_rejected(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        from side_effects.reconciliation import SideEffectReconciliationService

        service = SideEffectReconciliationService(
            execution_store=executor.store,
            idempotency=engine._gate().idempotency,
            registry=executor.registry,
            audit=executor.audit,
        )
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        await executor.execute(
            action,
            permit=permit,
            context=ctx("p"),
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs("executor_confirmed"),
        )
        consumed = engine._hitl().permits.get(permit.permit_id)
        with self.assertRaises(SideEffectAuthorizationError):
            service.policy.require_fresh_authorization(old_permit=consumed)

    async def test_x_recovery_requires_new_gate_evaluation(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="rec-x")
        first = engine._gate().evaluate(action, **eval_kwargs())
        again = engine._gate().evaluate(action, **eval_kwargs())
        self.assertNotEqual(first.decision_id, again.decision_id)

    async def test_y_protected_retry_needs_new_permit(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        self.assertEqual(permit.status, "issued")
        engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
        consumed = engine._hitl().permits.get(permit.permit_id)
        self.assertEqual(consumed.status, "consumed")
        from side_effects.recovery import RecoveryPolicy

        with self.assertRaises(SideEffectAuthorizationError):
            RecoveryPolicy().require_fresh_authorization(permit=consumed)

    async def test_z_task_queue_does_not_retry_uncertain(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        queue = TaskQueue(
            InMemoryTaskQueueStore(),
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=1),
        )
        worker = TaskWorker(queue, engine=engine)

        async def handler(ctx_item):
            action = se_action(workflow_id, idempotency_key="q-unc")
            await make_uncertain(executor, action, engine)

        queue.enqueue(workflow_id=workflow_id, task_id="task-se", execution_key="ek-r")
        worker.handler = handler
        result = await worker.run_once()
        self.assertNotEqual(result.status, STATUS_RETRY_WAIT)

    async def test_aa_to_af_success_reconciliation(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="rec-aa")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        execution = executor.store.get(result.execution_id)
        self.assertEqual(execution.outcome, OUTCOME_KNOWN_SUCCESS)
        self.assertTrue(outcome.external_reference)
        self.assertEqual(engine._gate().idempotency.get("rec-aa").state, "completed")
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(outcome.rollback_candidate)
        self.assertEqual(adapter.rollback_calls, 0)
        self.assertEqual(service.rollback_invocations, 0)

    async def test_ag_to_ak_failure_reconciliation(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.reconcile_override = "failed"
        action = se_action(workflow_id, idempotency_key="rec-ag")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.outcome, OUTCOME_KNOWN_FAILURE)
        self.assertEqual(engine._gate().idempotency.get("rec-ag").state, "failed")
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(outcome.recovery_id)
        self.assertEqual(
            outcome.metadata.get("parent_execution_id"), result.execution_id
        )
        self.assertIn(
            engine.state_manager.get(workflow_id).status, TERMINAL_STATUSES
        )

    async def test_al_timeout_still_uncertain(self):
        engine, workflow_id, adapter, executor, service = recon_runtime(
            timeout_seconds=0.01
        )
        adapter.hang_reconcile = True
        action = se_action(workflow_id, idempotency_key="rec-al")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_STILL_UNCERTAIN)
        self.assertEqual(outcome.reason_code, "reconciliation_timeout")

    async def test_am_max_attempts_manual_review(self):
        engine, workflow_id, adapter, executor, service = recon_runtime(max_attempts=1)
        adapter.reconcile_override = "unknown"
        action = se_action(workflow_id, idempotency_key="rec-am")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertTrue(outcome.manual_review_required)

    async def test_an_ao_conflict_no_overwrite(self):
        from dataclasses import replace

        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="rec-an")
        result = await make_uncertain(executor, action, engine)
        execution = executor.store.get(result.execution_id)
        executor.store.save(replace(execution, external_reference="local-A"))
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertTrue(outcome.manual_review_required)
        self.assertEqual(executor.store.get(result.execution_id).outcome, OUTCOME_UNCERTAIN)

    async def test_ap_manual_review_does_not_unlock(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.set_reconciliation_flags(supports_reconciliation=False)
        action = se_action(workflow_id, idempotency_key="rec-ap")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        with self.assertRaises(Exception):
            await allow_execute(executor, action, engine, "nope")
        self.assertEqual(adapter.calls, 1)

    async def test_aq_to_at_manual_resolution(self):
        from side_effects.errors import ReconciliationConflictError

        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.set_reconciliation_flags(supports_reconciliation=False)
        action = se_action(workflow_id, idempotency_key="rec-aq")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        with self.assertRaises(ReconciliationConflictError):
            service.resolve_manual(
                record.reconciliation_id,
                outcome="confirm_succeeded",
                resolver_id="",
                reason_code="ops",
            )
        resolved = service.resolve_manual(
            record.reconciliation_id,
            outcome="confirm_failure" if False else "confirm_failed",
            resolver_id="ops-1",
            reason_code="ops_confirmed_absent",
        )
        self.assertEqual(resolved.outcome, OUTCOME_KNOWN_FAILURE)
        self.assertEqual(adapter.calls, 1)
        events = [event.event_type for event in executor.audit.events()]
        self.assertIn("manual_resolution_failure", events)

    async def test_bh_success_path_unchanged(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="rec-bh")
        result = await allow_execute(executor, action, engine, "ok")
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(adapter.calls, 1)

    def test_bl_analyze_seven_fields(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            payload = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "openai"},
            ).json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))

    def test_bo_bp_modes(self):
        main_mod = load_auto_app("anthropic", "openai", auto_order="anthropic,openai")
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "anthropic", "openai")
        with stack:
            client = TestClient(main_mod.app)
            auto = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(auto.status_code, 200)
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            both = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "both"},
            )
        self.assertEqual(both.status_code, 200)
        self.assertEqual(len(both.json()), 7)

    def test_bm_tool_gateway(self):
        self.assertEqual(ToolGateway().tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)


if __name__ == "__main__":
    unittest.main()
