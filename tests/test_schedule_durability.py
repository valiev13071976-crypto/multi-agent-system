"""Phase 2 Patch 2 — schedule durability across restart."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import MissingTenantError
from side_effects.persistence import build_side_effect_persistence
from side_effects.runtime import compose_side_effect_runtime
from side_effects.schema import SCHEMA_VERSION
from tests.test_github_write_config import DictSecrets
from tests.test_workflow_foundation import linear_demo_definition
from workflow.definition import STEP_TYPE_HANDLER, ScheduleSpec, StepResult
from workflow.models import utc_now
from workflow.schedule import ScheduleState, WorkflowScheduler
from workflow.schedule_store import PersistentScheduleStore
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager


def _env(path: str, **extra) -> dict:
    base = {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
        "INTEGRATION_SECRETS_BACKEND": "memory",
    }
    base.update(extra)
    return base


class ScheduleDurabilityRestartTests(unittest.TestCase):
    def test_register_survives_store_recreate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sched.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            self.assertTrue(bundle.ready)
            self.assertEqual(bundle.schema_version, SCHEMA_VERSION)
            store = bundle.schedule_store
            self.assertIsInstance(store, PersistentScheduleStore)
            scheduler = WorkflowScheduler(store=store)
            due_at = utc_now() + timedelta(hours=1)
            scheduler.register(
                ScheduleSpec(
                    schedule_id="durable-s1",
                    workflow_type="demo.linear",
                    version="1",
                    payload={"tenant_id": "tenant-A", "k": 1},
                    run_at=due_at,
                    interval_seconds=3600,
                )
            )
            saved = store.get("durable-s1")
            self.assertIsNotNone(saved)
            self.assertEqual(dict(saved.payload).get("tenant_id"), "tenant-A")
            next_before = saved.next_run_at
            if bundle.connection is not None:
                bundle.connection.close()

            bundle2 = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            try:
                store2 = bundle2.schedule_store
                restored = store2.get("durable-s1")
                self.assertIsNotNone(restored)
                self.assertEqual(restored.schedule_id, "durable-s1")
                self.assertEqual(dict(restored.payload).get("tenant_id"), "tenant-A")
                self.assertEqual(restored.next_run_at, next_before)
                all_ids = [s.schedule_id for s in store2.list_all()]
                self.assertEqual(all_ids.count("durable-s1"), 1)
            finally:
                if bundle2.connection is not None:
                    bundle2.connection.close()

    def test_compose_restart_no_duplicate_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "compose.sqlite3")
            env = _env(
                path,
                COMMERCE_ENABLED="true",
                COMMERCE_USE_SHARED_DB="true",
                COMMERCE_RECONCILIATION_ENABLED="true",
                COMMERCE_RECONCILIATION_INTERVAL_SECONDS="3600",
                COMMERCE_RECONCILIATION_TENANTS="tenant-A",
            )
            runtime = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                st = runtime.workflow_runtime.scheduler.store.get(
                    "commerce-reconcile:tenant-A"
                )
                self.assertIsNotNone(st)
                advanced = utc_now() + timedelta(hours=2)
                runtime.workflow_runtime.scheduler.store.save(
                    replace(st, next_run_at=advanced, run_count=3)
                )
            finally:
                runtime.close()

            runtime2 = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                schedules = [
                    s
                    for s in runtime2.workflow_runtime.scheduler.store.list_all()
                    if s.schedule_id == "commerce-reconcile:tenant-A"
                ]
                self.assertEqual(len(schedules), 1)
                self.assertEqual(schedules[0].run_count, 3)
                self.assertEqual(schedules[0].next_run_at, advanced)
            finally:
                runtime2.close()


class ScheduleDurabilityTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_restored_schedule_due_enqueues_with_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "due.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            runtime = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                schedule_store=bundle.schedule_store,
            )
            runtime.definitions.register(linear_demo_definition())
            runtime.platform.register_handler(
                STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
            )
            now = utc_now()
            runtime.register_schedule(
                ScheduleSpec(
                    schedule_id="due-a",
                    workflow_type="demo.linear",
                    version="1",
                    payload={"tenant_id": "tenant-A"},
                    run_at=now - timedelta(seconds=5),
                    interval_seconds=3600,
                )
            )
            if bundle.connection is not None:
                bundle.connection.close()

            bundle2 = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            try:
                runtime2 = build_workflow_runtime(
                    state_manager=StateManager(store=bundle2.workflow_runtime_store),
                    schedule_store=bundle2.schedule_store,
                )
                runtime2.definitions.register(linear_demo_definition())
                runtime2.platform.register_handler(
                    STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
                )
                self.assertIsNotNone(runtime2.scheduler.store.get("due-a"))
                launched = await runtime2.tick_schedules()
                self.assertEqual(len(launched), 1)
                state = runtime2.state_manager.get(launched[0])
                self.assertEqual(state.tenant_id, "tenant-A")
                self.assertEqual(dict(state.metadata).get("tenant_id"), "tenant-A")
            finally:
                if bundle2.connection is not None:
                    bundle2.connection.close()

    async def test_tenant_b_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "iso.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            runtime = build_workflow_runtime(
                state_manager=StateManager(store=bundle.workflow_runtime_store),
                schedule_store=bundle.schedule_store,
            )
            runtime.definitions.register(linear_demo_definition())
            runtime.platform.register_handler(
                STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
            )
            now = utc_now()
            for tenant in ("tenant-A", "tenant-B"):
                runtime.register_schedule(
                    ScheduleSpec(
                        schedule_id=f"iso-{tenant}",
                        workflow_type="demo.linear",
                        version="1",
                        payload={"tenant_id": tenant},
                        run_at=now - timedelta(seconds=1),
                        interval_seconds=3600,
                    )
                )
            launched = await runtime.tick_schedules()
            self.assertEqual(len(launched), 2)
            tenants = {
                runtime.state_manager.get(wid).tenant_id for wid in launched
            }
            self.assertEqual(tenants, {"tenant-A", "tenant-B"})
            if bundle.connection is not None:
                bundle.connection.close()

    def test_blank_tenant_fail_closed(self):
        scheduler = WorkflowScheduler()
        with self.assertRaises(MissingTenantError):
            scheduler.register(
                ScheduleSpec(
                    schedule_id="blank",
                    workflow_type="demo.linear",
                    version="1",
                    payload={},
                    run_at=utc_now(),
                )
            )

    async def test_duplicate_tick_same_window_no_duplicate_workflow(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        window = utc_now().replace(microsecond=0)
        bundle.scheduler.store.save(
            ScheduleState(
                schedule_id="dup-window",
                workflow_type="demo.linear",
                version="1",
                payload={"tenant_id": "tenant-dup"},
                next_run_at=window,
                interval_seconds=3600,
                enabled=True,
            )
        )
        launched1 = await bundle.tick_schedules()
        self.assertEqual(len(launched1), 1)
        # Force same logical window due again
        st = bundle.scheduler.store.get("dup-window")
        bundle.scheduler.store.save(replace(st, next_run_at=window, enabled=True))
        launched2 = await bundle.tick_schedules()
        self.assertEqual(len(launched2), 1)
        self.assertEqual(launched2[0], launched1[0])
        all_for_tenant = [
            w
            for w in bundle.state_manager._store.list_all()
            if w.tenant_id == "tenant-dup"
            and dict(w.metadata).get("schedule_id") == "dup-window"
        ]
        self.assertEqual(len(all_for_tenant), 1)

    async def test_missed_windows_execute_latest_skip_catchup(self):
        """Policy: one fire per tick; next_run = now+interval (skip missed intermediates)."""
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        interval = 3600.0
        # Due far in the past — multiple intervals missed
        far_past = utc_now() - timedelta(seconds=interval * 5 + 30)
        bundle.register_schedule(
            ScheduleSpec(
                schedule_id="missed-policy",
                workflow_type="demo.linear",
                version="1",
                payload={"tenant_id": "tenant-miss"},
                run_at=far_past,
                interval_seconds=interval,
            )
        )
        before = utc_now()
        launched = await bundle.tick_schedules()
        self.assertEqual(len(launched), 1)
        st = bundle.scheduler.store.get("missed-policy")
        # Single advance from enqueue stamp — not 5 catch-up fires
        self.assertEqual(st.run_count, 1)
        self.assertGreaterEqual(
            st.next_run_at, before + timedelta(seconds=interval - 5)
        )
        # Immediate second tick must not fire again (next is in the future)
        launched2 = await bundle.tick_schedules()
        self.assertEqual(launched2, [])
        self.assertEqual(st.run_count, 1)

    async def test_legacy_blank_payload_still_ticks(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        bundle.scheduler.store.save(
            ScheduleState(
                schedule_id="legacy-dur",
                workflow_type="demo.linear",
                version="1",
                payload={},
                next_run_at=utc_now() - timedelta(seconds=1),
                interval_seconds=3600,
                enabled=True,
            )
        )
        launched = await bundle.tick_schedules()
        self.assertEqual(len(launched), 1)
        self.assertEqual(
            bundle.state_manager.get(launched[0]).tenant_id, DEFAULT_LEGACY_TENANT
        )

    def test_idempotent_register_preserves_next_run(self):
        scheduler = WorkflowScheduler()
        first_due = utc_now() + timedelta(hours=3)
        scheduler.register(
            ScheduleSpec(
                schedule_id="idem-reg",
                workflow_type="demo.linear",
                version="1",
                payload={"tenant_id": "tenant-x"},
                run_at=first_due,
                interval_seconds=60,
            )
        )
        scheduler.store.save(
            replace(
                scheduler.store.get("idem-reg"),
                run_count=7,
                next_run_at=first_due,
            )
        )
        again = scheduler.register(
            ScheduleSpec(
                schedule_id="idem-reg",
                workflow_type="demo.linear",
                version="2",
                payload={"tenant_id": "tenant-x", "extra": True},
                run_at=utc_now(),  # would reset if not idempotent
                interval_seconds=120,
            )
        )
        self.assertEqual(again.version, "2")
        self.assertEqual(again.run_count, 7)
        self.assertEqual(again.next_run_at, first_due)
        self.assertEqual(again.interval_seconds, 120.0)
        self.assertTrue(dict(again.payload).get("extra"))


if __name__ == "__main__":
    unittest.main()
