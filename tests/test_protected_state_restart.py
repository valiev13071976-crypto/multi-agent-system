import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autonomy.models import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    IDEMPOTENCY_COMPLETED,
)
from hitl.errors import ApprovalInvalidStateError, ExecutionPermitConsumedError
from hitl.models import PERMIT_CONSUMED, PERMIT_ISSUED
from side_effects.models import STATUS_SUCCEEDED
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


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _bundle(path, scan=False):
    return build_side_effect_persistence(
        durable=True, db_path=path, run_recovery_scan=scan
    )


def _engine_with_bundle(bundle):
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
    engine.side_effect_executor = executor
    return engine, adapter, executor


class ProtectedStateRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_lifecycle_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "p7e-life.sqlite3")

            bundle_a = _bundle(path)
            engine_a, _adapter_a, _exec_a = _engine_with_bundle(bundle_a)
            workflow_id = engine_a.create("task-p7e")
            engine_a.state_manager.plan(workflow_id)
            engine_a.state_manager.start(workflow_id)
            action = se_action(
                workflow_id,
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                idempotency_key="p7e-life",
                metadata={"reversible": True},
            )
            kwargs = eval_kwargs(
                "executor_confirmed", capabilities=action.requested_capabilities
            )
            engine_a.evaluate_action(action, requested_by="agent-1", **kwargs)
            approval_id = engine_a.last_approval_id
            self.assertIsNotNone(approval_id)
            self.assertEqual(
                engine_a.state_manager.get(workflow_id).status, STATUS_WAITING_APPROVAL
            )
            self.assertEqual(
                bundle_a.approval_store.get(approval_id).status, APPROVAL_PENDING
            )
            bundle_a.connection.close()

            bundle_b = _bundle(path)
            engine_b, _adapter_b, _exec_b = _engine_with_bundle(bundle_b)
            self.assertEqual(
                bundle_b.approval_store.get(approval_id).status, APPROVAL_PENDING
            )
            self.assertEqual(
                bundle_b.workflow_runtime_store.get(workflow_id).status,
                STATUS_WAITING_APPROVAL,
            )
            engine_b.hitl_service.approve(approval_id, resolved_by="reviewer-1", now=T0)
            permit = engine_b.hitl_service.reevaluate_and_issue_permit(
                approval_id, action, **kwargs
            )
            self.assertIsNotNone(permit)
            self.assertEqual(permit.status, PERMIT_ISSUED)
            permit_id = permit.permit_id
            self.assertEqual(
                bundle_b.approval_store.get(approval_id).status, APPROVAL_APPROVED
            )
            bundle_b.connection.close()

            bundle_c = _bundle(path)
            engine_c, adapter_c, executor_c = _engine_with_bundle(bundle_c)
            permit_c = bundle_c.permit_store.get(permit_id)
            self.assertEqual(permit_c.status, PERMIT_ISSUED)
            if engine_c.state_manager.get(workflow_id).status == STATUS_WAITING_APPROVAL:
                engine_c.state_manager.approve(workflow_id)
            result = await executor_c.execute(
                action,
                permit=permit_c,
                context=ctx("ok"),
                gate=engine_c._gate(),
                hitl=engine_c.hitl_service,
                state_manager=engine_c.state_manager,
                evaluate_kwargs=kwargs,
                now=T0,
            )
            self.assertEqual(result.status, STATUS_SUCCEEDED)
            self.assertEqual(adapter_c.calls, 1)
            self.assertEqual(
                bundle_c.permit_store.get(permit_id).status, PERMIT_CONSUMED
            )
            execution = bundle_c.execution_store.get(result.execution_id)
            self.assertEqual(execution.permit_id, permit_id)
            self.assertEqual(execution.approval_id, approval_id)
            bundle_c.connection.close()

            bundle_d = _bundle(path, scan=True)
            self.assertEqual(
                bundle_d.approval_store.get(approval_id).status, APPROVAL_APPROVED
            )
            self.assertEqual(
                bundle_d.permit_store.get(permit_id).status, PERMIT_CONSUMED
            )
            self.assertEqual(
                bundle_d.execution_store.get(result.execution_id).status,
                STATUS_SUCCEEDED,
            )
            self.assertEqual(
                bundle_d.idempotency_registry.get("p7e-life").state,
                IDEMPOTENCY_COMPLETED,
            )
            self.assertIsNotNone(bundle_d.last_scan)
            with self.assertRaises(ExecutionPermitConsumedError):
                engine = WorkflowEngine(
                    state_manager=StateManager(
                        store=bundle_d.workflow_runtime_store
                    )
                )
                attach_protected_persistence(engine, bundle_d)
                engine.hitl_service.consume_for_execution(
                    permit_id, action=action, now=T0
                )
            bundle_d.connection.close()

    async def test_rejection_blocks_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "p7e-rej.sqlite3")
            bundle_a = _bundle(path)
            engine_a, adapter_a, _executor_a = _engine_with_bundle(bundle_a)
            workflow_id = engine_a.create("task-rej")
            engine_a.state_manager.plan(workflow_id)
            engine_a.state_manager.start(workflow_id)
            action = se_action(
                workflow_id,
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                idempotency_key="p7e-rej",
            )
            kwargs = eval_kwargs(
                "executor_confirmed", capabilities=action.requested_capabilities
            )
            engine_a.evaluate_action(action, requested_by="agent-1", **kwargs)
            approval_id = engine_a.last_approval_id
            bundle_a.connection.close()

            bundle_b = _bundle(path)
            engine_b, adapter_b, _executor_b = _engine_with_bundle(bundle_b)
            engine_b.hitl_service.reject(
                approval_id, resolved_by="reviewer-1", now=T0
            )
            self.assertEqual(
                bundle_b.approval_store.get(approval_id).status, APPROVAL_REJECTED
            )
            with self.assertRaises(ApprovalInvalidStateError):
                engine_b.hitl_service.reevaluate_and_issue_permit(
                    approval_id, action, **kwargs
                )
            self.assertEqual(adapter_b.calls, 0)
            self.assertEqual(adapter_a.calls, 0)
            bundle_b.connection.close()


if __name__ == "__main__":
    unittest.main()
