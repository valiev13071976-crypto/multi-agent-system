"""Workflow & Execution Foundation — DAG / retry / queue / resume tests."""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta

from workflow.builtins import (
    branch_demo_definition,
    linear_demo_definition,
    parallel_demo_definition,
    retry_demo_definition,
)
from workflow.dag import ready_step_ids, validate_definition
from workflow.definition import (
    BranchCondition,
    BranchRule,
    FAILURE_COMPENSATE,
    FAILURE_CONTINUE,
    FAILURE_SKIP,
    FAILURE_WAIT_FOR_HUMAN,
    ScheduleSpec,
    STEP_TYPE_HANDLER,
    STEP_TYPE_SIDE_EFFECT,
    StepResult,
    StepRetryPolicy,
    WorkflowDefinition,
    WorkflowStep,
)
from workflow.errors import WorkflowDefinitionError
from workflow.models import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_WAITING_APPROVAL,
    STEP_COMPLETED,
    STEP_SKIPPED,
    utc_now,
)
from workflow.platform import WorkflowPlatform
from workflow.registry import DefinitionRegistry
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager
from workflow.store import InMemoryWorkflowStateStore
from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import scope_execution_key


class DefinitionValidationTests(unittest.TestCase):
    def test_valid_dag(self):
        validate_definition(linear_demo_definition())

    def test_missing_dependency(self):
        bad = WorkflowDefinition(
            workflow_type="bad",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="a",
                    step_type=STEP_TYPE_HANDLER,
                    dependencies=("missing",),
                ),
            ),
        )
        with self.assertRaises(WorkflowDefinitionError) as ctx:
            validate_definition(bad)
        self.assertEqual(ctx.exception.error_code, "unknown_dependency")

    def test_cycle_rejected(self):
        bad = WorkflowDefinition(
            workflow_type="cycle",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="a",
                    step_type=STEP_TYPE_HANDLER,
                    dependencies=("b",),
                ),
                WorkflowStep(
                    step_id="b",
                    step_type=STEP_TYPE_HANDLER,
                    dependencies=("a",),
                ),
            ),
        )
        with self.assertRaises(WorkflowDefinitionError) as ctx:
            validate_definition(bad)
        self.assertEqual(ctx.exception.error_code, "cycle_detected")


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_linear_workflow(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        platform.register_definition(linear_demo_definition())

        async def handler(ctx):
            return StepResult(ok=True, data={"n": ctx["step"].step_id})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(
            linear_demo_definition(), task_id="t1"
        )
        platform.state_manager.start(state.workflow_id)
        result = await platform.advance(state.workflow_id)
        self.assertEqual(result["status"], STATUS_COMPLETED)
        final = platform.state_manager.get(state.workflow_id)
        self.assertEqual(
            [s.status for s in final.steps],
            [STEP_COMPLETED, STEP_COMPLETED, STEP_COMPLETED],
        )

    async def test_parallel_ready_dependencies(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = parallel_demo_definition()
        platform.register_definition(definition)

        async def handler(ctx):
            return StepResult(ok=True, data={})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t2")
        platform.state_manager.start(state.workflow_id)
        # after root, left and right both ready
        await platform._run_step(state.workflow_id, "root")
        ready = ready_step_ids(
            definition, platform.state_manager.get(state.workflow_id)
        )
        self.assertEqual(set(ready), {"left", "right"})
        await platform.advance(state.workflow_id)
        self.assertEqual(
            platform.state_manager.get(state.workflow_id).status, STATUS_COMPLETED
        )

    async def test_branching_skips_other_path(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = branch_demo_definition()
        platform.register_definition(definition)

        async def handler(ctx):
            sid = ctx["step"].step_id
            if sid == "decide":
                return StepResult(ok=True, data={"path": "left"})
            return StepResult(ok=True, data={"ran": sid})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        platform.register_handler("branch", handler)
        state = platform.create_instance(definition, task_id="t3")
        platform.state_manager.start(state.workflow_id)
        await platform.advance(state.workflow_id)
        final = platform.state_manager.get(state.workflow_id)
        self.assertEqual(final.step("left_path").status, STEP_COMPLETED)
        self.assertEqual(final.step("right_path").status, STEP_SKIPPED)
        self.assertEqual(final.status, STATUS_COMPLETED)


class PersistenceResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_restart_no_duplicate(self):
        store = InMemoryWorkflowStateStore()
        platform = WorkflowPlatform(StateManager(store=store, step_names=()))
        definition = linear_demo_definition()
        platform.register_definition(definition)
        calls = []

        async def handler(ctx):
            calls.append(ctx["step"].step_id)
            return StepResult(ok=True, data={})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t4")
        platform.state_manager.start(state.workflow_id)
        await platform._run_step(state.workflow_id, "a")
        platform.state_manager.checkpoint(state.workflow_id)

        # Simulate new process sharing store
        platform2 = WorkflowPlatform(StateManager(store=store, step_names=()))
        platform2.register_definition(definition)
        platform2.register_handler(STEP_TYPE_HANDLER, handler)
        # interrupt mid-flight: mark b running then recover
        platform2.state_manager.start_step(state.workflow_id, "b")
        platform2.recover_after_restart(state.workflow_id)
        await platform2.advance(state.workflow_id)
        self.assertEqual(calls.count("a"), 1)
        self.assertEqual(
            platform2.state_manager.get(state.workflow_id).status, STATUS_COMPLETED
        )


class RetryTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_retry_then_success(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = retry_demo_definition()
        platform.register_definition(definition)
        attempts = {"n": 0}

        async def handler(ctx):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return StepResult(
                    ok=False,
                    error_code="temporary_provider_error",
                    error_class="temporary_provider_error",
                )
            return StepResult(ok=True, data={"ok": True})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t5")
        platform.state_manager.start(state.workflow_id)
        r1 = await platform.advance(state.workflow_id)
        self.assertEqual(r1["status"], STATUS_RETRY_WAIT)
        # force retry window elapsed
        st = platform.state_manager.get(state.workflow_id)
        platform.state_manager.mark_retry_wait(
            state.workflow_id,
            next_retry_at=utc_now() - timedelta(seconds=1),
            error_code=st.error_code,
        )
        r2 = await platform.advance(state.workflow_id)
        self.assertEqual(r2["status"], STATUS_COMPLETED)
        self.assertEqual(attempts["n"], 2)

    async def test_non_retryable_stops(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = WorkflowDefinition(
            workflow_type="nr",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="x",
                    step_type=STEP_TYPE_HANDLER,
                    retry_policy=StepRetryPolicy(max_attempts=5),
                    failure_policy="retry",
                ),
            ),
        )
        platform.register_definition(definition)

        async def handler(ctx):
            return StepResult(ok=False, error_code="validation_error")

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t6")
        platform.state_manager.start(state.workflow_id)
        result = await platform.advance(state.workflow_id)
        self.assertEqual(result["status"], STATUS_FAILED)

    async def test_step_timeout(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = WorkflowDefinition(
            workflow_type="to",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="slow",
                    step_type=STEP_TYPE_HANDLER,
                    timeout_seconds=0.05,
                    retry_policy=StepRetryPolicy(max_attempts=1),
                ),
            ),
        )
        platform.register_definition(definition)

        async def handler(ctx):
            await asyncio.sleep(1)
            return StepResult(ok=True, data={})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t7")
        platform.state_manager.start(state.workflow_id)
        result = await platform.advance(state.workflow_id)
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(
            platform.state_manager.get(state.workflow_id).error_code, "step_timeout"
        )

    async def test_workflow_deadline(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = WorkflowDefinition(
            workflow_type="dl",
            version="1",
            timeout_seconds=0.01,
            steps=(WorkflowStep(step_id="a", step_type=STEP_TYPE_HANDLER),),
        )
        platform.register_definition(definition)

        async def handler(ctx):
            await asyncio.sleep(0.05)
            return StepResult(ok=True, data={})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t8")
        # force deadline in the past
        from dataclasses import replace

        st = platform.state_manager.get(state.workflow_id)
        platform.state_manager._store.save(
            replace(st, deadline_at=utc_now() - timedelta(seconds=1))
        )
        platform.state_manager.start(state.workflow_id)
        result = await platform.advance(state.workflow_id)
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(
            platform.state_manager.get(state.workflow_id).error_code,
            "workflow_deadline_exceeded",
        )


class HitlPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_approval_and_resume(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = WorkflowDefinition(
            workflow_type="hitl",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="needs_ok",
                    step_type=STEP_TYPE_HANDLER,
                    requires_approval=True,
                ),
                WorkflowStep(
                    step_id="after",
                    step_type=STEP_TYPE_HANDLER,
                    dependencies=("needs_ok",),
                ),
            ),
        )
        platform.register_definition(definition)

        async def handler(ctx):
            return StepResult(ok=True, data={})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t9")
        platform.state_manager.start(state.workflow_id)
        r1 = await platform.advance(state.workflow_id)
        self.assertEqual(r1["status"], STATUS_WAITING_APPROVAL)
        platform.state_manager.approve(state.workflow_id)
        r2 = await platform.advance(state.workflow_id)
        self.assertEqual(r2["status"], STATUS_COMPLETED)

    async def test_deny_fail_policy(self):
        sm = StateManager(step_names=())
        platform = WorkflowPlatform(sm)
        definition = WorkflowDefinition(
            workflow_type="deny",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="needs_ok",
                    step_type=STEP_TYPE_HANDLER,
                    requires_approval=True,
                    failure_policy=FAILURE_WAIT_FOR_HUMAN,
                ),
            ),
        )
        platform.register_definition(definition)
        platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        state = platform.create_instance(definition, task_id="t10")
        platform.state_manager.start(state.workflow_id)
        await platform.advance(state.workflow_id)
        # simulate HITL deny → fail workflow
        platform.state_manager.fail_workflow(state.workflow_id, "approval_rejected")
        self.assertEqual(
            platform.state_manager.get(state.workflow_id).status, STATUS_FAILED
        )


class QueueWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_worker_consume(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())

        async def handler(ctx):
            return StepResult(ok=True, data={})

        bundle.platform.register_handler(STEP_TYPE_HANDLER, handler)
        created = await bundle.create_and_enqueue("demo.linear", "1", task_id="q1", tenant_id="wf-test-tenant")
        self.assertEqual(created["status"], STATUS_QUEUED)
        self.assertIsNotNone(created["queue_task_id"])
        task = await bundle.worker.run_once()
        self.assertIsNotNone(task)
        self.assertEqual(
            bundle.state_manager.get(created["workflow_id"]).status, STATUS_COMPLETED
        )

    async def test_schedule_persisted_next_run(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        now = utc_now()
        bundle.register_schedule(
            ScheduleSpec(
                schedule_id="s1",
                workflow_type="demo.linear",
                version="1",
                payload={"tenant_id": "wf-test-tenant"},
                run_at=now - timedelta(seconds=1),
                interval_seconds=3600,
            )
        )
        launched = await bundle.tick_schedules()
        self.assertEqual(len(launched), 1)
        st = bundle.scheduler.store.get("s1")
        self.assertIsNotNone(st.last_execution_key)
        self.assertGreater(st.next_run_at, now)
        # duplicate tick same window should not double-fire (new next_run)
        launched2 = await bundle.tick_schedules()
        self.assertEqual(launched2, [])


class FailurePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_policy(self):
        platform = WorkflowPlatform(StateManager(step_names=()))
        definition = WorkflowDefinition(
            workflow_type="skip",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="bad",
                    step_type=STEP_TYPE_HANDLER,
                    failure_policy=FAILURE_SKIP,
                ),
                WorkflowStep(
                    step_id="ok",
                    step_type=STEP_TYPE_HANDLER,
                    dependencies=("bad",),
                ),
            ),
        )
        platform.register_definition(definition)

        async def handler(ctx):
            if ctx["step"].step_id == "bad":
                return StepResult(ok=False, error_code="temporary_tool_unavailable")
            return StepResult(ok=True, data={})

        platform.register_handler(STEP_TYPE_HANDLER, handler)
        state = platform.create_instance(definition, task_id="t11")
        platform.state_manager.start(state.workflow_id)
        await platform.advance(state.workflow_id)
        final = platform.state_manager.get(state.workflow_id)
        self.assertEqual(final.step("bad").status, STEP_SKIPPED)
        self.assertEqual(final.step("ok").status, STEP_COMPLETED)
        self.assertEqual(final.status, STATUS_COMPLETED)


