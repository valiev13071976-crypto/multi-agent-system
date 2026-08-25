import tempfile
import unittest
from pathlib import Path

from autonomy.idempotency import IdempotencyRegistry
from side_effects.persistence import build_side_effect_persistence
from side_effects.protected_state_store import (
    PersistentApprovalStore,
    PersistentExecutionPermitStore,
    PersistentWorkflowRuntimeStore,
)
from side_effects.runtime import compose_side_effect_runtime
from side_effects.sqlite_store import (
    PersistentIdempotencyStore,
    PersistentReconciliationStore,
    PersistentSideEffectExecutionStore,
)
from tests.test_github_write_config import DictSecrets


class ProductionPersistenceWiringTests(unittest.TestCase):
    def test_sqlite_compose_auto_wires_all_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "wire.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                },
            )
            self.assertIsInstance(
                runtime.persistence.execution_store, PersistentSideEffectExecutionStore
            )
            self.assertIsInstance(
                runtime.persistence.idempotency_store, PersistentIdempotencyStore
            )
            self.assertIsInstance(
                runtime.persistence.reconciliation_store, PersistentReconciliationStore
            )
            self.assertIsInstance(
                runtime.persistence.approval_store, PersistentApprovalStore
            )
            self.assertIsInstance(
                runtime.persistence.permit_store, PersistentExecutionPermitStore
            )
            self.assertIsInstance(
                runtime.persistence.workflow_runtime_store,
                PersistentWorkflowRuntimeStore,
            )
            self.assertIs(runtime.hitl_service.store, runtime.persistence.approval_store)
            self.assertIs(
                runtime.hitl_service.permits.store, runtime.persistence.permit_store
            )
            self.assertIs(
                runtime.workflow_engine.state_manager._store,
                runtime.persistence.workflow_runtime_store,
            )
            self.assertIs(
                runtime.executor.permit_service.store, runtime.persistence.permit_store
            )
            self.assertTrue(runtime.protected_persistence_attached)
            self.assertTrue(runtime.persistence.ready)
            runtime.persistence.connection.close()

    def test_memory_compose_preserves_in_memory(self):
        runtime = compose_side_effect_runtime(secrets=DictSecrets(), env={})
        self.assertEqual(runtime.persistence.backend, "memory")
        self.assertFalse(runtime.protected_persistence_attached)
        self.assertNotIsInstance(
            runtime.persistence.approval_store, PersistentApprovalStore
        )
        self.assertIsNotNone(runtime.hitl_service)
        self.assertIsNotNone(runtime.workflow_engine)
        self.assertIs(runtime.workflow_engine.side_effect_executor, runtime.executor)

    def test_health_reports_backends_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "health.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_secret_token_xyz"}),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "true",
                    "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                    "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
                    "GITHUB_WRITE_DRY_RUN": "true",
                    "GITHUB_WRITE_KILL_SWITCH": "true",
                },
            )
            health = runtime.health()
            meta = dict(health.metadata)
            self.assertEqual(meta["persistence_backend"], "sqlite")
            self.assertTrue(meta["persistence_ready"])
            self.assertTrue(meta["protected_persistence_attached"])
            self.assertEqual(meta["workflow_store_backend"], "sqlite")
            self.assertEqual(meta["approval_store_backend"], "sqlite")
            self.assertEqual(meta["permit_store_backend"], "sqlite")
            self.assertIn("recovery_scan", meta)
            blob = str(health) + str(meta) + repr(runtime.config)
            self.assertNotIn("ghs_secret_token_xyz", blob)
            self.assertNotIn(path, blob)
            runtime.persistence.connection.close()

    def test_shared_connection_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "shared.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                },
            )
            conn = runtime.persistence.connection
            self.assertIs(
                runtime.persistence.execution_store._connection, conn
            )
            self.assertIs(runtime.persistence.approval_store._connection, conn)
            self.assertIs(runtime.persistence.permit_store._connection, conn)
            self.assertIs(
                runtime.persistence.workflow_runtime_store._connection, conn
            )
            runtime.persistence.connection.close()


if __name__ == "__main__":
    unittest.main()
