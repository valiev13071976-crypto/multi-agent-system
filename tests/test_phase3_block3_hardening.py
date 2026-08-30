"""Phase 3 Block 3 — config validation, readiness, metrics, backup, harness."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from config.runtime_config import (
    PROFILE_DEVELOPMENT,
    RuntimeConfigError,
    apply_profile_defaults,
    validate_runtime_config,
)
from config.runtime_health import (
    DRAIN,
    STATUS_HEALTHY,
    STATUS_NOT_READY,
    begin_api_drain,
    evaluate_readiness,
)
from harness.load_harness import run_all, scenario_interactive_baseline
from observability.runtime_metrics import (
    RUNTIME_METRICS,
    collect_operational_metrics,
    collect_queue_snapshot,
)
from side_effects.persistence import build_side_effect_persistence
from side_effects.runtime import compose_side_effect_runtime
from task_queue.lanes import LANE_INTERACTIVE
from task_queue.queue import TaskQueue
from tests.test_github_write_config import DictSecrets
from workflow.admission import AdmissionController, AdmissionRejectedError
from workflow.definition import STEP_TYPE_HANDLER, StepResult
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager
from tests.test_workflow_foundation import linear_demo_definition


class RuntimeConfigValidationTests(unittest.TestCase):
    def test_profile_defaults_development(self):
        merged = apply_profile_defaults({"PANDA_RUNTIME_PROFILE": "development"})
        self.assertEqual(merged["RUNTIME_ROLE"], "combined")
        self.assertEqual(merged["PANDA_RUNTIME_PROFILE"], PROFILE_DEVELOPMENT)

    def test_heartbeat_gte_lease_invalid(self):
        with self.assertRaises(RuntimeConfigError) as ctx:
            validate_runtime_config(
                {
                    "WORKER_LEASE_SECONDS": "30",
                    "WORKER_HEARTBEAT_INTERVAL_SECONDS": "30",
                },
                raise_on_error=True,
            )
        self.assertTrue(
            any("heartbeat_interval_gte_lease_duration" in e for e in ctx.exception.errors)
        )

    def test_interactive_reserved_gt_global_invalid(self):
        with self.assertRaises(RuntimeConfigError):
            validate_runtime_config(
                {
                    "MAX_RUNNING_GLOBAL": "5",
                    "INTERACTIVE_RESERVED": "10",
                },
                raise_on_error=True,
            )

    def test_per_tenant_gt_global_running_invalid(self):
        with self.assertRaises(RuntimeConfigError):
            validate_runtime_config(
                {
                    "MAX_RUNNING_GLOBAL": "10",
                    "MAX_RUNNING_PER_TENANT": "20",
                    "INTERACTIVE_RESERVED": "2",
                },
                raise_on_error=True,
            )

    def test_invalid_runtime_role(self):
        with self.assertRaises(RuntimeConfigError):
            validate_runtime_config(
                {"RUNTIME_ROLE": "spaceship"},
                raise_on_error=True,
            )

    def test_valid_defaults(self):
        cfg = validate_runtime_config({}, raise_on_error=True)
        self.assertTrue(cfg.valid)
        self.assertGreater(cfg.lease_seconds, cfg.heartbeat_interval_seconds)


class ReadinessDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DRAIN.clear_drain()

    async def asyncTearDown(self):
        DRAIN.clear_drain()

    async def test_api_readiness_and_drain_blocks_admission(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "ready.sqlite3")
            env = {
                "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                "SIDE_EFFECT_DB_PATH": path,
                "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                "RUNTIME_ROLE": "api",
                "INTEGRATION_SECRETS_BACKEND": "memory",
            }
            runtime = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                snap = evaluate_readiness(
                    side_effect_runtime=runtime,
                    env=env,
                )
                self.assertEqual(snap.liveness, STATUS_HEALTHY)
                self.assertIn(snap.readiness, {STATUS_HEALTHY, "degraded"})
                begin_api_drain()
                self.assertTrue(DRAIN.draining)
                snap2 = evaluate_readiness(side_effect_runtime=runtime, env=env)
                self.assertEqual(snap2.readiness, STATUS_NOT_READY)
                adm = AdmissionController()
                with self.assertRaises(AdmissionRejectedError) as ctx:
                    adm.require_enqueue(
                        runtime.workflow_runtime.queue,
                        tenant_id="tenant-A",
                        priority="normal",
                    )
                self.assertEqual(ctx.exception.reason, "api_draining")
            finally:
                runtime.close()
                DRAIN.clear_drain()

    async def test_worker_stop_new_claims(self):
        runtime = build_workflow_runtime(runtime_role="worker")
        runtime.stop_new_claims()
        launched = await runtime.tick_schedules()
        self.assertEqual(launched, [])


class MetricsTests(unittest.TestCase):
    def test_queue_snapshot_and_operational(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "met.sqlite3")
            bundle = build_side_effect_persistence(
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                },
                durable=True,
                run_recovery_scan=False,
            )
            q = TaskQueue(store=bundle.task_queue_store)
            q.enqueue(
                workflow_id="w",
                task_id="t",
                execution_key="ek",
                tenant_id="tenant-A",
                execution_lane=LANE_INTERACTIVE,
                priority="high",
            )
            snap = collect_queue_snapshot(q)
            self.assertGreaterEqual(snap["pending_global"], 1)
            self.assertIn("interactive", snap["queue_depth_by_lane"])
            # Tenant ids anonymized
            for key in snap["pending_by_tenant"]:
                self.assertTrue(str(key).startswith("t:"))
            RUNTIME_METRICS.record_admission("ACCEPT", lane=LANE_INTERACTIVE)
            ops = collect_operational_metrics()
            self.assertIn("interactive_slo", ops)
            if bundle.connection is not None:
                bundle.connection.close()


class BackupRestoreSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_restore_preserves_workflow_and_queue(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            src = Path(tmp) / "live.sqlite3"
            bak = Path(tmp) / "backup.sqlite3"
            env = {
                "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                "SIDE_EFFECT_DB_PATH": str(src),
                "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                "INTEGRATION_SECRETS_BACKEND": "memory",
                "RUNTIME_ROLE": "combined",
            }
            runtime = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                wr = runtime.workflow_runtime
                wr.definitions.register(linear_demo_definition())
                wr.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )
                created = await wr.create_and_enqueue(
                    "demo.linear",
                    "1",
                    tenant_id="tenant-A",
                    execution_key="backup-ek-1",
                    execution_lane=LANE_INTERACTIVE,
                    priority="high",
                )
                wid = created["workflow_id"]
                state = wr.state_manager.get(wid)
                self.assertEqual(getattr(state, "tenant_id", None) or "tenant-A", "tenant-A")
            finally:
                await runtime.workflow_runtime.stop_background()
                runtime.close()

            shutil.copy2(src, bak)
            # Recreate from backup path
            env2 = {**env, "SIDE_EFFECT_DB_PATH": str(bak)}
            runtime2 = compose_side_effect_runtime(secrets=DictSecrets(), env=env2)
            try:
                wr2 = runtime2.workflow_runtime
                restored = wr2.state_manager.get(wid)
                self.assertEqual(restored.workflow_id, wid)
                # Queue row coherent
                tasks = [
                    t
                    for t in wr2.queue.store.list_all()
                    if t.workflow_id == wid
                ]
                self.assertTrue(tasks)
                self.assertEqual(tasks[0].tenant_id, "tenant-A")
            finally:
                await runtime2.workflow_runtime.stop_background()
                runtime2.close()


class HarnessSmokeTests(unittest.TestCase):
    def test_interactive_baseline_scenario(self):
        result = scenario_interactive_baseline(n=10)
        self.assertTrue(result.ok, result.notes)

    def test_all_scenarios(self):
        results = run_all()
        failed = [r.name for r in results if not r.ok]
        self.assertEqual(failed, [], failed)


class StartupOrderIndependenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_then_worker_and_worker_then_api(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "order.sqlite3")
            base = {
                "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                "SIDE_EFFECT_DB_PATH": path,
                "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                "INTEGRATION_SECRETS_BACKEND": "memory",
            }
            api = compose_side_effect_runtime(
                secrets=DictSecrets(), env={**base, "RUNTIME_ROLE": "api"}
            )
            try:
                wr = api.workflow_runtime
                wr.definitions.register(linear_demo_definition())
                wr.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )
                created = await wr.create_and_enqueue(
                    "demo.linear",
                    "1",
                    tenant_id="tenant-A",
                    execution_key="order-ek",
                )
            finally:
                api.close()

            worker = compose_side_effect_runtime(
                secrets=DictSecrets(), env={**base, "RUNTIME_ROLE": "worker"}
            )
            try:
                wr = worker.workflow_runtime
                wr.definitions.register(linear_demo_definition())
                wr.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )
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


if __name__ == "__main__":
    unittest.main()
