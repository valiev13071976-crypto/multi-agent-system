"""Recovery case store durability, versioning, dedup."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recovery.models import (
    CASE_UNCERTAIN_SIDE_EFFECT,
    STATUS_OPEN,
    STATUS_RESOLVED,
    SEVERITY_HIGH,
    RecoveryCase,
    RecoveryDecision,
    utc_now,
)
from recovery.store import (
    InMemoryRecoveryCaseStore,
    RecoveryConflictError,
    SqliteRecoveryCaseStore,
)


def _case(**kwargs) -> RecoveryCase:
    stamp = utc_now()
    fields = {
        "recovery_id": "r1",
        "execution_id": "e1",
        "workflow_id": "w1",
        "task_id": "t1",
        "action_id": "a1",
        "tool_id": "tool",
        "operation": "op",
        "case_type": CASE_UNCERTAIN_SIDE_EFFECT,
        "status": STATUS_OPEN,
        "severity": SEVERITY_HIGH,
        "reason_code": "uncertain",
        "created_at": stamp,
        "updated_at": stamp,
        "version": 1,
    }
    fields.update(kwargs)
    return RecoveryCase(**fields)


class RecoveryCaseStoreTests(unittest.TestCase):
    def test_create_get_update(self):
        store = InMemoryRecoveryCaseStore()
        created = store.create(_case())
        self.assertEqual(store.get("r1").execution_id, "e1")
        updated = store.update(
            _case(status=STATUS_RESOLVED, reason_code="done", version=1),
            expected_version=1,
        )
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.status, STATUS_RESOLVED)

    def test_version_conflict(self):
        store = InMemoryRecoveryCaseStore()
        store.create(_case())
        with self.assertRaises(RecoveryConflictError):
            store.update(_case(status=STATUS_RESOLVED), expected_version=99)

    def test_dedup_active(self):
        store = InMemoryRecoveryCaseStore()
        store.create(_case())
        with self.assertRaises(RecoveryConflictError):
            store.create(_case(recovery_id="r2"))

    def test_resolved_remains_terminal(self):
        store = InMemoryRecoveryCaseStore()
        store.create(_case())
        store.update(_case(status=STATUS_RESOLVED), expected_version=1)
        with self.assertRaises(RecoveryConflictError):
            store.update(_case(status=STATUS_OPEN, version=2), expected_version=2)

    def test_sqlite_restart_durability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recovery.sqlite3"
            store = SqliteRecoveryCaseStore(path)
            store.create(_case())
            store.add_decision(
                RecoveryDecision(
                    decision_id="d1",
                    recovery_id="r1",
                    decision="DEFER",
                    actor_id="op",
                    reason_code="later",
                    created_at=utc_now(),
                )
            )
            store.close()
            store2 = SqliteRecoveryCaseStore(path)
            self.assertIsNotNone(store2.get("r1"))
            self.assertEqual(len(store2.list_decisions("r1")), 1)
            store2.close()


if __name__ == "__main__":
    unittest.main()
