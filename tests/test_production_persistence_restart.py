import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autonomy.models import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    IDEMPOTENCY_COMPLETED,
)
from hitl.errors import ExecutionPermitConsumedError
from hitl.models import PERMIT_CONSUMED, PERMIT_ISSUED
from side_effects.models import STATUS_SUCCEEDED
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.runtime import compose_side_effect_runtime
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import ctx, eval_kwargs, se_action
from tests.test_github_write_config import DictSecrets
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.models import STATUS_WAITING_APPROVAL


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _compose(path, scan=False):
    return compose_side_effect_runtime(
        secrets=DictSecrets(),
        env={
            "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
            "SIDE_EFFECT_DB_PATH": path,
            "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "true" if scan else "false",
        },
    )


def _install_fake_adapter(runtime):
    adapter = InMemoryReversibleWriteAdapter(
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, reversible=True
    )
    registry = SideEffectAdapterRegistry()
    registry.register(adapter)
    runtime.executor.registry = registry
    # Fake reversible adapter is not the GitHub write path; skip GitHub activation.
    runtime.executor.activation = None
    runtime.workflow_engine.side_effect_executor = runtime.executor
    return adapter


class ProductionPersistenceRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_path_full_lineage_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "prod-life.sqlite3")

            # Runtime A — waiting_approval + pending approval (no manual attach)
            runtime_a = _compose(path)
            engine_a = runtime_a.workflow_engine
            _install_fake_adapter(runtime_a)
            workflow_id = engine_a.create("task-prod")
            engine_a.state_manager.plan(workflow_id)
            engine_a.state_manager.start(workflow_id)
            action = se_action(
                workflow_id,
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                idempotency_key="prod-life",
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
                runtime_a.persistence.approval_store.get(approval_id).status,
                APPROVAL_PENDING,
            )
            runtime_a.persistence.connection.close()

            # Runtime B — approve + permit
            runtime_b = _compose(path)
            engine_b = runtime_b.workflow_engine
            self.assertEqual(
                runtime_b.persistence.approval_store.get(approval_id).status,
                APPROVAL_PENDING,
            )
            self.assertEqual(
                runtime_b.persistence.workflow_runtime_store.get(workflow_id).status,
                STATUS_WAITING_APPROVAL,
            )
            engine_b.hitl_service.approve(
                approval_id, resolved_by="reviewer-1", now=T0
            )
            permit = engine_b.hitl_service.reevaluate_and_issue_permit(
                approval_id, action, **kwargs
            )
            self.assertIsNotNone(permit)
            self.assertEqual(permit.status, PERMIT_ISSUED)
            permit_id = permit.permit_id
            self.assertEqual(
                runtime_b.persistence.approval_store.get(approval_id).status,
                APPROVAL_APPROVED,
            )
            runtime_b.persistence.connection.close()

            # Runtime C — execute with durable permit
            runtime_c = _compose(path)
            engine_c = runtime_c.workflow_engine
            adapter_c = _install_fake_adapter(runtime_c)
            permit_c = runtime_c.persistence.permit_store.get(permit_id)
            self.assertEqual(permit_c.status, PERMIT_ISSUED)
            if engine_c.state_manager.get(workflow_id).status == STATUS_WAITING_APPROVAL:
                engine_c.state_manager.approve(workflow_id)
            result = await runtime_c.executor.execute(
                action,
                permit=permit_c,
                context=ctx("ok"),
                gate=runtime_c.autonomy_gate,
                hitl=runtime_c.hitl_service,
                state_manager=engine_c.state_manager,
                evaluate_kwargs=kwargs,
                now=T0,
            )
            self.assertEqual(result.status, STATUS_SUCCEEDED)
            self.assertEqual(adapter_c.calls, 1)
            self.assertEqual(
                runtime_c.persistence.permit_store.get(permit_id).status,
                PERMIT_CONSUMED,
            )
            execution_id = result.execution_id
            runtime_c.persistence.connection.close()

            # Runtime D — lineage
            runtime_d = _compose(path, scan=True)
            self.assertEqual(
                runtime_d.persistence.approval_store.get(approval_id).status,
                APPROVAL_APPROVED,
            )
            self.assertEqual(
                runtime_d.persistence.permit_store.get(permit_id).status,
                PERMIT_CONSUMED,
            )
            loaded = runtime_d.persistence.execution_store.get(execution_id)
            self.assertEqual(loaded.status, STATUS_SUCCEEDED)
            self.assertEqual(loaded.permit_id, permit_id)
            self.assertEqual(loaded.approval_id, approval_id)
            self.assertEqual(
                runtime_d.persistence.idempotency_registry.get("prod-life").state,
                IDEMPOTENCY_COMPLETED,
            )
            runtime_d.persistence.connection.close()

    async def test_consumed_permit_denied_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "prod-consume.sqlite3")
            runtime_a = _compose(path)
            engine_a = runtime_a.workflow_engine
            adapter_a = _install_fake_adapter(runtime_a)
            workflow_id = engine_a.create("task-consume")
            engine_a.state_manager.plan(workflow_id)
            engine_a.state_manager.start(workflow_id)
            action = se_action(
                workflow_id,
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                idempotency_key="prod-consume",
            )
            kwargs = eval_kwargs(
                "executor_confirmed", capabilities=action.requested_capabilities
            )
            engine_a.evaluate_action(action, requested_by="agent-1", **kwargs)
            approval_id = engine_a.last_approval_id
            engine_a.hitl_service.approve(
                approval_id, resolved_by="reviewer-1", now=T0
            )
            permit = engine_a.hitl_service.reevaluate_and_issue_permit(
                approval_id, action, **kwargs
            )
            if engine_a.state_manager.get(workflow_id).status == STATUS_WAITING_APPROVAL:
                engine_a.state_manager.approve(workflow_id)
            result = await runtime_a.executor.execute(
                action,
                permit=permit,
                context=ctx("ok"),
                gate=runtime_a.autonomy_gate,
                hitl=runtime_a.hitl_service,
                state_manager=engine_a.state_manager,
                evaluate_kwargs=kwargs,
                now=T0,
            )
            self.assertEqual(result.status, STATUS_SUCCEEDED)
            self.assertEqual(adapter_a.calls, 1)
            permit_id = permit.permit_id
            runtime_a.persistence.connection.close()

            runtime_b = _compose(path)
            engine_b = runtime_b.workflow_engine
            adapter_b = _install_fake_adapter(runtime_b)
            permit_b = runtime_b.persistence.permit_store.get(permit_id)
            self.assertEqual(permit_b.status, PERMIT_CONSUMED)
            # Completed idempotency returns prior success without new mutation.
            result2 = await runtime_b.executor.execute(
                action,
                permit=permit_b,
                context=ctx("again"),
                gate=runtime_b.autonomy_gate,
                hitl=runtime_b.hitl_service,
                state_manager=engine_b.state_manager,
                evaluate_kwargs=kwargs,
                now=T0,
            )
            self.assertEqual(result2.status, STATUS_SUCCEEDED)
            self.assertEqual(adapter_b.calls, 0)
            with self.assertRaises(ExecutionPermitConsumedError):
                runtime_b.hitl_service.consume_for_execution(
                    permit_id, action=action, now=T0
                )
            runtime_b.persistence.connection.close()


if __name__ == "__main__":
    unittest.main()
