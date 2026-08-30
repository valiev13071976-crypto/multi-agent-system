"""Schema v3 tenant column, migration, and multi-tenant store isolation."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import MissingTenantError
from side_effects.protected_state_store import PersistentWorkflowRuntimeStore
from side_effects.schema import SCHEMA_VERSION
from side_effects.sqlite_store import SqliteConnection
from workflow.definition import STEP_TYPE_HANDLER, ScheduleSpec, StepResult
from workflow.errors import WorkflowNotFoundError
from workflow.models import STATUS_PLANNED, utc_now
from workflow.schedule import ScheduleState, WorkflowScheduler
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager
from workflow.store import InMemoryWorkflowStateStore
from tests.test_smoke import load_app
from tests.test_workflow_foundation import linear_demo_definition


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _seed_v2_db(path: str, *, rows: list[dict]) -> None:
    """Create a schema-v2 side_effects DB with workflow_runtime_state rows (no tenant column)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE side_effect_schema_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        INSERT INTO side_effect_schema_meta(id, version) VALUES (1, 2);
        CREATE TABLE workflow_runtime_state (
            workflow_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            state TEXT NOT NULL,
            current_step TEXT,
            waiting_reason TEXT,
            approval_id TEXT,
            action_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            failed_at TEXT,
            error_code TEXT,
            execution_key TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            sensitivity TEXT NOT NULL DEFAULT 'internal',
            safe_metadata_json TEXT NOT NULL DEFAULT '{}',
            encrypted_payload_json TEXT
        );
        """
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO workflow_runtime_state (
                workflow_id, task_id, state, current_step, waiting_reason,
                approval_id, action_id, created_at, updated_at, started_at,
                completed_at, failed_at, error_code, execution_key, version,
                sensitivity, safe_metadata_json, encrypted_payload_json
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, ?, 1,
                      'internal', ?, NULL)
            """,
            (
                row["workflow_id"],
                row["task_id"],
                row.get("state", "created"),
                None,
                row.get("created_at", T0.isoformat()),
                row.get("updated_at", T0.isoformat()),
                row["execution_key"],
                json.dumps(row.get("safe_metadata") or {}),
            ),
        )
    conn.commit()
    conn.close()


