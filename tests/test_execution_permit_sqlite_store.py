import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hitl.errors import ExecutionPermitConflictError
from hitl.models import PERMIT_CONSUMED, PERMIT_EXPIRED, PERMIT_ISSUED, ExecutionPermit
from hitl.permit import PermitService
from side_effects.protected_state_store import PersistentExecutionPermitStore
from side_effects.sqlite_store import SqliteConnection
from tools.models import TOOL_TRUST_INTERNAL_SAFE
from autonomy.gate import build_proposed_action
from autonomy.capabilities import CAP_EXTERNAL_WRITE
from hitl.models import action_fingerprint
from hitl.errors import (
    ExecutionPermitConsumedError,
    ExecutionPermitExpiredError,
    ExecutionPermitMismatchError,
)


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _permit(**kwargs):
    base = dict(
        permit_id="perm-1",
        workflow_id="wf-1",
        task_id="task-1",
        action_id="act-1",
        approval_id="ap-1",
        decision_id="dec-1",
        action_fingerprint="fp-1",
        issued_at=T0,
        expires_at=T0 + timedelta(minutes=5),
        capabilities=(CAP_EXTERNAL_WRITE,),
        tool_id="test.write",
        operation="set_value",
        idempotency_key="idem-1",
        status=PERMIT_ISSUED,
        version=1,
    )
    base.update(kwargs)
    return ExecutionPermit(**base)


def _action(workflow_id="wf-1", **kwargs):
    fields = dict(
        action_type="write",
        workflow_id=workflow_id,
        task_id="task-1",
        tool_id="test.write",
        operation="set_value",
        resource="test/key",
        idempotency_key="idem-1",
        metadata={"reversible": True},
        tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
        requested_capabilities=(CAP_EXTERNAL_WRITE,),
        risk_class="low",
        action_id="act-1",
    )
    fields.update(kwargs)
    return build_proposed_action(**fields)


class ExecutionPermitSqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "permit.sqlite3")
        self.conn = SqliteConnection(self.path)
        self.conn.initialize_schema()
        self.store = PersistentExecutionPermitStore(self.conn)
        self.service = PermitService(store=self.store)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_issue_persist_load(self):
        self.store.create(_permit())
        loaded = self.store.get("perm-1")
        self.assertEqual(loaded.status, PERMIT_ISSUED)
        self.assertEqual(loaded.approval_id, "ap-1")

    def test_active_survives_restart(self):
        self.store.create(_permit())
        self.conn.close()
        conn2 = SqliteConnection(self.path)
        conn2.initialize_schema()
        store2 = PersistentExecutionPermitStore(conn2)
        self.assertEqual(store2.get("perm-1").status, PERMIT_ISSUED)
        conn2.close()

    def test_consumed_survives_restart(self):
        action = _action()
        fp = action_fingerprint(action)
        self.store.create(_permit(action_fingerprint=fp))
        consumed = self.service.consume_for_execution("perm-1", action=action, now=T0)
        self.assertEqual(consumed.status, PERMIT_CONSUMED)
        self.conn.close()
        conn2 = SqliteConnection(self.path)
        conn2.initialize_schema()
        store2 = PersistentExecutionPermitStore(conn2)
        loaded = store2.get("perm-1")
        self.assertEqual(loaded.status, PERMIT_CONSUMED)
        self.assertIsNotNone(loaded.consumed_at)
        with self.assertRaises(ExecutionPermitConsumedError):
            PermitService(store=store2).consume_for_execution(
                "perm-1", action=action, now=T0
            )
        conn2.close()

    def test_expired_denied(self):
        self.store.create(_permit(expires_at=T0 - timedelta(seconds=1)))
        with self.assertRaises(ExecutionPermitExpiredError):
            self.service.validate(self.store.get("perm-1"), now=T0)

    def test_fingerprint_mismatch_denied(self):
        action = _action()
        self.store.create(_permit(action_fingerprint="wrong"))
        with self.assertRaises(ExecutionPermitMismatchError):
            self.service.validate(self.store.get("perm-1"), action=action, now=T0)

    def test_approval_mismatch_via_active_lookup(self):
        self.store.create(_permit(approval_id="ap-1"))
        self.assertIsNone(self.store.find_active_by_approval("ap-other"))
        self.assertIsNotNone(self.store.find_active_by_approval("ap-1"))

    def test_second_consume_denied(self):
        action = _action()
        fp = action_fingerprint(action)
        self.store.create(_permit(action_fingerprint=fp))
        self.service.consume_for_execution("perm-1", action=action, now=T0)
        with self.assertRaises(ExecutionPermitConsumedError):
            self.service.consume_for_execution("perm-1", action=action, now=T0)

    def test_stale_version_conflict(self):
        self.store.create(_permit())
        with self.assertRaises(ExecutionPermitConflictError):
            self.store.save(_permit(status=PERMIT_CONSUMED, version=3))

    def test_duplicate_active_permit_protection(self):
        self.store.create(_permit(permit_id="p1", approval_id="ap-x"))
        with self.assertRaises(ExecutionPermitConflictError):
            self.store.create(_permit(permit_id="p2", approval_id="ap-x"))

    def test_expired_status_after_normalize(self):
        self.store.create(_permit(expires_at=T0 - timedelta(seconds=1)))
        self.assertEqual(self.store.normalize_expired(now=T0), 1)
        self.assertEqual(self.store.get("perm-1").status, PERMIT_EXPIRED)


if __name__ == "__main__":
    unittest.main()
