"""P1-SE-TENANT — first-class tenant_id on side_effect_executions."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from side_effects.errors import SideEffectExecutionDeniedError
from side_effects.models import (
    OUTCOME_KNOWN_SUCCESS,
    ROLLBACK_NONE,
    STATUS_SUCCEEDED,
    SideEffectExecutionRecord,
)
from side_effects.schema import SCHEMA_VERSION
from side_effects.sqlite_store import PersistentSideEffectExecutionStore, SqliteConnection
from side_effects.store import InMemorySideEffectExecutionStore
from tests.side_effect_fixtures import allow_execute, runtime, se_action
from workflow.engine import WorkflowEngine


T0 = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _record(**overrides) -> SideEffectExecutionRecord:
    base = dict(
        execution_id="exec-1",
        action_id="act-1",
        workflow_id="wf-1",
        task_id="task-1",
        tool_id="test.reversible_store",
        operation="set_value",
        status=STATUS_SUCCEEDED,
        authorization_type="autonomy_decision",
        authorization_id="auth-1",
        idempotency_key_hash="hash-1",
        attempt=1,
        started_at=T0,
        completed_at=T0,
        outcome=OUTCOME_KNOWN_SUCCESS,
        rollback_status=ROLLBACK_NONE,
        tenant_id="tenant-a",
    )
    base.update(overrides)
    return SideEffectExecutionRecord(**base)


class SideEffectTenantOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_1_new_write_persists_tenant_id(self):
        engine, workflow_id, adapter, executor = runtime(tenant_id="tenant-write")
        action = se_action(workflow_id, idempotency_key="se-tenant-1")
        result = await allow_execute(executor, action, engine)
        record = executor.store.get(result.execution_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.tenant_id, "tenant-write")

    async def test_2_missing_tenant_fail_closed(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("task-se")  # no tenant
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        _, _, _, executor = runtime()
        # Reuse adapter registry from fixture executor, but target tenant-less workflow.
        action = se_action(workflow_id, idempotency_key="se-missing-tenant")
        with self.assertRaises(SideEffectExecutionDeniedError) as ctx:
            await allow_execute(executor, action, engine)
        self.assertEqual(ctx.exception.error_code, "side_effect_tenant_required")

    async def test_3_two_tenants_do_not_mix(self):
        engine_a, wf_a, _, exec_a = runtime(tenant_id="tenant-a")
        engine_b, wf_b, _, exec_b = runtime(tenant_id="tenant-b")
        # Share one in-memory store across executors to prove isolation by field.
        shared = InMemorySideEffectExecutionStore()
        exec_a.store = shared
        exec_b.store = shared
        await allow_execute(
            exec_a, se_action(wf_a, idempotency_key="key-a"), engine_a
        )
        await allow_execute(
            exec_b, se_action(wf_b, idempotency_key="key-b"), engine_b
        )
        rows_a = shared.list_by_tenant("tenant-a")
        rows_b = shared.list_by_tenant("tenant-b")
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_a[0].tenant_id, "tenant-a")
        self.assertEqual(rows_b[0].tenant_id, "tenant-b")
        self.assertEqual(rows_a[0].workflow_id, wf_a)
        self.assertEqual(rows_b[0].workflow_id, wf_b)

    async def test_4_direct_lookup_preserves_tenant(self):
        engine, workflow_id, _, executor = runtime(tenant_id="tenant-lookup")
        result = await allow_execute(
            executor, se_action(workflow_id, idempotency_key="lookup-key"), engine
        )
        loaded = executor.store.get(result.execution_id)
        self.assertEqual(loaded.tenant_id, "tenant-lookup")
        self.assertEqual(loaded.workflow_id, workflow_id)

    def test_5_legacy_create_without_tenant_rejected(self):
        store = InMemorySideEffectExecutionStore()
        with self.assertRaises(SideEffectExecutionDeniedError) as ctx:
            store.create(_record(tenant_id=""))
        self.assertEqual(ctx.exception.error_code, "side_effect_tenant_required")


class SideEffectTenantMigrationTests(unittest.TestCase):
    def test_6_v7_migrates_to_v8_and_backfills_known_workflow_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v7.sqlite3")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE side_effect_schema_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO side_effect_schema_meta(id, version) VALUES (1, 7);
                CREATE TABLE workflow_runtime_state (
                    workflow_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    current_step TEXT,
                    waiting_reason TEXT,
                    approval_id TEXT,
                    action_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    failed_at TEXT,
                    error_code TEXT,
                    execution_key TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    sensitivity TEXT NOT NULL DEFAULT 'internal',
                    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
                    encrypted_payload_json TEXT,
                    tenant_id TEXT
                );
                CREATE TABLE side_effect_executions (
                    execution_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    resource_ref TEXT,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    authorization_type TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    idempotency_key_hash TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    external_reference TEXT,
                    reversible INTEGER NOT NULL DEFAULT 0,
                    rollback_reference TEXT,
                    rollback_status TEXT NOT NULL,
                    error_code TEXT,
                    parent_execution_id TEXT,
                    reconciliation_id TEXT,
                    recovery_attempt INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    sensitivity TEXT NOT NULL DEFAULT 'internal',
                    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
                    encrypted_payload_json TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO workflow_runtime_state (
                    workflow_id, task_id, state, created_at, updated_at,
                    execution_key, tenant_id, safe_metadata_json
                ) VALUES ('wf-known', 't1', 'running', ?, ?, 'ek-1', 'tenant-known', '{}')
                """,
                (T0.isoformat(), T0.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO side_effect_executions (
                    execution_id, workflow_id, task_id, action_id, tool_id, operation,
                    status, outcome, authorization_type, authorization_id,
                    idempotency_key_hash, attempt, started_at, rollback_status,
                    safe_metadata_json
                ) VALUES (
                    'exec-known', 'wf-known', 't1', 'act-1', 'tool', 'op',
                    'succeeded', 'known_success', 'autonomy_decision', 'a',
                    'h1', 1, ?, 'none', '{}'
                )
                """,
                (T0.isoformat(),),
            )
            conn.execute(
                """
                INSERT INTO side_effect_executions (
                    execution_id, workflow_id, task_id, action_id, tool_id, operation,
                    status, outcome, authorization_type, authorization_id,
                    idempotency_key_hash, attempt, started_at, rollback_status,
                    safe_metadata_json
                ) VALUES (
                    'exec-orphan', 'wf-missing', 't2', 'act-2', 'tool', 'op',
                    'succeeded', 'known_success', 'autonomy_decision', 'a',
                    'h2', 1, ?, 'none', '{}'
                )
                """,
                (T0.isoformat(),),
            )
            conn.commit()
            conn.close()

            sqlite = SqliteConnection(path)
            version = sqlite.initialize_schema()
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertEqual(SCHEMA_VERSION, 8)
            store = PersistentSideEffectExecutionStore(sqlite)
            known = store.get("exec-known")
            orphan = store.get("exec-orphan")
            self.assertEqual(known.tenant_id, "tenant-known")
            # Legacy unresolved: empty string contract.
            self.assertEqual(orphan.tenant_id, "")
            cols = {
                str(r["name"])
                for r in sqlite.connect()
                .execute("PRAGMA table_info(side_effect_executions)")
                .fetchall()
            }
            self.assertIn("tenant_id", cols)
            sqlite.close()

    def test_7_metadata_backfill_when_workflow_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "meta.sqlite3")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE side_effect_schema_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO side_effect_schema_meta(id, version) VALUES (1, 7);
                CREATE TABLE side_effect_executions (
                    execution_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    resource_ref TEXT,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    authorization_type TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    idempotency_key_hash TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    external_reference TEXT,
                    reversible INTEGER NOT NULL DEFAULT 0,
                    rollback_reference TEXT,
                    rollback_status TEXT NOT NULL,
                    error_code TEXT,
                    parent_execution_id TEXT,
                    reconciliation_id TEXT,
                    recovery_attempt INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    sensitivity TEXT NOT NULL DEFAULT 'internal',
                    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
                    encrypted_payload_json TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO side_effect_executions (
                    execution_id, workflow_id, task_id, action_id, tool_id, operation,
                    status, outcome, authorization_type, authorization_id,
                    idempotency_key_hash, attempt, started_at, rollback_status,
                    safe_metadata_json
                ) VALUES (
                    'exec-meta', 'wf-x', 't', 'a', 'tool', 'op',
                    'succeeded', 'known_success', 'autonomy_decision', 'a',
                    'hm', 1, ?, 'none', ?
                )
                """,
                (T0.isoformat(), json.dumps({"tenant_id": "tenant-from-meta"})),
            )
            conn.commit()
            conn.close()
            sqlite = SqliteConnection(path)
            sqlite.initialize_schema()
            store = PersistentSideEffectExecutionStore(sqlite)
            self.assertEqual(store.get("exec-meta").tenant_id, "tenant-from-meta")
            sqlite.close()

    def test_8_persistence_round_trip_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "rt.sqlite3")
            sqlite = SqliteConnection(path)
            sqlite.initialize_schema()
            store = PersistentSideEffectExecutionStore(sqlite)
            created = store.create(_record(execution_id="exec-rt", tenant_id="tenant-rt"))
            self.assertEqual(created.tenant_id, "tenant-rt")
            loaded = store.get("exec-rt")
            self.assertEqual(loaded.tenant_id, "tenant-rt")
            sqlite.close()


if __name__ == "__main__":
    unittest.main()
