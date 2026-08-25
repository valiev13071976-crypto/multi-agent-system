import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autonomy.models import (
    APPROVAL_APPROVED,
    APPROVAL_CANCELLED,
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ApprovalRecord,
)
from hitl.errors import ApprovalConflictError
from side_effects.protected_state_store import PersistentApprovalStore
from side_effects.sqlite_store import SqliteConnection


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _record(**kwargs):
    base = dict(
        approval_id="ap-1",
        workflow_id="wf-1",
        task_id="task-1",
        action_id="act-1",
        decision_id="dec-1",
        status=APPROVAL_PENDING,
        approved_by="pending",
        created_at=T0,
        approval_class="high_risk",
        requested_by="agent-1",
        requested_at=T0,
        expires_at=T0 + timedelta(hours=1),
        version=1,
        action_fingerprint="fp-abc",
        metadata={"tool_id": "test.write", "operation": "set_value"},
    )
    base.update(kwargs)
    return ApprovalRecord(**base)


class HITLSqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "p7e.sqlite3")
        self.conn = SqliteConnection(self.path)
        self.conn.initialize_schema()
        self.store = PersistentApprovalStore(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_pending_round_trip(self):
        self.store.create(_record())
        loaded = self.store.get("ap-1")
        self.assertEqual(loaded.status, APPROVAL_PENDING)
        self.assertEqual(loaded.action_fingerprint, "fp-abc")
        self.assertEqual(loaded.requested_by, "agent-1")

    def test_approved_rejected_cancelled_round_trip(self):
        for status, aid in (
            (APPROVAL_APPROVED, "ap-ok"),
            (APPROVAL_REJECTED, "ap-no"),
            (APPROVAL_CANCELLED, "ap-cx"),
        ):
            self.store.create(
                _record(
                    approval_id=aid,
                    action_id=aid,
                    status=status,
                    approved_by="reviewer-1",
                    resolved_by="reviewer-1",
                    resolved_at=T0,
                    version=1,
                )
            )
            self.assertEqual(self.store.get(aid).status, status)

    def test_expiry_after_restart(self):
        self.store.create(
            _record(expires_at=T0 - timedelta(seconds=1), approval_id="ap-exp")
        )
        self.conn.close()
        conn2 = SqliteConnection(self.path)
        conn2.initialize_schema()
        store2 = PersistentApprovalStore(conn2)
        changed = store2.normalize_expired(now=T0)
        self.assertEqual(changed, 1)
        self.assertEqual(store2.get("ap-exp").status, APPROVAL_EXPIRED)
        conn2.close()

    def test_fingerprint_preserved(self):
        self.store.create(_record(action_fingerprint="deadbeef" * 8))
        self.assertEqual(self.store.get("ap-1").action_fingerprint, "deadbeef" * 8)

    def test_stale_version_conflict(self):
        self.store.create(_record())
        with self.assertRaises(ApprovalConflictError):
            self.store.save(_record(status=APPROVAL_APPROVED, version=3))

    def test_duplicate_active_approval_protection(self):
        self.store.create(_record(approval_id="ap-a", action_id="same-action"))
        with self.assertRaises(ApprovalConflictError):
            self.store.create(
                _record(approval_id="ap-b", action_id="same-action", version=1)
            )


if __name__ == "__main__":
    unittest.main()
