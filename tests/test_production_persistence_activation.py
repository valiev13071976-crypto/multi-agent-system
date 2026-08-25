import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from side_effects.activation import ACTIVATION_BLOCKED
from side_effects.persistence import SideEffectPersistenceBundle, build_side_effect_persistence
from side_effects.runtime import compose_side_effect_runtime
from side_effects.github.transport import FakeGitHubTransport
from tests.test_github_write_config import DictSecrets


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class ProductionPersistenceActivationTests(unittest.IsolatedAsyncioTestCase):
    def test_sqlite_init_failure_blocks_real_write_no_memory_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bad.sqlite3")
            # Create unsupported newer schema so sqlite open fails closed.
            import sqlite3

            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE side_effect_schema_meta "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO side_effect_schema_meta(id, version) VALUES (1, 99)"
            )
            conn.commit()
            conn.close()

            runtime = compose_side_effect_runtime(
                secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                    "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
                    "GITHUB_WRITE_DRY_RUN": "false",
                    "GITHUB_WRITE_KILL_SWITCH": "false",
                    "GITHUB_WRITE_REQUIRE_PROBE_SUCCESS": "false",
                },
                transport=FakeGitHubTransport(),
            )
            self.assertFalse(runtime.persistence.ready)
            self.assertFalse(runtime.protected_persistence_attached)
            self.assertEqual(runtime.activation.state, ACTIVATION_BLOCKED)
            # Must not silently claim durable sqlite success.
            self.assertNotEqual(runtime.persistence.backend, "memory")
            decision = runtime.activation.evaluate(
                mock.Mock(resource="github://octo/hello/issues/1/labels/x"),
                mock.Mock(),
                purpose="mutate",
                now=T0,
            )
            self.assertTrue(decision.blocked)
            self.assertFalse(decision.allowed)
            self.assertIn(
                decision.reason_code,
                {
                    "side_effect_schema_version_unsupported",
                    "side_effect_persistence_unavailable",
                    "protected_state_persistence_unavailable",
                },
            )

    def test_protected_state_unavailable_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ok.sqlite3")
            base = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            # Simulate protected wiring failure while connection exists.
            broken = SideEffectPersistenceBundle(
                backend="sqlite",
                ready=True,
                connection=base.connection,
                execution_store=base.execution_store,
                idempotency_store=base.idempotency_store,
                reconciliation_store=base.reconciliation_store,
                idempotency_registry=base.idempotency_registry,
                encryption=base.encryption,
                schema_version=base.schema_version,
                database_path_ref=base.database_path_ref,
                approval_store=base.approval_store,
                permit_store=base.permit_store,
                workflow_runtime_store=base.workflow_runtime_store,
                protected_state_ready=False,
                reason_code="protected_state_persistence_unavailable",
            )
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
                env={
                    "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                    "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
                    "GITHUB_WRITE_DRY_RUN": "false",
                    "GITHUB_WRITE_KILL_SWITCH": "false",
                    "GITHUB_WRITE_REQUIRE_PROBE_SUCCESS": "false",
                },
                transport=FakeGitHubTransport(),
                persistence=broken,
            )
            self.assertEqual(runtime.activation.state, ACTIVATION_BLOCKED)
            self.assertEqual(
                runtime.activation.health().reason_code,
                "protected_state_persistence_unavailable",
            )
            self.assertFalse(runtime.protected_persistence_attached)
            base.connection.close()

    async def test_start_idempotent(self):
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env={
                "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
                "GITHUB_WRITE_DRY_RUN": "true",
                "GITHUB_WRITE_KILL_SWITCH": "true",
                "GITHUB_WRITE_PROBE_ON_STARTUP": "true",
            },
            transport=FakeGitHubTransport(),
        )
        calls = {"n": 0}
        original = runtime.activation.refresh

        async def counted(*args, **kwargs):
            calls["n"] += 1
            return await original(*args, **kwargs)

        runtime.activation.refresh = counted
        await runtime.start()
        await runtime.start()
        self.assertEqual(calls["n"], 1)
        self.assertTrue(runtime.startup_probe_ran)

    async def test_startup_scan_local_only(self):
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
            scan = runtime.recovery_scan
            self.assertIsNotNone(scan)
            self.assertEqual(scan.network_calls, 0)
            self.assertEqual(scan.mutation_calls, 0)
            await runtime.start()
            self.assertFalse(runtime.startup_probe_ran)
            runtime.persistence.connection.close()


if __name__ == "__main__":
    unittest.main()
