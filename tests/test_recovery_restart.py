"""Restart durability and dedup for recovery cases."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autonomy.models import utc_now
from recovery.orchestrator import RecoveryOrchestrator
from recovery.store import SqliteRecoveryCaseStore
from side_effects.models import (
    AUTHORIZATION_AUTONOMY_DECISION,
    OUTCOME_UNCERTAIN,
    STATUS_UNKNOWN,
    SideEffectExecutionRecord,
)
from side_effects.store import InMemorySideEffectExecutionStore


class RecoveryRestartTests(unittest.TestCase):
    def test_startup_scan_creates_once(self):
        exec_store = InMemorySideEffectExecutionStore()
        stamp = utc_now()
        exec_store.save(
            SideEffectExecutionRecord(
                execution_id="exec-u",
                action_id="a",
                workflow_id="w",
                task_id="t",
                tool_id="tool",
                operation="op",
                status=STATUS_UNKNOWN,
                authorization_type=AUTHORIZATION_AUTONOMY_DECISION,
                authorization_id="d1",
                idempotency_key_hash="h",
                attempt=1,
                started_at=stamp,
                completed_at=stamp,
                outcome=OUTCOME_UNCERTAIN,
                tenant_id="tenant-se",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rec.sqlite3"
            store = SqliteRecoveryCaseStore(path)
            orch = RecoveryOrchestrator(store=store, enqueue_reconcile_on_create=False)
            first = orch.materialize_from_local_scan(
                execution_store=exec_store, enqueue=False
            )
            self.assertEqual(first["network_calls"], 0)
            self.assertEqual(first["mutation_calls"], 0)
            self.assertEqual(len(orch.list_open_cases()), 1)
            store.close()

            store2 = SqliteRecoveryCaseStore(path)
            orch2 = RecoveryOrchestrator(store=store2, enqueue_reconcile_on_create=False)
            second = orch2.materialize_from_local_scan(
                execution_store=exec_store, enqueue=False
            )
            self.assertEqual(len(orch2.list_open_cases()), 1)
            self.assertEqual(second["network_calls"], 0)
            self.assertEqual(second["mutation_calls"], 0)
            store2.close()


if __name__ == "__main__":
    unittest.main()
