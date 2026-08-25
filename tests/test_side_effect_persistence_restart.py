import tempfile
import unittest
from pathlib import Path

from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import IDEMPOTENCY_COMPLETED, IDEMPOTENCY_UNCERTAIN
from side_effects.models import STATUS_SUCCEEDED
from side_effects.persistence import build_side_effect_persistence
from side_effects.sqlite_store import PersistentIdempotencyStore, SqliteConnection
from tests.side_effect_fixtures import allow_execute, runtime, se_action


class SideEffectPersistenceRestartTests(unittest.IsolatedAsyncioTestCase):

    async def test_h_i_j_execution_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "p7d.sqlite3")
            bundle_a = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            engine, workflow_id, adapter, executor = runtime()
            executor.store = bundle_a.execution_store
            executor.idempotency = bundle_a.idempotency_registry
            executor.persistence = bundle_a
            action = se_action(workflow_id, idempotency_key="restart-key")
            result = await allow_execute(executor, action, engine, "ok")
            execution_id = result.execution_id
            self.assertEqual(result.status, STATUS_SUCCEEDED)
            bundle_a.connection.close()

            bundle_b = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            loaded = bundle_b.execution_store.get(execution_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.status, STATUS_SUCCEEDED)
            self.assertEqual(loaded.action_id, action.action_id)
            # rollback can locate original without seed
            self.assertTrue(bool(loaded.rollback_reference) or loaded.reversible is not None)
            self.assertEqual(
                bundle_b.idempotency_registry.get("restart-key").state,
                IDEMPOTENCY_COMPLETED,
            )
            bundle_b.connection.close()

    def test_l_m_idempotency_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "idem.sqlite3")
            conn_a = SqliteConnection(path)
            conn_a.initialize_schema()
            store_a = PersistentIdempotencyStore(conn_a)
            reg_a = IdempotencyRegistry(store_a)
            reg_a.reserve("done-key", "a1")
            reg_a.mark_completed("done-key")
            reg_a.reserve("unc-key", "a2")
            reg_a.mark_started("unc-key")
            reg_a.mark_uncertain("unc-key")
            conn_a.close()

            conn_b = SqliteConnection(path)
            conn_b.initialize_schema()
            store_b = PersistentIdempotencyStore(conn_b)
            reg_b = IdempotencyRegistry(store_b)
            self.assertEqual(reg_b.get("done-key").state, IDEMPOTENCY_COMPLETED)
            self.assertEqual(reg_b.get("unc-key").state, IDEMPOTENCY_UNCERTAIN)
            conn_b.close()


if __name__ == "__main__":
    unittest.main()
