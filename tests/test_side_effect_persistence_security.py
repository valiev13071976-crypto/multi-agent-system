import tempfile
import unittest
from pathlib import Path

from security.encryption import EncryptionService, EncryptionUnavailableError
from side_effects.github.activation import GitHubWriteActivationService
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.activation import PURPOSE_DRY_RUN, PURPOSE_MUTATE
from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.executor import SideEffectExecutor
from side_effects.models import STATUS_SUCCEEDED, STATUS_UNKNOWN, OUTCOME_UNCERTAIN
from side_effects.persistence import build_side_effect_persistence
from side_effects.recovery_scan import scan_recovery_candidates
from side_effects.sqlite_store import PersistentSideEffectExecutionStore, SqliteConnection
from tests.side_effect_fixtures import allow_execute, runtime, se_action
from autonomy.models import IDEMPOTENCY_STARTED, IDEMPOTENCY_UNCERTAIN
from autonomy.idempotency import IdempotencyRegistry
from side_effects.sqlite_store import PersistentIdempotencyStore
from side_effects.models import (
    AUTHORIZATION_AUTONOMY_DECISION,
    OUTCOME_KNOWN_FAILURE,
    ROLLBACK_NONE,
    STATUS_STARTED,
    SideEffectExecutionRecord,
)
from datetime import datetime, timezone


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class SideEffectPersistenceSecurityTests(unittest.TestCase):

    def test_ai_aj_ak_no_secrets_in_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sec.sqlite3")
            key = EncryptionService.generate_key() if hasattr(EncryptionService, "generate_key") else None
            if key is None:
                import base64, os
                raw = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
                encryption = EncryptionService(key=base64.urlsafe_b64decode(raw), key_id="v1")
            else:
                encryption = EncryptionService(key=key, key_id="v1")
            bundle = build_side_effect_persistence(
                durable=True, db_path=path, encryption=encryption, run_recovery_scan=False
            )
            store = bundle.execution_store
            store.create(
                SideEffectExecutionRecord(
                    execution_id="e1",
                    action_id="a1",
                    workflow_id="w1",
                    task_id="t1",
                    tool_id="github.issue_labels",
                    operation="ensure_label_present",
                    status=STATUS_SUCCEEDED,
                    authorization_type=AUTHORIZATION_AUTONOMY_DECISION,
                    authorization_id="x",
                    idempotency_key_hash="h",
                    attempt=1,
                    started_at=T0,
                    completed_at=T0,
                    outcome="known_success",
                    rollback_status=ROLLBACK_NONE,
                    metadata={"note": "ok"},
                )
            )
            bundle.connection.close()
            raw = Path(path).read_bytes()
            for marker in (
                b"GITHUB_WRITE_TOKEN",
                b"PANDA_ENCRYPTION_KEY",
                b"Authorization",
                b"Bearer ",
                b"ghs_",
            ):
                self.assertNotIn(marker, raw)

    def test_af_sensitive_without_encryption_fails(self):
        from security.encryption import SENSITIVITY_SENSITIVE
        from side_effects.sqlite_store import _split_payload

        with self.assertRaises(EncryptionUnavailableError):
            _split_payload(
                {"secret": "value"},
                sensitivity=SENSITIVITY_SENSITIVE,
                encryption=None,
            )


class SideEffectPersistenceFailureTests(unittest.IsolatedAsyncioTestCase):

    async def test_w_db_failure_before_mutate_zero_calls(self):
        engine, workflow_id, adapter, executor = runtime()

        class BoomStore:
            def get(self, execution_id):
                return None

            def create(self, record):
                raise SideEffectPersistenceUnavailableError()

            def save(self, record):
                raise SideEffectPersistenceUnavailableError()

            def find_by_idempotency(self, key_hash):
                return None

        executor.store = BoomStore()
        executor.require_durable_persistence = False
        action = se_action(workflow_id, idempotency_key="boom-key")
        with self.assertRaises(SideEffectPersistenceUnavailableError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_z_finalization_failure_uncertain_no_retry(self):
        engine, workflow_id, adapter, executor = runtime()
        executor.simulate_finalization_failure = True
        action = se_action(workflow_id, idempotency_key="fin-fail")
        result = await allow_execute(executor, action, engine, "x")
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.outcome, OUTCOME_UNCERTAIN)
        self.assertEqual(adapter.calls, 1)
        with self.assertRaises(Exception):
            await allow_execute(executor, action, engine, "retry")
        self.assertEqual(adapter.calls, 1)

    def test_ao_as_recovery_scan_local_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scan.sqlite3")
            conn = SqliteConnection(path)
            conn.initialize_schema()
            exec_store = PersistentSideEffectExecutionStore(conn)
            idem = PersistentIdempotencyStore(conn)
            reg = IdempotencyRegistry(idem)
            reg.reserve("s1", "a1")
            reg.mark_started("s1")
            reg.reserve("u1", "a2")
            reg.mark_started("u1")
            reg.mark_uncertain("u1")
            exec_store.create(
                SideEffectExecutionRecord(
                    execution_id="e-started",
                    action_id="a1",
                    workflow_id="w",
                    task_id="t",
                    tool_id="test.tool",
                    operation="set_value",
                    status=STATUS_STARTED,
                    authorization_type=AUTHORIZATION_AUTONOMY_DECISION,
                    authorization_id="",
                    idempotency_key_hash="h",
                    attempt=1,
                    started_at=T0,
                    completed_at=None,
                    outcome=OUTCOME_KNOWN_FAILURE,
                    rollback_status=ROLLBACK_NONE,
                )
            )
            scan = scan_recovery_candidates(
                execution_store=exec_store,
                idempotency_store=idem,
                reconciliation_store=None,
            )
            self.assertGreaterEqual(scan.stale_started_count, 1)
            self.assertGreaterEqual(scan.uncertain_count, 1)
            self.assertEqual(scan.network_calls, 0)
            self.assertEqual(scan.mutation_calls, 0)
            conn.close()


class SideEffectPersistenceActivationTests(unittest.TestCase):

    def test_at_real_mode_persistence_unavailable_blocked(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=False,
        )
        service = GitHubWriteActivationService(
            config=config, registered=True, persistence_ready=False
        )
        from tests.side_effect_fixtures import github_action
        from workflow.engine import WorkflowEngine

        engine = WorkflowEngine()
        wid = engine.create("t")
        action = github_action(wid)
        decision = service.evaluate(action, None, purpose=PURPOSE_MUTATE)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason_code, "side_effect_persistence_unavailable")

    def test_au_dry_run_without_persistence_ok(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=True,
            kill_switch=False,
        )
        service = GitHubWriteActivationService(
            config=config, registered=True, persistence_ready=False
        )
        from tests.side_effect_fixtures import github_action
        from workflow.engine import WorkflowEngine

        engine = WorkflowEngine()
        wid = engine.create("t")
        action = github_action(wid)
        decision = service.evaluate(action, None, purpose=PURPOSE_DRY_RUN)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.dry_run)

    def test_aw_kill_switch_overrides_persistence(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=True,
        )
        service = GitHubWriteActivationService(
            config=config, registered=True, persistence_ready=True
        )
        from tests.side_effect_fixtures import github_action
        from workflow.engine import WorkflowEngine

        engine = WorkflowEngine()
        wid = engine.create("t")
        action = github_action(wid)
        decision = service.evaluate(action, None, purpose=PURPOSE_MUTATE)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason_code, "github_write_kill_switch_active")


if __name__ == "__main__":
    unittest.main()
