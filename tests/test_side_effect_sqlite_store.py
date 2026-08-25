import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from side_effects.errors import SideEffectPersistenceConflictError
from side_effects.models import (
    AUTHORIZATION_EXECUTION_PERMIT,
    OUTCOME_KNOWN_SUCCESS,
    ROLLBACK_NONE,
    STATUS_SUCCEEDED,
    SideEffectExecutionRecord,
)
from side_effects.sqlite_store import PersistentSideEffectExecutionStore, SqliteConnection


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _record(**extra):
    fields = dict(
        execution_id="exec-1",
        action_id="act-1",
        workflow_id="wf-1",
        task_id="task-1",
        tool_id="github.issue_labels",
        operation="ensure_label_present",
        status=STATUS_SUCCEEDED,
        authorization_type=AUTHORIZATION_EXECUTION_PERMIT,
        authorization_id="permit-1",
        idempotency_key_hash="abc",
        attempt=1,
        started_at=T0,
        completed_at=T0,
        external_reference="github_issue:octo/hello#1",
        rollback_reference="prior_present=0:changed=1",
        rollback_status=ROLLBACK_NONE,
        outcome=OUTCOME_KNOWN_SUCCESS,
        resource_ref="github://octo/hello/issues/1/labels/bug",
        reversible=True,
        version=1,
        metadata={"state_after_present": True},
    )
    fields.update(extra)
    return SideEffectExecutionRecord(**fields)


class SideEffectSqliteStoreTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmpdir.name) / "side_effects.sqlite3")
        self.conn = SqliteConnection(self.path)
        self.conn.initialize_schema()
        self.store = PersistentSideEffectExecutionStore(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass

    def test_a_round_trip(self):
        created = self.store.create(_record())
        loaded = self.store.get("exec-1")
        self.assertEqual(loaded.execution_id, created.execution_id)
        self.assertEqual(loaded.status, STATUS_SUCCEEDED)
        self.assertEqual(loaded.outcome, OUTCOME_KNOWN_SUCCESS)

    def test_b_ids_preserved(self):
        self.store.create(_record())
        loaded = self.store.get("exec-1")
        self.assertEqual(loaded.workflow_id, "wf-1")
        self.assertEqual(loaded.task_id, "task-1")
        self.assertEqual(loaded.action_id, "act-1")
        self.assertEqual(loaded.tool_id, "github.issue_labels")

    def test_c_rollback_reference_preserved(self):
        self.store.create(_record())
        loaded = self.store.get("exec-1")
        self.assertEqual(loaded.rollback_reference, "prior_present=0:changed=1")

    def test_d_external_reference_preserved(self):
        self.store.create(_record())
        self.assertEqual(
            self.store.get("exec-1").external_reference, "github_issue:octo/hello#1"
        )

    def test_e_version_increments(self):
        self.store.create(_record())
        updated = _record(version=2, status=STATUS_SUCCEEDED, metadata={"n": 2})
        self.store.save(updated)
        self.assertEqual(self.store.get("exec-1").version, 2)

    def test_f_stale_version_conflicts(self):
        self.store.create(_record())
        self.store.save(_record(version=2))
        with self.assertRaises(SideEffectPersistenceConflictError):
            self.store.save(_record(version=2, metadata={"stale": True}))

    def test_g_metadata_bounded(self):
        huge = {"blob": "x" * 20_000}
        with self.assertRaises(Exception):
            self.store.create(_record(metadata=huge))


if __name__ == "__main__":
    unittest.main()
