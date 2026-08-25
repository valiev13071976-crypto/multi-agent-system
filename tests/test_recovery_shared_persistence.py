"""P12.1 shared SQLite recovery persistence wiring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recovery.models import (
    ACTION_RECONCILE_READ_ONLY,
    CASE_UNCERTAIN_SIDE_EFFECT,
    DECISION_DEFER,
    SEVERITY_HIGH,
    STATUS_OPEN,
    RecoveryCase,
    utc_now,
)
from recovery.orchestrator import RecoveryOrchestrator
from recovery.runtime import build_recovery_orchestrator, build_recovery_store
from recovery.store import InMemoryRecoveryCaseStore, SqliteRecoveryCaseStore
from side_effects.persistence import build_side_effect_persistence
from side_effects.runtime import compose_side_effect_runtime
from side_effects.sqlite_store import SqliteConnection
from tests.test_github_write_config import DictSecrets


class RecoverySharedPersistenceTests(unittest.TestCase):
    def test_sqlite_unset_recovery_db_path_is_durable_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "shared.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "RECOVERY_ORCHESTRATION_ENABLED": "true",
                },
            )
            try:
                orch = runtime.recovery_orchestrator
                self.assertIsNotNone(orch)
                store = orch.store
                self.assertIsInstance(store, SqliteRecoveryCaseStore)
                self.assertEqual(store.connection_mode, "shared")
                self.assertFalse(store.owns_connection)
                self.assertIs(store._shared, runtime.persistence.connection)
                case = orch.create_case(
                    execution_id="e1",
                    case_type=CASE_UNCERTAIN_SIDE_EFFECT,
                    enqueue=False,
                )
                self.assertEqual(case.execution_id, "e1")
            finally:
                runtime.close()

    def test_same_path_override_reuses_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "same.sqlite3")
            resolved = str(Path(path).resolve())
            bundle = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            try:
                store = build_recovery_store(
                    env={"RECOVERY_DB_PATH": resolved},
                    shared_connection=bundle.connection,
                    side_effect_db_path=bundle.connection.path,
                )
                self.assertEqual(store.connection_mode, "shared")
                self.assertIs(store._shared, bundle.connection)
                self.assertFalse(store.owns_connection)
            finally:
                bundle.connection.close()

    def test_different_path_override_dedicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            se = str(Path(tmp) / "se.sqlite3")
            rec = str(Path(tmp) / "rec.sqlite3")
            bundle = build_side_effect_persistence(
                durable=True, db_path=se, run_recovery_scan=False
            )
            try:
                store = build_recovery_store(
                    env={"RECOVERY_DB_PATH": rec},
                    shared_connection=bundle.connection,
                    side_effect_db_path=bundle.connection.path,
                )
                self.assertEqual(store.connection_mode, "dedicated")
                self.assertTrue(store.owns_connection)
                self.assertIsNone(store._shared)
                store.close()
            finally:
                bundle.connection.close()

    def test_memory_mode_unchanged(self):
        runtime = compose_side_effect_runtime(secrets=DictSecrets(), env={})
        orch = runtime.recovery_orchestrator
        self.assertIsNotNone(orch)
        self.assertIsInstance(orch.store, InMemoryRecoveryCaseStore)
        self.assertEqual(orch.store.connection_mode, "memory")

    def test_health_reports_connection_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "health.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                },
            )
            try:
                health = runtime.health()
                rec = dict(health.metadata).get("recovery") or {}
                self.assertEqual(rec.get("connection_mode"), "shared")
                self.assertEqual(rec.get("persistence_backend"), "sqlite")
                self.assertTrue(rec.get("persistence_ready"))
                self.assertNotIn(path, str(rec))
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
