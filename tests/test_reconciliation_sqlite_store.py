import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from side_effects.models import (
    DECISION_NO_ACTION,
    RECON_PENDING,
    ReconciliationRecord,
)
from side_effects.sqlite_store import PersistentReconciliationStore, SqliteConnection


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class ReconciliationSqliteStoreTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmpdir.name) / "recon.sqlite3")
        self.conn = SqliteConnection(self.path)
        self.conn.initialize_schema()
        self.store = PersistentReconciliationStore(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass

    def test_create_get_find(self):
        record = ReconciliationRecord(
            reconciliation_id="rec-1",
            execution_id="exec-1",
            workflow_id="wf-1",
            task_id="task-1",
            action_id="act-1",
            tool_id="github.issue_labels",
            operation="ensure_label_present",
            idempotency_key_hash="hash",
            status=RECON_PENDING,
            decision=DECISION_NO_ACTION,
            attempt=1,
            created_at=T0,
            version=1,
        )
        self.store.create(record)
        loaded = self.store.get("rec-1")
        self.assertEqual(loaded.execution_id, "exec-1")
        found = self.store.find_by_execution("exec-1")
        self.assertEqual(len(found), 1)
        pending = self.store.list_pending()
        self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
