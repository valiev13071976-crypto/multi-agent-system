import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from side_effects.protected_state_store import PersistentWorkflowRuntimeStore
from side_effects.sqlite_store import SqliteConnection
from workflow.errors import WorkflowConflictError, WorkflowTransitionError
from workflow.models import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    TERMINAL_STATUSES,
)
from workflow.state_manager import StateManager


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class WorkflowRuntimeSqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "wf.sqlite3")
        self.conn = SqliteConnection(self.path)
        self.conn.initialize_schema()
        self.store = PersistentWorkflowRuntimeStore(self.conn)
        self.manager = StateManager(store=self.store)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_waiting_approval_persists(self):
        wf = self.manager.create(task_id="t1")
        self.manager.plan(wf.workflow_id)
        self.manager.start(wf.workflow_id)
        self.manager.wait_for_approval(wf.workflow_id)
        self.manager.checkpoint(
            wf.workflow_id,
            extra_payload={"approval_id": "ap-1", "action_id": "act-1"},
        )
        self.conn.close()
        conn2 = SqliteConnection(self.path)
        conn2.initialize_schema()
        store2 = PersistentWorkflowRuntimeStore(conn2)
        loaded = store2.get(wf.workflow_id)
        self.assertEqual(loaded.status, STATUS_WAITING_APPROVAL)
        point = store2.get_checkpoint(wf.workflow_id)
        self.assertEqual(point.payload.get("approval_id"), "ap-1")
        waiting = store2.list_waiting_approval()
        self.assertEqual(len(waiting), 1)
        conn2.close()

    def test_running_and_terminal_persist(self):
        wf = self.manager.create(task_id="t2")
        self.manager.plan(wf.workflow_id)
        self.manager.start(wf.workflow_id)
        self.assertEqual(self.store.get(wf.workflow_id).status, STATUS_RUNNING)
        self.manager.complete_workflow(wf.workflow_id)
        self.assertEqual(self.store.get(wf.workflow_id).status, STATUS_COMPLETED)

    def test_terminal_not_reopened(self):
        wf = self.manager.create(task_id="t3")
        self.manager.plan(wf.workflow_id)
        self.manager.start(wf.workflow_id)
        self.manager.fail_workflow(wf.workflow_id, "boom")
        self.assertIn(self.store.get(wf.workflow_id).status, TERMINAL_STATUSES)
        with self.assertRaises(WorkflowTransitionError):
            self.manager.wait_for_approval(wf.workflow_id)
        self.assertEqual(self.store.get(wf.workflow_id).status, STATUS_FAILED)

    def test_version_conflict(self):
        wf = self.manager.create(task_id="t4")
        state = self.store.get(wf.workflow_id)
        from dataclasses import replace

        with self.assertRaises(WorkflowConflictError):
            self.store.save(replace(state, version=state.version + 2, status=STATUS_RUNNING))


if __name__ == "__main__":
    unittest.main()
