"""Operator decisions durability."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recovery.models import (
    CASE_UNCERTAIN_SIDE_EFFECT,
    DECISION_BLOCK,
    DECISION_CANCEL,
    DECISION_DEFER,
    DECISION_RECONCILE,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_WAITING_OPERATOR,
)
from recovery.orchestrator import RecoveryOrchestrator
from recovery.store import SqliteRecoveryCaseStore


class RecoveryOperatorDecisionTests(unittest.TestCase):
    def test_decisions_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rec.sqlite3"
            store = SqliteRecoveryCaseStore(path)
            orch = RecoveryOrchestrator(store=store, enqueue_reconcile_on_create=False)
            case = orch.create_case(
                execution_id="e1",
                case_type=CASE_UNCERTAIN_SIDE_EFFECT,
                enqueue=False,
            )
            orch.record_decision(
                case.recovery_id, DECISION_DEFER, actor_id="op", reason_code="later"
            )
            store.close()
            store2 = SqliteRecoveryCaseStore(path)
            orch2 = RecoveryOrchestrator(store=store2, enqueue_reconcile_on_create=False)
            loaded = orch2.get_case(case.recovery_id)
            self.assertEqual(loaded.operator_decision, DECISION_DEFER)
            self.assertEqual(loaded.status, STATUS_WAITING_OPERATOR)
            self.assertEqual(len(store2.list_decisions(case.recovery_id)), 1)
            store2.close()

    def test_block_and_cancel(self):
        orch = RecoveryOrchestrator(enqueue_reconcile_on_create=False)
        a = orch.create_case(execution_id="e2", case_type=CASE_UNCERTAIN_SIDE_EFFECT, enqueue=False)
        orch.record_decision(a.recovery_id, DECISION_BLOCK, actor_id="op", reason_code="stop")
        self.assertEqual(orch.get_case(a.recovery_id).status, STATUS_BLOCKED)
        b = orch.create_case(execution_id="e3", case_type=CASE_UNCERTAIN_SIDE_EFFECT, enqueue=False)
        orch.record_decision(b.recovery_id, DECISION_CANCEL, actor_id="op", reason_code="cancel")
        self.assertEqual(orch.get_case(b.recovery_id).status, STATUS_CANCELLED)
        c = orch.create_case(execution_id="e4", case_type=CASE_UNCERTAIN_SIDE_EFFECT, enqueue=False)
        orch.record_decision(c.recovery_id, DECISION_RECONCILE, actor_id="op", reason_code="check")
        self.assertEqual(orch.get_case(c.recovery_id).operator_decision, DECISION_RECONCILE)


if __name__ == "__main__":
    unittest.main()
