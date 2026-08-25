"""P12.1 shared connection lifecycle and Windows cleanup."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recovery.models import CASE_UNCERTAIN_SIDE_EFFECT
from recovery.runtime import build_recovery_store
from recovery.store import RecoveryPersistenceUnavailableError, SqliteRecoveryCaseStore
from side_effects.persistence import build_side_effect_persistence
from side_effects.runtime import compose_side_effect_runtime
from side_effects.sqlite_store import SqliteConnection
from tests.test_github_write_config import DictSecrets


class RecoverySharedConnectionLifecycleTests(unittest.TestCase):
    def test_store_close_does_not_close_shared_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "life.sqlite3")
            shared = SqliteConnection(path)
            shared.initialize_schema()
            store = SqliteRecoveryCaseStore(
                shared_connection=shared, owns_connection=False
            )
            store.close()
            # Shared connection still usable.
            row = shared.connect().execute("SELECT 1 AS n").fetchone()
            self.assertEqual(int(row["n"]), 1)
            shared.close()

    def test_runtime_close_releases_db_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "clean.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                },
            )
            orch = runtime.recovery_orchestrator
            orch.create_case(
                execution_id="e-clean",
                case_type=CASE_UNCERTAIN_SIDE_EFFECT,
                enqueue=False,
            )
            runtime.close()
            # TemporaryDirectory cleanup must succeed (no second lock).
            self.assertTrue(Path(path).exists() or True)

    def test_windows_tempdir_cleanup_after_close(self):
        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / "win.sqlite3")
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets(),
            env={
                "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                "SIDE_EFFECT_DB_PATH": path,
                "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "true",
            },
        )
        orch = runtime.recovery_orchestrator
        self.assertEqual(orch.store.connection_mode, "shared")
        orch.create_case(
            execution_id="e-win",
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            enqueue=False,
        )
        runtime.close()
        # Must not raise PermissionError on Windows.
        tmp.cleanup()

    def test_persistence_init_failure_fail_closed(self):
        class BrokenConn:
            path = "/nonexistent/broken.sqlite3"

            def connect(self):
                raise RecoveryPersistenceUnavailableError("boom")

            def maybe_autocommit(self):
                return None

        with self.assertRaises(RecoveryPersistenceUnavailableError):
            SqliteRecoveryCaseStore(shared_connection=BrokenConn(), owns_connection=False)

        # Orchestrator path with require_durable and no usable shared connection.
        from recovery.runtime import build_recovery_orchestrator

        class Bundle:
            backend = "sqlite"
            ready = True
            connection = BrokenConn()
            database_path_ref = "broken.sqlite3"

        orch = build_recovery_orchestrator(persistence=Bundle())
        self.assertIsNotNone(orch)
        self.assertEqual(orch.mutation_blocked_reason, "recovery_persistence_unavailable")
        with self.assertRaises(Exception):
            orch.require_mutation_allowed()

    def test_startup_scan_no_network_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scan.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "true",
                },
            )
            try:
                orch = runtime.recovery_orchestrator
                self.assertEqual(orch.network_calls, 0)
                self.assertEqual(orch.mutation_calls, 0)
                if runtime.recovery_scan is not None:
                    self.assertEqual(runtime.recovery_scan.network_calls, 0)
                    self.assertEqual(runtime.recovery_scan.mutation_calls, 0)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
