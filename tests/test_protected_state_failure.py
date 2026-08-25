import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from autonomy.models import APPROVAL_APPROVED, ApprovalRecord
from hitl.models import PERMIT_CONSUMED, PERMIT_ISSUED, ExecutionPermit, action_fingerprint
from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.persistence import (
    attach_protected_persistence,
    build_side_effect_persistence,
)
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.executor import SideEffectExecutor
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import ctx, eval_kwargs, se_action
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_WAITING_APPROVAL
from workflow.state_manager import StateManager
from hitl.errors import ExecutionPermitConsumedError


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class ProtectedStateFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_permit_consume_db_failure_blocks_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "fail.sqlite3")
            bundle = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            engine = WorkflowEngine(
                state_manager=StateManager(store=bundle.workflow_runtime_store)
            )
            attach_protected_persistence(engine, bundle)
            adapter = InMemoryReversibleWriteAdapter(
                trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, reversible=True
            )
            registry = SideEffectAdapterRegistry()
            registry.register(adapter)
            executor = SideEffectExecutor(
                registry,
                gate=engine._gate(),
                store=bundle.execution_store,
                idempotency=bundle.idempotency_registry,
                persistence=bundle,
                permit_service=engine.hitl_service.permits,
            )
            workflow_id = engine.create("task-fail")
            engine.state_manager.plan(workflow_id)
            engine.state_manager.start(workflow_id)
            action = se_action(
                workflow_id,
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                idempotency_key="fail-key",
            )
            kwargs = eval_kwargs(
                "executor_confirmed", capabilities=action.requested_capabilities
            )
            engine.evaluate_action(action, requested_by="agent-1", **kwargs)
            approval_id = engine.last_approval_id
            engine.hitl_service.approve(approval_id, resolved_by="reviewer-1", now=T0)
            permit = engine.hitl_service.reevaluate_and_issue_permit(
                approval_id, action, **kwargs
            )
            self.assertIsNotNone(permit)

            real_consume = engine.hitl_service.consume_for_execution

            def boom(*args, **kwargs):
                raise SideEffectPersistenceUnavailableError(
                    "protected_state_persistence_unavailable"
                )

            with mock.patch.object(
                engine.hitl_service, "consume_for_execution", side_effect=boom
            ):
                with self.assertRaises(SideEffectPersistenceUnavailableError):
                    await executor.execute(
                        action,
                        permit=permit,
                        context=ctx("x"),
                        gate=engine._gate(),
                        hitl=engine.hitl_service,
                        state_manager=engine.state_manager,
                        evaluate_kwargs=kwargs,
                        now=T0,
                    )
            self.assertEqual(adapter.calls, 0)
            # Permit must not be marked consumed by failed path
            loaded = bundle.permit_store.get(permit.permit_id)
            self.assertEqual(loaded.status, PERMIT_ISSUED)
            _ = real_consume
            bundle.connection.close()

    async def test_crash_after_consume_no_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "crash.sqlite3")
            bundle = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            engine = WorkflowEngine(
                state_manager=StateManager(store=bundle.workflow_runtime_store)
            )
            attach_protected_persistence(engine, bundle)
            adapter = InMemoryReversibleWriteAdapter(
                trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, reversible=True
            )
            registry = SideEffectAdapterRegistry()
            registry.register(adapter)
            executor = SideEffectExecutor(
                registry,
                gate=engine._gate(),
                store=bundle.execution_store,
                idempotency=bundle.idempotency_registry,
                persistence=bundle,
                permit_service=engine.hitl_service.permits,
            )
            workflow_id = engine.create("task-crash")
            engine.state_manager.plan(workflow_id)
            engine.state_manager.start(workflow_id)
            action = se_action(
                workflow_id,
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                idempotency_key="crash-key",
            )
            kwargs = eval_kwargs(
                "executor_confirmed", capabilities=action.requested_capabilities
            )
            engine.evaluate_action(action, requested_by="agent-1", **kwargs)
            approval_id = engine.last_approval_id
            engine.hitl_service.approve(approval_id, resolved_by="reviewer-1", now=T0)
            permit = engine.hitl_service.reevaluate_and_issue_permit(
                approval_id, action, **kwargs
            )
            # Simulate consume-then-crash before adapter
            engine.hitl_service.consume_for_execution(
                permit.permit_id, action=action, now=T0
            )
            self.assertEqual(
                bundle.permit_store.get(permit.permit_id).status, PERMIT_CONSUMED
            )
            bundle.connection.close()

            bundle2 = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            engine2 = WorkflowEngine(
                state_manager=StateManager(store=bundle2.workflow_runtime_store)
            )
            attach_protected_persistence(engine2, bundle2)
            adapter2 = InMemoryReversibleWriteAdapter(
                trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, reversible=True
            )
            registry2 = SideEffectAdapterRegistry()
            registry2.register(adapter2)
            executor2 = SideEffectExecutor(
                registry2,
                gate=engine2._gate(),
                store=bundle2.execution_store,
                idempotency=bundle2.idempotency_registry,
                persistence=bundle2,
                permit_service=engine2.hitl_service.permits,
            )
            if engine2.state_manager.get(workflow_id).status == STATUS_WAITING_APPROVAL:
                engine2.state_manager.approve(workflow_id)
            permit2 = bundle2.permit_store.get(permit.permit_id)
            with self.assertRaises(Exception):
                await executor2.execute(
                    action,
                    permit=permit2,
                    context=ctx("x"),
                    gate=engine2._gate(),
                    hitl=engine2.hitl_service,
                    state_manager=engine2.state_manager,
                    evaluate_kwargs=kwargs,
                    now=T0,
                )
            self.assertEqual(adapter2.calls, 0)
            bundle2.connection.close()

    def test_p7d_schema_migrates_to_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "migrate.sqlite3")
            # Simulate P7D-era DB: create v1 meta only then open with current code
            import sqlite3

            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE side_effect_schema_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO side_effect_schema_meta(id, version) VALUES (1, 1);
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
                INSERT INTO side_effect_executions (
                    execution_id, workflow_id, task_id, action_id, tool_id, operation,
                    status, outcome, authorization_type, authorization_id,
                    idempotency_key_hash, attempt, started_at, rollback_status
                ) VALUES (
                    'ex-old', 'wf', 't', 'a', 'test.write', 'set_value',
                    'succeeded', 'known_success', 'autonomy_decision', 'd1',
                    'hash', 1, '2026-08-25T12:00:00+00:00', 'none'
                );
                """
            )
            conn.commit()
            conn.close()

            bundle = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            self.assertEqual(bundle.schema_version, 2)
            self.assertTrue(bundle.protected_state_ready)
            old = bundle.execution_store.get("ex-old")
            self.assertIsNotNone(old)
            self.assertEqual(old.status, "succeeded")
            # New tables usable
            bundle.approval_store.create(
                ApprovalRecord(
                    approval_id="ap-mig",
                    workflow_id="wf",
                    task_id="t",
                    action_id="a",
                    decision_id="d",
                    status=APPROVAL_APPROVED,
                    approved_by="reviewer-1",
                    created_at=T0,
                    version=1,
                    action_fingerprint="fp",
                )
            )
            self.assertEqual(bundle.approval_store.get("ap-mig").status, APPROVAL_APPROVED)
            bundle.connection.close()


if __name__ == "__main__":
    unittest.main()