class SchemaV3MigrationTests(unittest.TestCase):
    def test_fresh_db_is_schema_v8_with_tenant_and_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "fresh.sqlite3")
            conn = SqliteConnection(path)
            version = conn.initialize_schema()
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertEqual(SCHEMA_VERSION, 8)
            cols = {
                str(r["name"])
                for r in conn.connect()
                .execute("PRAGMA table_info(workflow_runtime_state)")
                .fetchall()
            }
            self.assertIn("tenant_id", cols)
            se_cols = {
                str(r["name"])
                for r in conn.connect()
                .execute("PRAGMA table_info(side_effect_executions)")
                .fetchall()
            }
            self.assertIn("tenant_id", se_cols)
            indexes = {
                str(r["name"])
                for r in conn.connect()
                .execute("PRAGMA index_list(side_effect_executions)")
                .fetchall()
            }
            self.assertIn("idx_se_exec_tenant", indexes)
            indexes = {
                str(r["name"])
                for r in conn.connect()
                .execute("PRAGMA index_list(workflow_runtime_state)")
                .fetchall()
            }
            self.assertIn("idx_wf_runtime_tenant", indexes)
            tables = {
                str(r["name"])
                for r in conn.connect()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
            self.assertIn("workflow_schedules", tables)
            self.assertIn("queue_tasks", tables)
            self.assertIn("provider_governor_slots", tables)
            sched_cols = {
                str(r["name"])
                for r in conn.connect()
                .execute("PRAGMA table_info(workflow_schedules)")
                .fetchall()
            }
            self.assertIn("claim_token", sched_cols)
            qcols = {
                str(r["name"])
                for r in conn.connect()
                .execute("PRAGMA table_info(queue_tasks)")
                .fetchall()
            }
            self.assertIn("execution_lane", qcols)
            conn.close()

    def test_v2_migrates_to_current_and_preserves_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v2.sqlite3")
            _seed_v2_db(
                path,
                rows=[
                    {
                        "workflow_id": "wf-legacy",
                        "task_id": "t1",
                        "execution_key": "ek-1",
                        "safe_metadata": {"tenant_id": "tenant-A"},
                    }
                ],
            )
            conn = SqliteConnection(path)
            version = conn.initialize_schema()
            self.assertEqual(version, SCHEMA_VERSION)
            row = conn.connect().execute(
                "SELECT tenant_id, safe_metadata_json, workflow_id "
                "FROM workflow_runtime_state WHERE workflow_id = ?",
                ("wf-legacy",),
            ).fetchone()
            self.assertEqual(row["tenant_id"], "tenant-A")
            meta = json.loads(row["safe_metadata_json"])
            self.assertEqual(meta.get("tenant_id"), "tenant-A")
            version2 = conn.initialize_schema()
            self.assertEqual(version2, SCHEMA_VERSION)
            self.assertEqual(conn.get_schema_version(), SCHEMA_VERSION)
            tables = {
                str(r["name"])
                for r in conn.connect()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
            self.assertIn("workflow_schedules", tables)
            self.assertIn("queue_tasks", tables)
            self.assertIn("provider_governor_slots", tables)
            qcols = {
                str(r["name"])
                for r in conn.connect()
                .execute("PRAGMA table_info(queue_tasks)")
                .fetchall()
            }
            self.assertIn("execution_lane", qcols)
            conn.close()

    def test_legacy_row_without_tenant_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.sqlite3")
            _seed_v2_db(
                path,
                rows=[
                    {
                        "workflow_id": "wf-no-tenant",
                        "task_id": "t0",
                        "execution_key": "ek-0",
                        "safe_metadata": {"workflow_type": "demo"},
                    }
                ],
            )
            conn = SqliteConnection(path)
            conn.initialize_schema()
            store = PersistentWorkflowRuntimeStore(conn)
            state = store.get("wf-no-tenant")
            self.assertIsNotNone(state)
            self.assertIsNone(state.tenant_id)
            # Internal recovery still lists it
            self.assertEqual(len(store.list_all()), 1)
            conn.close()

    def test_new_durable_write_fail_closed_without_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "fc.sqlite3")
            conn = SqliteConnection(path)
            conn.initialize_schema()
            manager = StateManager(store=PersistentWorkflowRuntimeStore(conn))
            with self.assertRaises(MissingTenantError):
                manager.create(task_id="t-x")
            conn.close()


class TenantScopedStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryWorkflowStateStore()
        self.manager = StateManager(store=self.store)

    def test_cross_tenant_get_denied(self):
        a = self.manager.create(task_id="ta", tenant_id="tenant-A")
        b = self.manager.create(task_id="tb", tenant_id="tenant-B")
        self.assertEqual(
            self.store.get_for_tenant(a.workflow_id, "tenant-A").workflow_id,
            a.workflow_id,
        )
        self.assertEqual(
            self.store.get_for_tenant(b.workflow_id, "tenant-B").workflow_id,
            b.workflow_id,
        )
        self.assertIsNone(self.store.get_for_tenant(a.workflow_id, "tenant-B"))
        self.assertIsNone(self.store.get_for_tenant(b.workflow_id, "tenant-A"))
        with self.assertRaises(WorkflowNotFoundError):
            self.manager.get_for_tenant(a.workflow_id, "tenant-B")

    def test_list_by_status_tenant_scoped(self):
        a = self.manager.create(task_id="ta", tenant_id="tenant-A")
        b = self.manager.create(task_id="tb", tenant_id="tenant-B")
        self.manager.plan(a.workflow_id)
        self.manager.plan(b.workflow_id)
        listed_a = self.store.list_by_status(STATUS_PLANNED, tenant_id="tenant-A")
        listed_b = self.store.list_by_status(STATUS_PLANNED, tenant_id="tenant-B")
        self.assertEqual({s.workflow_id for s in listed_a}, {a.workflow_id})
        self.assertEqual({s.workflow_id for s in listed_b}, {b.workflow_id})

    def test_find_by_execution_key_tenant_scoped(self):
        self.manager.create(
            task_id="ta", tenant_id="tenant-A", execution_key="shared-ek"
        )
        self.manager.create(
            task_id="tb", tenant_id="tenant-B", execution_key="shared-ek"
        )
        found_a = self.store.find_by_execution_key("shared-ek", tenant_id="tenant-A")
        found_b = self.store.find_by_execution_key("shared-ek", tenant_id="tenant-B")
        self.assertEqual(found_a.tenant_id, "tenant-A")
        self.assertEqual(found_b.tenant_id, "tenant-B")
        self.assertNotEqual(found_a.workflow_id, found_b.workflow_id)

    def test_persistent_cross_tenant_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "mt.sqlite3")
            conn = SqliteConnection(path)
            conn.initialize_schema()
            store = PersistentWorkflowRuntimeStore(conn)
            manager = StateManager(store=store)
            a = manager.create(task_id="ta", tenant_id="tenant-A")
            b = manager.create(task_id="tb", tenant_id="tenant-B")
            self.assertIsNotNone(store.get_for_tenant(a.workflow_id, "tenant-A"))
            self.assertIsNone(store.get_for_tenant(a.workflow_id, "tenant-B"))
            self.assertIsNone(store.get_for_tenant(b.workflow_id, "tenant-A"))
            listed = store.list_by_status("created", tenant_id="tenant-A")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].workflow_id, a.workflow_id)
            # Dual-write: column + metadata
            row = conn.connect().execute(
                "SELECT tenant_id, safe_metadata_json FROM workflow_runtime_state "
                "WHERE workflow_id = ?",
                (a.workflow_id,),
            ).fetchone()
            self.assertEqual(row["tenant_id"], "tenant-A")
            self.assertEqual(
                json.loads(row["safe_metadata_json"]).get("tenant_id"), "tenant-A"
            )
            conn.close()


class DurableWorkflowPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_enqueue_propagates_identity_to_queue(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        created = await bundle.create_and_enqueue(
            "demo.linear",
            "1",
            task_id="prop-1",
            tenant_id="tenant-prop",
            request_id="req-prop-1",
            user_id="user-prop",
            actor_ref="tenant-prop:user-prop",
        )
        state = bundle.state_manager.get(created["workflow_id"])
        self.assertEqual(state.tenant_id, "tenant-prop")
        self.assertEqual(state.request_id, "req-prop-1")
        self.assertEqual(state.user_id, "user-prop")
        self.assertEqual(state.actor_ref, "tenant-prop:user-prop")
        qid = created.get("queue_task_id")
        self.assertIsNotNone(qid)
        task = bundle.queue.store.get(qid)
        self.assertEqual(task.tenant_id, "tenant-prop")
        self.assertEqual(task.user_id, "user-prop")
        self.assertEqual(task.actor_ref, "tenant-prop:user-prop")
        self.assertEqual(task.workflow_id, state.workflow_id)
        self.assertEqual(task.task_id, state.task_id)
        self.assertNotEqual(task.queue_task_id, state.workflow_id)
        self.assertNotEqual(task.execution_key, state.execution_key)

    def test_http_post_workflows_propagates_request_security_context(self):
        main_mod = load_app(
            SECURITY_AUTH_MODE="required",
            PANDA_API_KEYS=(
                "key-a|tenant-http|user-http|user,operator|secret-http"
            ),
        )
        client = TestClient(main_mod.app)
        created = client.post(
            "/api/workflows",
            json={"workflow_type": "demo.linear", "version": "1", "sync": False},
            headers={"X-API-Key": "secret-http"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        wid = created.json()["workflow_id"]
        state = main_mod.workflow_runtime.state_manager.get(wid)
        self.assertEqual(state.tenant_id, "tenant-http")
        self.assertEqual(state.user_id, "user-http")
        self.assertTrue(state.request_id)
        self.assertEqual(state.actor_ref, "tenant-http:user-http")
        qid = created.json().get("queue_task_id")
        if qid:
            task = main_mod.workflow_runtime.queue.store.get(qid)
            self.assertEqual(task.tenant_id, "tenant-http")
            self.assertEqual(task.user_id, "user-http")
            self.assertEqual(task.actor_ref, "tenant-http:user-http")


class ScheduleSecurityTests(unittest.TestCase):
    def test_new_schedule_requires_tenant(self):
        scheduler = WorkflowScheduler()
        with self.assertRaises(MissingTenantError):
            scheduler.register(
                ScheduleSpec(
                    schedule_id="s-blank",
                    workflow_type="demo.linear",
                    version="1",
                    payload={},
                    run_at=utc_now(),
                )
            )
        with self.assertRaises(MissingTenantError):
            scheduler.register(
                ScheduleSpec(
                    schedule_id="s-empty",
                    workflow_type="demo.linear",
                    version="1",
                    payload={"tenant_id": ""},
                    run_at=utc_now(),
                )
            )

    def test_new_schedule_with_tenant_passes(self):
        scheduler = WorkflowScheduler()
        state = scheduler.register(
            ScheduleSpec(
                schedule_id="s-ok",
                workflow_type="demo.linear",
                version="1",
                payload={"tenant_id": "tenant-sched"},
                run_at=utc_now(),
            )
        )
        self.assertEqual(state.schedule_id, "s-ok")
        self.assertEqual(dict(state.payload).get("tenant_id"), "tenant-sched")


class LegacyScheduleRunnableTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_schedule_still_runnable(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        now = utc_now()
        bundle.scheduler.store.save(
            ScheduleState(
                schedule_id="legacy-s1",
                workflow_type="demo.linear",
                version="1",
                payload={},
                next_run_at=now - timedelta(seconds=1),
                interval_seconds=3600,
                enabled=True,
            )
        )
        launched = await bundle.tick_schedules()
        self.assertEqual(len(launched), 1)
        state = bundle.state_manager.get(launched[0])
        self.assertEqual(state.tenant_id, DEFAULT_LEGACY_TENANT)


class CancelResumeTenantRegression(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_and_resume_paths_respect_tenant_lookup(self):
        bundle = build_workflow_runtime()
        bundle.definitions.register(linear_demo_definition())
        bundle.platform.register_handler(
            STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
        )
        created = await bundle.create_and_enqueue(
            "demo.linear",
            "1",
            tenant_id="tenant-A",
            user_id="ua",
            actor_ref="tenant-A:ua",
            request_id="req-a",
        )
        wid = created["workflow_id"]
        cancelled = bundle.cancel(wid)
        status = cancelled.get("status") or bundle.get_status(wid)["status"]
        self.assertEqual(status, "cancelled")
        with self.assertRaises(WorkflowNotFoundError):
            bundle.state_manager.get_for_tenant(wid, "tenant-B")


if __name__ == "__main__":
    unittest.main()
