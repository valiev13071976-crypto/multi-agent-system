"""FH.3 — DB-level tenant isolation regression (cross-tenant get/list)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from side_effects.models import (
    AUTHORIZATION_AUTONOMY_DECISION,
    OUTCOME_KNOWN_SUCCESS,
    ROLLBACK_NONE,
    STATUS_SUCCEEDED,
    SideEffectExecutionRecord,
)
from side_effects.sqlite_store import PersistentSideEffectExecutionStore, SqliteConnection
from side_effects.store import InMemorySideEffectExecutionStore
from task_queue.models import PRIORITY_NORMAL, STATUS_QUEUED, QueueTask
from task_queue.sqlite_store import PersistentTaskQueueStore
from workflow.engine import WorkflowEngine


T0 = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _se_record(**kw) -> SideEffectExecutionRecord:
    base = dict(
        execution_id="exec-a",
        action_id="act-a",
        workflow_id="wf-a",
        task_id="task-a",
        tool_id="test.tool",
        operation="set_value",
        status=STATUS_SUCCEEDED,
        authorization_type=AUTHORIZATION_AUTONOMY_DECISION,
        authorization_id="auth",
        idempotency_key_hash="h",
        attempt=1,
        started_at=T0,
        completed_at=T0,
        outcome=OUTCOME_KNOWN_SUCCESS,
        rollback_status=ROLLBACK_NONE,
        tenant_id="tenant-a",
    )
    base.update(kw)
    return SideEffectExecutionRecord(**base)


class FH3DbTenantIsolationTests(unittest.TestCase):
    def test_workflow_cross_tenant_get_denied(self):
        engine = WorkflowEngine()
        wid = engine.create("t", tenant_id="tenant-a")
        with self.assertRaises(Exception):
            engine.state_manager.get_for_tenant(wid, "tenant-b")

    def test_side_effect_get_for_tenant_isolates(self):
        store = InMemorySideEffectExecutionStore()
        store.create(_se_record(execution_id="e1", tenant_id="tenant-a"))
        store.create(
            _se_record(execution_id="e2", workflow_id="wf-b", tenant_id="tenant-b")
        )
        self.assertIsNotNone(store.get_for_tenant("e1", "tenant-a"))
        self.assertIsNone(store.get_for_tenant("e1", "tenant-b"))
        self.assertEqual(len(store.list_by_tenant("tenant-a")), 1)
        self.assertEqual(len(store.list_by_tenant("tenant-b")), 1)

    def test_side_effect_sqlite_get_for_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "se.sqlite3")
            conn = SqliteConnection(path)
            conn.initialize_schema()
            store = PersistentSideEffectExecutionStore(conn)
            store.create(_se_record(execution_id="e1", tenant_id="tenant-a"))
            self.assertEqual(store.get_for_tenant("e1", "tenant-a").tenant_id, "tenant-a")
            self.assertIsNone(store.get_for_tenant("e1", "tenant-b"))
            conn.close()

    def test_queue_get_for_tenant_isolates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "q.sqlite3")
            se_conn = SqliteConnection(path)
            se_conn.initialize_schema()
            store = PersistentTaskQueueStore(se_conn)
            task = QueueTask(
                queue_task_id="qt-1",
                workflow_id="wf-1",
                task_id="task-1",
                execution_key="ek-1",
                status=STATUS_QUEUED,
                priority=PRIORITY_NORMAL,
                attempt=0,
                max_attempts=3,
                created_at=T0,
                available_at=T0,
                tenant_id="tenant-a",
            )
            store.save(task)
            self.assertIsNotNone(store.get_for_tenant("qt-1", "tenant-a"))
            self.assertIsNone(store.get_for_tenant("qt-1", "tenant-b"))
            se_conn.close()

    def test_concurrent_tenants_list_independent(self):
        store = InMemorySideEffectExecutionStore()
        for i, tid in enumerate(("tenant-a", "tenant-b")):
            store.create(
                _se_record(
                    execution_id=f"e-{tid}",
                    workflow_id=f"wf-{i}",
                    tenant_id=tid,
                )
            )
        self.assertEqual(
            {r.tenant_id for r in store.list_by_tenant("tenant-a")}, {"tenant-a"}
        )
        self.assertEqual(
            {r.tenant_id for r in store.list_by_tenant("tenant-b")}, {"tenant-b"}
        )


if __name__ == "__main__":
    unittest.main()
