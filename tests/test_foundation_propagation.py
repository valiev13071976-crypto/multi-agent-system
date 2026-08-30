"""P0 Foundation: request/tenant/actor/cost/queue identity propagation."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# Import workflow before task_queue to avoid package circular import.
from workflow.engine import WorkflowEngine
from workflow.state_manager import StateManager

from agents.provider_result import ProviderResult
from finops.models import UsageRecord
from finops.service import FinOpsService
from observability.runtime import build_observability_runtime
from security.tenant import MissingTenantError, require_tenant_id
from task_queue.queue import TaskQueue
from task_queue.worker import ExecutionContext, TaskWorker
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import load_app
from agents.core.expert_manager import ExpertManager


class RequireTenantTests(unittest.TestCase):
    def test_blank_tenant_fail_closed(self):
        with self.assertRaises(MissingTenantError):
            require_tenant_id(None)
        with self.assertRaises(MissingTenantError):
            require_tenant_id("")
        with self.assertRaises(MissingTenantError):
            require_tenant_id("   ")

    def test_explicit_tenant_accepted(self):
        self.assertEqual(require_tenant_id("tenant-a"), "tenant-a")
        # Explicit legacy string is intentional (auth-disabled default), not silent collapse.
        self.assertEqual(require_tenant_id("legacy-default"), "legacy-default")


class FoundationPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_id_binds_observability_correlation(self):
        obs = build_observability_runtime(env={})
        engine = WorkflowEngine(observability=obs)
        request_id = "req-corr-1"
        task_id = "task-1"

        class CM:
            async def prepare(self, prompt, **kwargs):
                return prompt

        async def fake_router(**kwargs):
            self.assertEqual(kwargs.get("request_id"), request_id)
            self.assertEqual(kwargs.get("tenant_id"), "tenant-a")
            self.assertEqual(kwargs.get("user_id"), "user-a")
            return {"summary": "ok", "role": "Judge"}

        await engine.execute(
            "hello",
            "openai",
            "strategist",
            context_manager=CM(),
            run_router=fake_router,
            task_id=task_id,
            tenant_id="tenant-a",
            request_id=request_id,
            user_id="user-a",
            actor_ref="tenant-a:user-a",
        )
        state = engine.state_manager.get(engine.last_workflow_id)
        self.assertEqual(state.request_id, request_id)
        self.assertEqual(state.tenant_id, "tenant-a")
        self.assertEqual(state.user_id, "user-a")
        ctx = obs.context_for_workflow(engine.last_workflow_id)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.correlation_id, request_id)
        self.assertEqual(ctx.tenant_id, "tenant-a")
        self.assertEqual(ctx.actor_ref, "tenant-a:user-a")
        events = [e for e in obs.list_events() if e.workflow_id == engine.last_workflow_id]
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event.correlation_id, request_id)
            self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-a")
            self.assertEqual(event.metadata_safe.get("request_id"), request_id)

    async def test_execute_missing_tenant_fail_closed(self):
        engine = WorkflowEngine()

        async def fake_prepare(self, prompt, **kwargs):
            return prompt

        class CM:
            prepare = fake_prepare

        with self.assertRaises(MissingTenantError):
            await engine.execute(
                "hello",
                "openai",
                "strategist",
                context_manager=CM(),
                run_router=AsyncMock(),
                task_id="t",
                tenant_id=None,
            )

    async def test_tenants_do_not_mix_on_usage_path(self):
        finops = FinOpsService()
        manager = ExpertManager(openai=object(), finops=finops)
        manager.last_task_id = "task-a"
        manager.last_workflow_id = "wf-a"
        manager.last_tenant_id = "tenant-a"
        manager.last_user_id = "user-a"
        manager.last_request_id = "req-a"
        result = ProviderResult(
            text="ok",
            provider_id="openai",
            model_id="m",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            raw_usage={},
        )
        with patch.object(finops, "estimate", return_value=Decimal("0.01")):
            with patch.object(finops, "quote", return_value=None):
                record = manager._record_usage(result)
        self.assertIsInstance(record, UsageRecord)
        self.assertEqual(record.tenant_id, "tenant-a")
        self.assertEqual(record.workflow_id, "wf-a")
        self.assertEqual(record.request_id, "req-a")
        self.assertEqual(record.user_id, "user-a")
        self.assertEqual(record.task_id, "task-a")

        manager.last_tenant_id = "tenant-b"
        manager.last_workflow_id = "wf-b"
        manager.last_request_id = "req-b"
        manager.last_user_id = "user-b"
        manager.last_task_id = "task-b"
        with patch.object(finops, "estimate", return_value=Decimal("0.02")):
            with patch.object(finops, "quote", return_value=None):
                record_b = manager._record_usage(result)
        self.assertEqual(record_b.tenant_id, "tenant-b")
        self.assertNotEqual(record.tenant_id, record_b.tenant_id)
        stored = finops._store.records()
        self.assertEqual(len(stored), 2)
        self.assertEqual({r.tenant_id for r in stored}, {"tenant-a", "tenant-b"})


class AnalyzePropagationHttpTests(unittest.TestCase):
    def test_analyze_propagates_identity_into_usage(self):
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
            SECURITY_AUTH_MODE="disabled",
            SECURITY_DEFAULT_TENANT="tenant-http-a",
        )
        obs = build_observability_runtime(env={})
        main_mod.router.workflow_engine.observability = obs
        manager = main_mod.router.pipeline.expert_manager
        provider_result = ProviderResult(
            text="successful strategist answer",
            provider_id="openai",
            model_id="fake-model",
            input_tokens=3,
            output_tokens=7,
            total_tokens=10,
            raw_usage={},
        )
        with patch.object(manager.openai, "run", new=AsyncMock(return_value=provider_result)):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        usage = manager.last_usage
        self.assertTrue(usage)
        record = usage[0]
        self.assertEqual(record.tenant_id, "tenant-http-a")
        self.assertTrue(record.workflow_id)
        self.assertTrue(record.request_id)
        self.assertEqual(record.task_id, main_mod.router.last_task_id)
        self.assertEqual(record.workflow_id, main_mod.router.last_workflow_id)
        self.assertEqual(main_mod.router.last_request_id, record.request_id)
        ctx = obs.context_for_workflow(record.workflow_id)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.correlation_id, record.request_id)
        self.assertEqual(ctx.tenant_id, "tenant-http-a")


class QueueIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_execution_context_carries_tenant(self):
        queue = TaskQueue()
        seen = {}

        async def handler(ctx: ExecutionContext):
            seen["tenant_id"] = ctx.tenant_id
            seen["user_id"] = ctx.user_id
            seen["actor_ref"] = ctx.actor_ref

        worker = TaskWorker(queue, handler=handler)
        queue.enqueue(
            workflow_id="wf-1",
            task_id="t-1",
            execution_key="ek-a",
            tenant_id="tenant-a",
            user_id="user-a",
            actor_ref="tenant-a:user-a",
        )
        result = await worker.run_once()
        self.assertEqual(result.status, "completed")
        self.assertEqual(seen["tenant_id"], "tenant-a")
        self.assertEqual(seen["user_id"], "user-a")
        self.assertEqual(seen["actor_ref"], "tenant-a:user-a")

    async def test_worker_tenants_isolated(self):
        queue = TaskQueue()
        seen = []

        async def handler(ctx: ExecutionContext):
            seen.append(ctx.tenant_id)

        worker = TaskWorker(queue, handler=handler)
        queue.enqueue(
            workflow_id="wf-a",
            task_id="t-a",
            execution_key="ek-a",
            tenant_id="tenant-a",
        )
        queue.enqueue(
            workflow_id="wf-b",
            task_id="t-b",
            execution_key="ek-b",
            tenant_id="tenant-b",
        )
        await worker.run_once()
        await worker.run_once()
        self.assertEqual(set(seen), {"tenant-a", "tenant-b"})

    async def test_enqueue_from_workflow_state_identity(self):
        manager = StateManager()
        state = manager.create(
            task_id="task-q",
            tenant_id="tenant-q",
            user_id="user-q",
            actor_ref="tenant-q:user-q",
            request_id="req-q",
            step_names=("s1",),
            workflow_type="demo",
            definition_version="1",
        )
        manager.plan(state.workflow_id)
        queue = TaskQueue()
        from security.tenant import scope_execution_key, workflow_tenant_id

        tenant = workflow_tenant_id(state)
        scoped = scope_execution_key(tenant, state.execution_key)
        queue.enqueue(
            workflow_id=state.workflow_id,
            task_id=state.task_id,
            execution_key=scoped,
            tenant_id=tenant,
            user_id=state.user_id or "",
            actor_ref=state.actor_ref or "",
        )
        ctx_holder = {}

        async def capture(ctx):
            ctx_holder["ctx"] = ctx

        worker = TaskWorker(queue, handler=capture)
        await worker.run_once()
        self.assertEqual(ctx_holder["ctx"].tenant_id, "tenant-q")
        self.assertEqual(ctx_holder["ctx"].user_id, "user-q")


class ModeSemanticsSmoke(unittest.TestCase):
    def test_explicit_auto_both_still_work(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            self.assertEqual(
                client.post(
                    "/api/analyze",
                    json={"prompt": "task", "mode": "openai"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/analyze",
                    json={"prompt": "task", "mode": "both"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/analyze",
                    json={"prompt": "task", "mode": "auto"},
                ).status_code,
                200,
            )


if __name__ == "__main__":
    unittest.main()
