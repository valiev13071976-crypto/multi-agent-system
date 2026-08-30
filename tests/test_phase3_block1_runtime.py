"""Phase 3 Block 1 — roles, admission, scheduler claim, shared budget, limits."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from finops.budget_guard import BudgetGuard, BudgetGuardError
from finops.budget_models import SCOPE_GLOBAL, BudgetPolicy
from finops.budget_store import SqliteBudgetStore
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService
from side_effects.persistence import build_side_effect_persistence
from side_effects.runtime import compose_side_effect_runtime
from task_queue.queue import TaskQueue
from tests.test_github_write_config import DictSecrets
from tests.test_workflow_foundation import linear_demo_definition
from workflow.admission import AdmissionRejectedError
from workflow.definition import STEP_TYPE_HANDLER, ScheduleSpec, StepResult
from workflow.models import utc_now
from workflow.runtime_role import ROLE_API, ROLE_COMBINED, ROLE_WORKER, resolve_runtime_role
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager


def _env(path: str, **extra) -> dict:
    base = {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
        "INTEGRATION_SECRETS_BACKEND": "memory",
        "MAX_PENDING_GLOBAL": "50",
        "MAX_PENDING_PER_TENANT": "5",
        "MAX_RUNNING_GLOBAL": "3",
        "MAX_RUNNING_PER_TENANT": "2",
    }
    base.update(extra)
    return base


class RuntimeRoleTests(unittest.TestCase):
    def test_resolve_roles(self):
        self.assertEqual(resolve_runtime_role({"RUNTIME_ROLE": "api"}), ROLE_API)
        self.assertEqual(resolve_runtime_role({"RUNTIME_ROLE": "worker"}), ROLE_WORKER)
        self.assertEqual(resolve_runtime_role({}), ROLE_COMBINED)
        self.assertEqual(
            resolve_runtime_role({"WORKFLOW_WORKER_ENABLED": "false"}), ROLE_API
        )


class ApiRoleNoWorkerLoopsTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_role_skips_recovery_and_background(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "api.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(path, RUNTIME_ROLE="api"),
            )
            try:
                wr = runtime.workflow_runtime
                self.assertIsNotNone(wr)
                self.assertEqual(wr.runtime_role, ROLE_API)
                self.assertIsNone(wr.last_startup_recovery_result)
                await wr.start_background()
                self.assertIsNone(wr._worker_task)
            finally:
                runtime.close()


class ApiToWorkerSplitTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_enqueue_worker_executes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "split.sqlite3")
            env = _env(path)
            created = None
            api = compose_side_effect_runtime(
                secrets=DictSecrets(), env={**env, "RUNTIME_ROLE": "api"}
            )
            try:
                wr_api = api.workflow_runtime
                wr_api.definitions.register(linear_demo_definition())
                wr_api.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )
                created = await wr_api.create_and_enqueue(
                    "demo.linear",
                    "1",
                    tenant_id="tenant-A",
                    execution_key="split-ek-1",
                )
                self.assertTrue(created.get("queue_task_id"))
            finally:
                await api.workflow_runtime.stop_background()
                api.close()

            worker = compose_side_effect_runtime(
                secrets=DictSecrets(), env={**env, "RUNTIME_ROLE": "worker"}
            )
            try:
                wr = worker.workflow_runtime
                wr.definitions.register(linear_demo_definition())
                wr.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )
                # Worker startup recovery rebinds handlers for durable queued work.
                if wr.last_startup_recovery_result is None:
                    wr.recover_and_reenqueue_persisted()
                for _ in range(12):
                    if wr.state_manager.get(created["workflow_id"]).status == "completed":
                        break
                    await wr.worker.run_once()
                self.assertEqual(
                    wr.state_manager.get(created["workflow_id"]).status, "completed"
                )
            finally:
                await worker.workflow_runtime.stop_background()
                worker.close()


class ConcurrentApiEnqueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_api_replicas_concurrent_enqueue(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "api2.sqlite3")
            env = _env(path, MAX_PENDING_PER_TENANT="20", MAX_PENDING_GLOBAL="50")
            bundle = build_side_effect_persistence(
                env=env, durable=True, run_recovery_scan=False
            )
            a = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                task_queue_store=bundle.task_queue_store,
                env={**env, "RUNTIME_ROLE": "api"},
                runtime_role=ROLE_API,
            )
            b = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                task_queue_store=bundle.task_queue_store,
                env={**env, "RUNTIME_ROLE": "api"},
                runtime_role=ROLE_API,
            )
            for r in (a, b):
                r.definitions.register(linear_demo_definition())
                r.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )

            async def one(runtime, i: int):
                return await runtime.create_and_enqueue(
                    "demo.linear",
                    "1",
                    tenant_id="tenant-A",
                    execution_key=f"api-conc-{i}",
                )

            results = []
            results.append(await one(a, 0))
            results.append(await one(b, 1))
            results.append(await one(a, 2))
            results.append(await one(b, 3))
            self.assertEqual(len({r["workflow_id"] for r in results}), 4)
            pending = [
                t
                for t in bundle.task_queue_store.list_all()
                if t.status in {"queued", "retry_wait"}
            ]
            self.assertEqual(len(pending), 4)
            if bundle.connection is not None:
                bundle.connection.close()


class AdmissionLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_tenant_pending_limit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "adm.sqlite3")
            env = _env(path, MAX_PENDING_PER_TENANT="2")
            bundle = build_side_effect_persistence(
                env=env,
                durable=True,
                run_recovery_scan=False,
            )
            runtime = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                task_queue_store=bundle.task_queue_store,
                env=env,
            )
            runtime.definitions.register(linear_demo_definition())
            runtime.platform.register_handler(
                STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
            )
            await runtime.create_and_enqueue(
                "demo.linear", "1", tenant_id="tA", execution_key="p1"
            )
            await runtime.create_and_enqueue(
                "demo.linear", "1", tenant_id="tA", execution_key="p2"
            )
            with self.assertRaises(AdmissionRejectedError):
                await runtime.create_and_enqueue(
                    "demo.linear", "1", tenant_id="tA", execution_key="p3"
                )
            # Other tenant still ok — isolation
            await runtime.create_and_enqueue(
                "demo.linear", "1", tenant_id="tB", execution_key="pb1"
            )
            if bundle.connection is not None:
                bundle.connection.close()

    async def test_global_pending_limit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "gpend.sqlite3")
            env = _env(path, MAX_PENDING_GLOBAL="2", MAX_PENDING_PER_TENANT="10")
            bundle = build_side_effect_persistence(
                env=env, durable=True, run_recovery_scan=False
            )
            runtime = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                task_queue_store=bundle.task_queue_store,
                env=env,
            )
            runtime.definitions.register(linear_demo_definition())
            runtime.platform.register_handler(
                STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
            )
            await runtime.create_and_enqueue(
                "demo.linear", "1", tenant_id="tA", execution_key="g1"
            )
            await runtime.create_and_enqueue(
                "demo.linear", "1", tenant_id="tB", execution_key="g2"
            )
            with self.assertRaises(AdmissionRejectedError):
                await runtime.create_and_enqueue(
                    "demo.linear", "1", tenant_id="tC", execution_key="g3"
                )
            if bundle.connection is not None:
                bundle.connection.close()


class RunningLimitTests(unittest.TestCase):
    def test_global_and_tenant_running_limits_race_safe(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "runlim.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path, MAX_RUNNING_GLOBAL="2", MAX_RUNNING_PER_TENANT="1"),
                durable=True,
                run_recovery_scan=False,
            )
            store = bundle.task_queue_store
            q = TaskQueue(
                store=store,
                lease_seconds=60,
                admission_limits=None,
            )
            for i in range(4):
                q.enqueue(
                    workflow_id=f"wf-{i}",
                    task_id=f"t-{i}",
                    execution_key=f"ek-{i}",
                    tenant_id="tenant-A" if i < 3 else "tenant-B",
                )
            a = q.dequeue(
                worker_id="wa", max_running_global=2, max_running_per_tenant=1
            )
            self.assertIsNotNone(a)
            # Same tenant cannot get second running lease
            b = q.dequeue(
                worker_id="wb", max_running_global=2, max_running_per_tenant=1
            )
            self.assertIsNotNone(b)
            self.assertNotEqual(a.tenant_id, b.tenant_id)
            # Global full
            c = q.dequeue(
                worker_id="wc", max_running_global=2, max_running_per_tenant=1
            )
            self.assertIsNone(c)
            if bundle.connection is not None:
                bundle.connection.close()


class SchedulerClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_workers_one_schedule_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "sched.sqlite3")
            env = _env(path)
            bundle = build_side_effect_persistence(
                env=env, durable=True, run_recovery_scan=False
            )
            r1 = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                schedule_store=bundle.schedule_store,
                task_queue_store=bundle.task_queue_store,
                env=env,
            )
            r2 = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                schedule_store=bundle.schedule_store,
                task_queue_store=bundle.task_queue_store,
                env=env,
            )
            for r in (r1, r2):
                r.definitions.register(linear_demo_definition())
                r.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )
            due = utc_now() - timedelta(seconds=1)
            r1.register_schedule(
                ScheduleSpec(
                    schedule_id="shared-sched",
                    workflow_type="demo.linear",
                    version="1",
                    payload={"tenant_id": "tenant-A"},
                    run_at=due,
                    interval_seconds=3600,
                )
            )
            launched = []

            async def tick(runtime):
                launched.extend(await runtime.tick_schedules())

            await tick(r1)
            await tick(r2)
            self.assertEqual(len(set(launched)), 1)
            if bundle.connection is not None:
                bundle.connection.close()

    async def test_schedule_claim_crash_window_recoverable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "crash.sqlite3")
            env = _env(path)
            bundle = build_side_effect_persistence(
                env=env, durable=True, run_recovery_scan=False
            )
            runtime = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                schedule_store=bundle.schedule_store,
                task_queue_store=bundle.task_queue_store,
                env=env,
            )
            runtime.definitions.register(linear_demo_definition())
            runtime.platform.register_handler(
                STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
            )
            due = utc_now() - timedelta(seconds=2)
            runtime.register_schedule(
                ScheduleSpec(
                    schedule_id="crash-sched",
                    workflow_type="demo.linear",
                    version="1",
                    payload={"tenant_id": "tenant-A"},
                    run_at=due,
                    interval_seconds=3600,
                )
            )
            claim = bundle.schedule_store.claim_due_window(
                "crash-sched",
                expected_next_run_at=due,
                now=utc_now(),
                lease_seconds=1,
            )
            self.assertIsNotNone(claim)
            # Expire claim without enqueue
            time.sleep(1.1)
            launched = await runtime.tick_schedules()
            self.assertEqual(len(launched), 1)
            if bundle.connection is not None:
                bundle.connection.close()


class SharedBudgetRaceTests(unittest.TestCase):
    def test_concurrent_reserve_cannot_overspend(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "budget.sqlite3")
            store = SqliteBudgetStore(path)
            finops = FinOpsService(
                prices={
                    ("openai", "m"): PriceQuote(
                        "openai", "m", Decimal("1"), Decimal("1"), "USD", True
                    )
                },
                limits=BudgetLimits(None, None, None, "allow"),
            )
            policies = (
                BudgetPolicy(
                    policy_id="g1",
                    scope=SCOPE_GLOBAL,
                    hard_limit=Decimal("10"),
                ),
            )
            errors = []
            ok = []
            barrier = threading.Barrier(2)

            def worker(name: str):
                barrier.wait()
                guard = BudgetGuard(
                    finops=finops, policies=policies, store=store, required=True
                )
                try:
                    guard.reserve(
                        task_id=f"task-{name}",
                        provider="openai",
                        model="m",
                        estimated_cost=Decimal("8"),
                    )
                    ok.append(name)
                except BudgetGuardError as exc:
                    errors.append(str(exc.reason))

            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(worker, "A")
                f2 = pool.submit(worker, "B")
                f1.result()
                f2.result()
            self.assertEqual(len(ok), 1)
            self.assertTrue(errors)
            store.close()


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_new_claims(self):
        runtime = build_workflow_runtime(runtime_role=ROLE_COMBINED)
        runtime.stop_new_claims()
        self.assertTrue(runtime._claims_stopped)
        launched = await runtime.tick_schedules()
        self.assertEqual(launched, [])


class WorkerDeathReclaimTests(unittest.TestCase):
    def test_expired_lease_reclaimed_by_another_worker(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "reclaim.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(store=bundle.task_queue_store, lease_seconds=1)
            q.enqueue(
                workflow_id="wf",
                task_id="t",
                execution_key="ek-reclaim",
                tenant_id="tenant-A",
            )
            first = q.dequeue(worker_id="dead-worker", lease_seconds=1)
            self.assertIsNotNone(first)
            time.sleep(1.1)
            second = q.dequeue(worker_id="alive-worker", lease_seconds=30)
            self.assertIsNotNone(second)
            self.assertEqual(second.queue_task_id, first.queue_task_id)
            self.assertEqual(second.worker_id, "alive-worker")
            self.assertNotEqual(second.lease_id, first.lease_id)
            if bundle.connection is not None:
                bundle.connection.close()


if __name__ == "__main__":
    unittest.main()
