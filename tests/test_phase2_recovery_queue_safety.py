"""Phase 2 Patch 1 — recovery & queue safety regressions."""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from autonomy.gate import build_proposed_action
from autonomy.models import ACTION_WRITE
from observability.runtime import build_observability_runtime
from security.tenant import MissingTenantError
from side_effects.errors import SideEffectExecutionDeniedError
from side_effects.executor import SideEffectExecutor
from side_effects.idempotency_keys import stable_side_effect_idempotency_key
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from task_queue.models import STATUS_RETRY_WAIT, STATUS_RUNNING, utc_now
from task_queue.queue import TaskQueue
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.definition import STEP_TYPE_HANDLER, StepResult, WorkflowDefinition, WorkflowStep
from workflow.errors import WorkflowCancelledError
from workflow.models import STATUS_CANCELLED, utc_now
from workflow.platform import WorkflowPlatform
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager
from tests.test_workflow_foundation import linear_demo_definition
from tests.side_effect_fixtures import eval_kwargs, se_action


class StartupRecoverySurfacingTests(unittest.TestCase):
    def test_recovery_failure_is_recorded_not_silent(self):
        obs = build_observability_runtime(env={})
        bundle = build_workflow_runtime(observability=obs)
        with patch.object(
            bundle.state_manager._store, "list_all", side_effect=RuntimeError("recovery_boom")
        ):
            with self.assertRaises(RuntimeError):
                bundle.recover_and_reenqueue_persisted()
        self.assertIsNotNone(bundle.last_startup_recovery_error)
        self.assertIn("recovery_boom", bundle.last_startup_recovery_error)
        failed = [
            e
            for e in obs.list_events()
            if e.event_type == "workflow.startup_recovery" and e.status == "failed"
        ]
        self.assertTrue(failed)

    def test_compose_style_records_without_raising_to_caller(self):
        obs = build_observability_runtime(env={})
        bundle = build_workflow_runtime(observability=obs)
        with patch.object(
            bundle.state_manager._store, "list_all", side_effect=RuntimeError("list_all_failed")
        ):
            try:
                bundle.recover_and_reenqueue_persisted()
            except Exception as exc:
                bundle.record_startup_recovery_failure(exc)
        self.assertIsNotNone(bundle.last_startup_recovery_error)
        self.assertIn("list_all_failed", bundle.last_startup_recovery_error)


class StuckRunningQueueRecoveryTests(unittest.TestCase):
    def test_expired_lease_reclaim_running_to_retry_wait(self):
        queue = TaskQueue(lease_seconds=30.0)
        task = queue.enqueue(
            workflow_id="wf-1",
            task_id="t-1",
            execution_key="ek-1",
            tenant_id="tenant-a",
        )
        leased = queue.dequeue(worker_id="w1")
        started = queue.start(leased.queue_task_id, leased.lease_id, worker_id="w1")
        self.assertEqual(started.status, STATUS_RUNNING)

        # Live lease must not be reclaimed (multi-process safe), even with force=True.
        self.assertEqual(queue.recover_stuck_running(force=True), ())

        # Expire lease, then reclaim.
        from dataclasses import replace

        expired = replace(
            queue.get(task.queue_task_id),
            lease_expires_at=utc_now() - timedelta(seconds=1),
        )
        queue.store.save(expired)
        reclaimed = queue.recover_stuck_running(force=True)
        self.assertEqual(list(reclaimed), [task.queue_task_id])
        recovered = queue.get(task.queue_task_id)
        self.assertEqual(recovered.status, STATUS_RETRY_WAIT)
        self.assertTrue(recovered.metadata.get("recovered_from_running"))
        self.assertEqual(recovered.tenant_id, "tenant-a")

        ready = queue.list_ready()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].queue_task_id, task.queue_task_id)

    def test_completed_not_reclaimed(self):
        queue = TaskQueue()
        task = queue.enqueue(
            workflow_id="wf-1", task_id="t-1", execution_key="ek-c", tenant_id="t"
        )
        leased = queue.dequeue(worker_id="w1")
        queue.start(leased.queue_task_id, leased.lease_id, worker_id="w1")
        queue.ack(leased.queue_task_id, leased.lease_id, worker_id="w1")
        self.assertEqual(queue.recover_stuck_running(force=True), ())
        self.assertEqual(queue.get(task.queue_task_id).status, "completed")


class CancellationFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_complete_cannot_resurrect_cancelled(self):
        manager = StateManager()
        platform = WorkflowPlatform(manager)
        definition = WorkflowDefinition(
            workflow_type="fence.demo",
            version="1",
            steps=(WorkflowStep(step_id="s1", step_type=STEP_TYPE_HANDLER),),
        )
        platform.register_definition(definition)

        async def slow_handler(ctx):
            # Simulate cancel while handler runs.
            platform.cancel(ctx["workflow_id"])
            return StepResult(ok=True, data={"done": True})

        platform.register_handler(STEP_TYPE_HANDLER, slow_handler)
        state = platform.create_instance(
            definition, task_id="t1", tenant_id="tenant-a"
        )
        manager.start(state.workflow_id)
        outcome = await platform.advance(state.workflow_id)
        self.assertEqual(manager.get(state.workflow_id).status, STATUS_CANCELLED)
        step = manager.get(state.workflow_id).step("s1")
        self.assertNotEqual(step.status, "completed")
        self.assertEqual(outcome["status"], STATUS_CANCELLED)

    def test_complete_step_raises_on_terminal(self):
        manager = StateManager()
        state = manager.create(task_id="t", tenant_id="tenant-a", step_names=("s1",))
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.cancel(state.workflow_id)
        with self.assertRaises(WorkflowCancelledError):
            manager.complete_step(state.workflow_id, "s1")
        self.assertEqual(manager.get(state.workflow_id).status, STATUS_CANCELLED)

    async def test_cancelled_blocks_protected_mutate(self):
        manager = StateManager()
        state = manager.create(task_id="t", tenant_id="tenant-a", step_names=("s1",))
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.cancel(state.workflow_id)

        adapter = InMemoryReversibleWriteAdapter(
            trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, reversible=True
        )
        registry = SideEffectAdapterRegistry()
        registry.register(adapter)
        from autonomy.gate import AutonomyGate

        gate = AutonomyGate()
        executor = SideEffectExecutor(registry, gate=gate)
        action = se_action(
            state.workflow_id,
            idempotency_key="stable-cancel-1",
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        )
        decision = gate.evaluate(action, **eval_kwargs("executor_confirmed"))
        with self.assertRaises(SideEffectExecutionDeniedError) as ctx:
            await executor.execute(
                action,
                decision=decision,
                gate=gate,
                state_manager=manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(ctx.exception.error_code, "workflow_terminal")
        self.assertEqual(adapter.calls, 0)


class StableIdempotencyTests(unittest.TestCase):
    def test_build_proposed_action_stable_key_without_explicit(self):
        a1 = build_proposed_action(
            action_type=ACTION_WRITE,
            tool_id="test.write",
            operation="set_value",
            resource="k1",
            workflow_id="wf-stable",
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        )
        a2 = build_proposed_action(
            action_type=ACTION_WRITE,
            tool_id="test.write",
            operation="set_value",
            resource="k1",
            workflow_id="wf-stable",
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        )
        self.assertEqual(a1.idempotency_key, a2.idempotency_key)
        self.assertEqual(
            a1.idempotency_key,
            stable_side_effect_idempotency_key(
                workflow_id="wf-stable",
                tool_id="test.write",
                operation="set_value",
                resource="k1",
            ),
        )


class StableIdempotencyAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_resume_recovery_same_key_blocks_duplicate(self):
        from tests.side_effect_fixtures import runtime

        engine, workflow_id, adapter, executor = runtime(
            trust=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, reversible=True
        )
        # Use internal-safe path for auth simplicity; key stability is the assert.
        engine2, workflow_id2, adapter2, executor2 = runtime()
        key = stable_side_effect_idempotency_key(
            workflow_id=workflow_id2,
            tool_id="test.write",
            operation="set_value",
            resource="test/key",
        )
        action = se_action(workflow_id2, idempotency_key=key)
        await executor2.execute(
            action,
            gate=engine2._gate(),
            state_manager=engine2.state_manager,
            evaluate_kwargs=eval_kwargs(),
            decision=engine2._gate().evaluate(action, **eval_kwargs()),
        )
        await executor2.execute(
            action,
            gate=engine2._gate(),
            state_manager=engine2.state_manager,
            evaluate_kwargs=eval_kwargs(),
            decision=engine2._gate().evaluate(action, **eval_kwargs()),
        )
        self.assertEqual(adapter2.calls, 1)


class AnalyzeOrphanRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_without_workflow_type_not_dag_enqueued(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        # Simulate Path A analyze durable row (no workflow_type).
        analyze = bundle.state_manager.create(
            task_id="analyze-1",
            tenant_id="tenant-a",
            step_names=("prepare_context",),
        )
        self.assertIsNone(analyze.workflow_type)
        bundle.state_manager.plan(analyze.workflow_id)
        bundle.state_manager.start(analyze.workflow_id)

        durable = await bundle.create_and_enqueue(
            "demo.linear", "1", tenant_id="tenant-b", task_id="dag-1"
        )
        result = bundle.recover_and_reenqueue_persisted()
        skipped_reasons = {
            s["workflow_id"]: s["reason"] for s in result["skipped"]
        }
        self.assertEqual(
            skipped_reasons.get(analyze.workflow_id), "not_durable_definition"
        )
        self.assertNotIn(analyze.workflow_id, result["reenqueued"])
        # Durable path still recoverable / listed
        self.assertTrue(
            durable["workflow_id"] in result["reenqueued"]
            or any(
                s.get("workflow_id") == durable["workflow_id"]
                for s in result["skipped"]
            )
        )


class TenantPreservedOnQueueRecoveryTests(unittest.TestCase):
    def test_reclaim_keeps_tenant_identity(self):
        from dataclasses import replace

        queue = TaskQueue()
        task = queue.enqueue(
            workflow_id="wf",
            task_id="t",
            execution_key="ek",
            tenant_id="tenant-A",
            user_id="user-A",
            actor_ref="tenant-A:user-A",
        )
        leased = queue.dequeue(worker_id="w1")
        queue.start(leased.queue_task_id, leased.lease_id, worker_id="w1")
        queue.store.save(
            replace(
                queue.get(task.queue_task_id),
                lease_expires_at=utc_now() - timedelta(seconds=1),
            )
        )
        queue.recover_stuck_running(force=True)
        got = queue.get(task.queue_task_id)
        self.assertEqual(got.tenant_id, "tenant-A")
        self.assertEqual(got.user_id, "user-A")
        self.assertEqual(got.actor_ref, "tenant-A:user-A")


if __name__ == "__main__":
    unittest.main()
