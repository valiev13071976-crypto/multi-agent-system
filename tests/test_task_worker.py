import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from fastapi.testclient import TestClient

from agents.model_router import REASON_ALL_AVAILABLE_PROVIDERS, ModelRouter
from finops.models import CURRENCY_USD, UsageRecord
from task_queue.errors import QueueTimeoutError
from task_queue.models import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_QUEUED
from task_queue.queue import TaskQueue
from task_queue.retry import RetryPolicy
from task_queue.store import InMemoryTaskQueueStore
from task_queue.worker import MODE_BOTH_BUDGET_LIMITATION, TaskWorker, WorkerConfig
from tests.test_mode_auto import STRATEGY_TEXT, load_auto_app
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_model_router import registry_with
from tests.test_smoke import CONTRACT_KEYS, load_app
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_READ_ONLY_EXTERNAL
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_WAITING_APPROVAL
from workflow.state_manager import StateManager


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class CodedError(Exception):
    def __init__(self, code: str):
        self.error_code = code
        super().__init__(code)


class TaskWorkerTests(unittest.IsolatedAsyncioTestCase):

    def _queue(self, **policy):
        return TaskQueue(
            InMemoryTaskQueueStore(),
            retry_policy=RetryPolicy(**policy) if policy else RetryPolicy(),
        )

    async def test_f_worker_completes(self):
        ran = []

        async def handler(ctx):
            ran.append(ctx.task_id)

        queue = self._queue()
        queue.enqueue(workflow_id="wf", task_id="t1", execution_key="ek")
        worker = TaskWorker(queue, handler)
        result = await worker.run_once()
        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(ran, ["t1"])

    async def test_n_timeout_dead_letters_when_no_retry(self):
        async def slow(ctx):
            await asyncio.sleep(1)

        queue = self._queue()
        queue.enqueue(
            workflow_id="wf",
            task_id="t1",
            execution_key="ek",
            timeout_seconds=0.01,
        )
        worker = TaskWorker(queue, slow)
        result = await worker.run_once()
        self.assertEqual(result.status, "dead_lettered")
        self.assertEqual(result.error_code, "execution_timeout")

    async def test_n_timeout_retries_when_policy_allows(self):
        async def slow(ctx):
            await asyncio.sleep(1)

        queue = self._queue(max_attempts=2, base_delay_seconds=5)
        queue.enqueue(
            workflow_id="wf",
            task_id="t1",
            execution_key="ek",
            timeout_seconds=0.01,
            max_attempts=2,
        )
        worker = TaskWorker(queue, slow)
        result = await worker.run_once()
        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.error_code, "execution_timeout")

    async def test_o_default_policy_does_not_run_twice(self):
        calls = []

        async def boom(ctx):
            calls.append(1)
            raise CodedError("execution_timeout")

        queue = self._queue()
        queue.enqueue(workflow_id="wf", task_id="t1", execution_key="ek")
        worker = TaskWorker(queue, boom)
        await worker.run_once()
        second = await worker.run_once()
        self.assertIsNone(second)
        self.assertEqual(calls, [1])

    async def test_u_cancelled_workflow_does_not_run_handler(self):
        ran = []

        async def handler(ctx):
            ran.append("ran")

        engine = WorkflowEngine()
        workflow_id = engine.create("task-u")
        engine.state_manager.cancel(workflow_id)
        queue = self._queue()
        queue.enqueue(
            workflow_id=workflow_id,
            task_id="task-u",
            execution_key="ek-u",
        )
        worker = TaskWorker(queue, handler, engine=engine)
        result = await worker.run_once()
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(ran, [])

    async def test_v_waiting_approval_does_not_run_protected_handler(self):
        ran = []

        async def handler(ctx):
            ran.append("ran")

        engine = WorkflowEngine()
        workflow_id = engine.create("task-v")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        engine.state_manager.wait_for_approval(workflow_id)
        self.assertEqual(
            engine.state_manager.get(workflow_id).status,
            STATUS_WAITING_APPROVAL,
        )
        queue = self._queue()
        queue.enqueue(
            workflow_id=workflow_id,
            task_id="task-v",
            execution_key="ek-v",
        )
        worker = TaskWorker(queue, handler, engine=engine)
        result = await worker.run_once()
        self.assertEqual(result.status, STATUS_QUEUED)
        self.assertEqual(result.metadata.get("requeue_reason"), "workflow_waiting_approval")
        self.assertEqual(ran, [])
        self.assertIsNone(await worker.run_once())

    async def test_w_completed_workflow_does_not_execute(self):
        ran = []

        async def handler(ctx):
            ran.append("ran")

        engine = WorkflowEngine()
        workflow_id = engine.create("task-w")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        engine.state_manager.complete_workflow(workflow_id)
        queue = self._queue()
        queue.enqueue(
            workflow_id=workflow_id,
            task_id="task-w",
            execution_key="ek-w",
        )
        worker = TaskWorker(queue, handler, engine=engine)
        result = await worker.run_once()
        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(result.metadata.get("skipped_reason"), "workflow_completed")
        self.assertEqual(ran, [])

    async def test_x_resume_skips_completed_step(self):
        manager = StateManager(step_names=("one", "two"))
        engine = WorkflowEngine(state_manager=manager, step_names=("one", "two"))
        state = manager.create(task_id="task-x")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.start_step(state.workflow_id, "one")
        manager.complete_step(state.workflow_id, "one")
        ran = []

        async def one():
            ran.append("one")

        async def two():
            ran.append("two")

        async def handler(ctx):
            await engine.resume(
                ctx.workflow_id,
                handlers={"one": one, "two": two},
            )

        queue = self._queue()
        queue.enqueue(
            workflow_id=state.workflow_id,
            task_id="task-x",
            execution_key=state.execution_key,
        )
        worker = TaskWorker(queue, handler, engine=engine)
        result = await worker.run_once()
        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(ran, ["two"])

    async def test_y_task_id_reaches_finops_record(self):
        records = []

        async def handler(ctx):
            records.append(
                UsageRecord(
                    task_id=ctx.task_id,
                    provider_id="openai",
                    model_id="m",
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    estimated_cost=Decimal("0.01"),
                    currency=CURRENCY_USD,
                    timestamp=T0,
                )
            )

        engine = WorkflowEngine()
        task_id = "canonical-finops-task"
        workflow_id = engine.create(task_id)
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        queue = self._queue()
        queue.enqueue(
            workflow_id=workflow_id,
            task_id=task_id,
            execution_key="ek-y",
        )
        worker = TaskWorker(queue, handler, engine=engine)
        await worker.run_once()
        self.assertEqual(records[0].task_id, task_id)
        self.assertEqual(engine.last_task_id, task_id)

    async def test_l_finops_denied_dead_letters(self):
        async def handler(ctx):
            raise CodedError("finops_budget_denied")

        queue = self._queue(max_attempts=4)
        queue.enqueue(
            workflow_id="wf",
            task_id="t",
            execution_key="ek",
            max_attempts=4,
        )
        worker = TaskWorker(queue, handler)
        result = await worker.run_once()
        self.assertEqual(result.status, "dead_lettered")
        self.assertEqual(result.error_code, "finops_budget_denied")

    def test_ab_tool_gateway_trust_unchanged(self):
        gateway = ToolGateway()
        self.assertEqual(gateway.tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)

    def test_ac_analyze_contract_unchanged(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(len(payload), 7)
        self.assertEqual(payload["role"], "Judge")
        self.assertNotIn("workflow_id", payload)
        self.assertNotIn("queue_task_id", payload)

    def test_ad_http_400_preserved(self):
        main_mod = load_app(**env_for("openai"))
        client = TestClient(main_mod.app)
        response = client.post(
            "/api/analyze",
            json={"prompt": "Найди поставщика", "mode": "not-a-provider"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error"], "invalid_mode")

    def test_ae_http_429_preserved(self):
        overrides = env_for("openai")
        overrides["FINOPS_UNKNOWN_COST_POLICY"] = "deny"
        main_mod = load_app(**overrides)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(response.json()["detail"]["error"], "finops_budget_denied")

    def test_af_http_503_preserved(self):
        main_mod = load_app()
        client = TestClient(main_mod.app)
        response = client.post(
            "/api/analyze",
            json={"prompt": "test", "mode": "both"},
        )
        self.assertEqual(response.status_code, 503)

    def test_ag_mode_both_unchanged(self):
        router = ModelRouter(registry_with("openai", "anthropic"))
        decision = router.decide(mode="both", role_id="strategist")
        self.assertEqual(decision.provider_ids, ("openai", "anthropic"))
        self.assertEqual(decision.reason, REASON_ALL_AVAILABLE_PROVIDERS)
        self.assertIn("concurrent", MODE_BOTH_BUDGET_LIMITATION)
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "both"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 7)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)

    def test_ah_mode_auto_unchanged(self):
        main_mod = load_auto_app("anthropic", "openai", auto_order="anthropic,openai")
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "anthropic", "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "Judge")
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)

    def test_worker_config_default_concurrency(self):
        self.assertEqual(WorkerConfig().max_concurrency, 1)

    def test_timeout_error_is_machine_safe(self):
        self.assertEqual(str(QueueTimeoutError()), "execution_timeout")


if __name__ == "__main__":
    unittest.main()