class CompensationTests(unittest.IsolatedAsyncioTestCase):
    async def test_compensation_failure_visible(self):
        class FakeEngine:
            async def rollback_side_effect(self, execution_id, action):
                raise RuntimeError("rollback_boom")

        platform = WorkflowPlatform(
            StateManager(step_names=()), workflow_engine=FakeEngine()
        )
        definition = WorkflowDefinition(
            workflow_type="comp",
            version="1",
            steps=(
                WorkflowStep(
                    step_id="write",
                    step_type=STEP_TYPE_SIDE_EFFECT,
                    compensation_action="github.label_issue",
                ),
                WorkflowStep(
                    step_id="boom",
                    step_type=STEP_TYPE_HANDLER,
                    dependencies=("write",),
                    failure_policy=FAILURE_COMPENSATE,
                ),
            ),
        )
        platform.register_definition(definition)

        async def se_handler(ctx):
            return StepResult(ok=True, data={}, result_ref="exec-1")

        async def boom(ctx):
            return StepResult(ok=False, error_code="temporary_provider_error")

        platform.register_handler(STEP_TYPE_SIDE_EFFECT, se_handler)
        platform.register_handler(STEP_TYPE_HANDLER, boom)
        state = platform.create_instance(definition, task_id="t12")
        platform.state_manager.start(state.workflow_id)
        await platform.advance(state.workflow_id)
        meta = platform.state_manager.get(state.workflow_id).metadata
        hist = meta.get("compensation_history") or []
        self.assertTrue(hist)
        self.assertEqual(hist[0]["status"], "compensation_failed")


class IdempotencyQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_execution_key_does_not_rerun(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        calls = []

        async def handler(ctx):
            calls.append(ctx["step"].step_id)
            return StepResult(ok=True, data={})

        bundle.platform.register_handler(STEP_TYPE_HANDLER, handler)
        key = "idem-exec-1"
        first = await bundle.create_and_enqueue(
            "demo.linear", "1", task_id="i1", execution_key=key, tenant_id="wf-test-tenant")
        second = await bundle.create_and_enqueue(
            "demo.linear", "1", task_id="i2", execution_key=key, tenant_id="wf-test-tenant")
        self.assertEqual(first["workflow_id"], second["workflow_id"])
        self.assertEqual(first["queue_task_id"], second["queue_task_id"])
        self.assertEqual(
            len(
                [
                    w
                    for w in bundle.state_manager._store.list_all()
                    if w.execution_key == key
                ]
            ),
            1,
        )
        await bundle.worker.run_once()
        self.assertEqual(calls.count("a"), 1)


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_survives_restart_and_completes(self):
        store = InMemoryWorkflowStateStore()
        sm = StateManager(store=store, step_names=())
        bundle1 = build_workflow_runtime(state_manager=sm)
        bundle1.definitions.register(linear_demo_definition())
        calls = []

        async def handler(ctx):
            calls.append(ctx["step"].step_id)
            return StepResult(ok=True, data={})

        bundle1.platform.register_handler(STEP_TYPE_HANDLER, handler)
        created = await bundle1.create_and_enqueue(
            "demo.linear", "1", task_id="sr1", execution_key="sr-key-1", tenant_id="wf-test-tenant")
        wid = created["workflow_id"]
        # Complete first step, then leave queued mid-flight
        bundle1.state_manager.start(wid)
        await bundle1.platform._run_step(wid, "a")
        bundle1.state_manager.queue(wid)
        self.assertEqual(bundle1.state_manager.get(wid).status, STATUS_QUEUED)
        self.assertEqual(
            bundle1.state_manager.get(wid).step("a").status, STEP_COMPLETED
        )

        # Process restart: new empty TaskQueue, same durable store
        bundle2 = build_workflow_runtime(state_manager=StateManager(store=store, step_names=()))
        bundle2.definitions.register(linear_demo_definition())
        bundle2.platform.register_handler(STEP_TYPE_HANDLER, handler)
        self.assertEqual(len(list(bundle2.queue.store.list_all())), 0)

        report = bundle2.recover_and_reenqueue_persisted()
        self.assertIn(wid, report["reenqueued"])
        self.assertEqual(len(list(bundle2.queue.store.list_all())), 1)

        task = await bundle2.worker.run_once()
        self.assertIsNotNone(task)
        self.assertEqual(bundle2.state_manager.get(wid).status, STATUS_COMPLETED)
        self.assertEqual(calls.count("a"), 1)  # not re-run
        self.assertEqual(calls.count("b"), 1)
        self.assertEqual(calls.count("c"), 1)

    async def test_due_retry_wait_reenqueued(self):
        store = InMemoryWorkflowStateStore()
        sm = StateManager(store=store, step_names=())
        bundle = build_workflow_runtime(state_manager=sm)
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        created = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="retry-due-1", tenant_id="wf-test-tenant")
        wid = created["workflow_id"]
        bundle.state_manager.mark_retry_wait(
            wid, next_retry_at=utc_now() - timedelta(seconds=1), error_code="timeout"
        )
        # New empty queue
        bundle2 = build_workflow_runtime(state_manager=StateManager(store=store, step_names=()))
        bundle2.definitions.register(linear_demo_definition())
        report = bundle2.recover_and_reenqueue_persisted()
        self.assertIn(wid, report["reenqueued"])

    async def test_future_retry_wait_not_enqueued(self):
        store = InMemoryWorkflowStateStore()
        sm = StateManager(store=store, step_names=())
        bundle = build_workflow_runtime(state_manager=sm)
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        created = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="retry-future-1", tenant_id="wf-test-tenant")
        wid = created["workflow_id"]
        bundle.state_manager.mark_retry_wait(
            wid, next_retry_at=utc_now() + timedelta(hours=1), error_code="timeout"
        )
        bundle2 = build_workflow_runtime(state_manager=StateManager(store=store, step_names=()))
        report = bundle2.recover_and_reenqueue_persisted()
        self.assertNotIn(wid, report["reenqueued"])
        self.assertTrue(
            any(s["workflow_id"] == wid and s["reason"] == "retry_not_due" for s in report["skipped"])
        )
        self.assertEqual(len(list(bundle2.queue.store.list_all())), 0)

    async def test_waiting_approval_not_enqueued(self):
        store = InMemoryWorkflowStateStore()
        sm = StateManager(store=store, step_names=())
        bundle = build_workflow_runtime(state_manager=sm)
        bundle.definitions.register(linear_demo_definition())
        created = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="wait-1", tenant_id="wf-test-tenant")
        wid = created["workflow_id"]
        bundle.state_manager.start(wid)
        bundle.state_manager.start_step(wid, "a")
        bundle.state_manager.wait_for_approval(wid)
        bundle2 = build_workflow_runtime(state_manager=StateManager(store=store, step_names=()))
        report = bundle2.recover_and_reenqueue_persisted()
        self.assertNotIn(wid, report["reenqueued"])
        self.assertTrue(
            any(
                s["workflow_id"] == wid and s["reason"] == "waiting_approval"
                for s in report["skipped"]
            )
        )

    async def test_terminal_not_enqueued(self):
        store = InMemoryWorkflowStateStore()
        sm = StateManager(store=store, step_names=())
        bundle = build_workflow_runtime(state_manager=sm)
        bundle.definitions.register(linear_demo_definition())
        created = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="term-1", tenant_id="wf-test-tenant")
        wid = created["workflow_id"]
        bundle.state_manager.fail_workflow(wid, "boom")
        bundle2 = build_workflow_runtime(state_manager=StateManager(store=store, step_names=()))
        report = bundle2.recover_and_reenqueue_persisted()
        self.assertNotIn(wid, report["reenqueued"])
        self.assertTrue(
            any(s["workflow_id"] == wid and s["reason"] == "terminal" for s in report["skipped"])
        )

    async def test_duplicate_startup_recovery_no_double_enqueue(self):
        store = InMemoryWorkflowStateStore()
        sm = StateManager(store=store, step_names=())
        bundle = build_workflow_runtime(state_manager=sm)
        bundle.definitions.register(linear_demo_definition())
        created = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="dup-startup-1", tenant_id="wf-test-tenant")
        # Drain original queue object so only recovery matters
        bundle2 = build_workflow_runtime(state_manager=StateManager(store=store, step_names=()))
        r1 = bundle2.recover_and_reenqueue_persisted()
        r2 = bundle2.recover_and_reenqueue_persisted()
        self.assertIn(created["workflow_id"], r1["reenqueued"])
        # Second pass: queue dedupe returns same task — still one queue item
        self.assertEqual(len(list(bundle2.queue.store.list_all())), 1)
        # May list in reenqueued again but same queue_task_id
        tasks = list(bundle2.queue.store.list_all())
        self.assertEqual(
            tasks[0].execution_key,
            scope_execution_key("wf-test-tenant", "dup-startup-1"),
        )


class WorkflowIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_execution_key_one_workflow_one_queue_task(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        a = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="wf-idem-1", tenant_id="wf-test-tenant")
        b = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="wf-idem-1", tenant_id="wf-test-tenant")
        self.assertEqual(a["workflow_id"], b["workflow_id"])
        self.assertEqual(a["queue_task_id"], b["queue_task_id"])
        self.assertEqual(len(list(bundle.state_manager._store.list_all())), 1)
        self.assertEqual(len(list(bundle.queue.store.list_all())), 1)

    async def test_duplicate_after_restart_same_workflow(self):
        store = InMemoryWorkflowStateStore()
        sm = StateManager(store=store, step_names=())
        bundle = build_workflow_runtime(state_manager=sm)
        bundle.definitions.register(linear_demo_definition())
        first = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="wf-idem-restart", tenant_id="wf-test-tenant")
        bundle2 = build_workflow_runtime(state_manager=StateManager(store=store, step_names=()))
        bundle2.definitions.register(linear_demo_definition())
        second = await bundle2.create_and_enqueue(
            "demo.linear", "1", execution_key="wf-idem-restart", tenant_id="wf-test-tenant")
        self.assertEqual(first["workflow_id"], second["workflow_id"])
        self.assertEqual(len(list(store.list_all())), 1)

    async def test_different_execution_keys_create_different_workflows(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        a = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="wf-a", tenant_id="wf-test-tenant")
        b = await bundle.create_and_enqueue(
            "demo.linear", "1", execution_key="wf-b", tenant_id="wf-test-tenant")
        self.assertNotEqual(a["workflow_id"], b["workflow_id"])
        self.assertEqual(len(list(bundle.state_manager._store.list_all())), 2)


class ApiAnalyzeRegression(unittest.TestCase):
    def test_analyze_route_still_present(self):
        from tests.test_smoke import load_app

        main_mod = load_app()
        paths = {getattr(r, "path", None) for r in main_mod.app.routes}
        self.assertIn("/api/analyze", paths)
        self.assertIn("/api/workflows", paths)


if __name__ == "__main__":
    unittest.main()
