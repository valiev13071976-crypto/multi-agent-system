import unittest

from side_effects.errors import (
    RollbackExecutionError,
    RollbackNotSupportedError,
    SideEffectAuthorizationError,
)
from side_effects.models import ROLLBACK_FAILED, ROLLBACK_SUCCEEDED, STATUS_SUCCEEDED
from tests.side_effect_fixtures import (
    allow_execute,
    ctx,
    eval_kwargs,
    runtime,
    se_action,
)
from tools.models import TOOL_TRUST_INTERNAL_SAFE


class SideEffectRollbackTests(unittest.IsolatedAsyncioTestCase):

    async def test_ai_successful_execution_has_rollback_reference(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        result = await allow_execute(executor, action, engine, "before")
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertTrue(result.rollback_reference)

    async def test_aj_authorized_rollback_restores_prior(self):
        engine, workflow_id, adapter, executor = runtime()
        adapter._values["test/key"] = "prior"
        action = se_action(workflow_id, idempotency_key="write-1")
        result = await allow_execute(executor, action, engine, "new")
        self.assertEqual(adapter.data["test/key"], "new")
        rollback_action = se_action(
            workflow_id,
            idempotency_key="rollback-1",
            tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
        )
        kwargs = eval_kwargs()
        decision = engine._gate().evaluate(rollback_action, **kwargs)
        await executor.rollback(
            result.execution_id,
            action=rollback_action,
            decision=decision,
            context=ctx(),
            gate=engine._gate(),
            evaluate_kwargs=kwargs,
        )
        self.assertEqual(adapter.data["test/key"], "prior")
        record = executor.store.get(result.execution_id)
        self.assertEqual(record.rollback_status, ROLLBACK_SUCCEEDED)

    async def test_ak_rollback_without_authorization_denies(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        result = await allow_execute(executor, action, engine, "new")
        rollback_action = se_action(workflow_id, idempotency_key="rb-unauth")
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.rollback(
                result.execution_id,
                action=rollback_action,
                context=ctx(),
                gate=engine._gate(),
            )
        self.assertEqual(adapter.data["test/key"], "new")

    async def test_al_duplicate_rollback_idempotency(self):
        engine, workflow_id, adapter, executor = runtime()
        adapter._values["test/key"] = "prior"
        action = se_action(workflow_id, idempotency_key="w2")
        result = await allow_execute(executor, action, engine, "new")
        rollback_action = se_action(workflow_id, idempotency_key="rb-dup")
        kwargs = eval_kwargs()
        decision = engine._gate().evaluate(rollback_action, **kwargs)
        await executor.rollback(
            result.execution_id,
            action=rollback_action,
            decision=decision,
            gate=engine._gate(),
            evaluate_kwargs=kwargs,
        )
        await executor.rollback(
            result.execution_id,
            action=rollback_action,
            decision=decision,
            gate=engine._gate(),
            evaluate_kwargs=kwargs,
        )
        self.assertEqual(adapter.rollback_calls, 1)
        self.assertEqual(adapter.data["test/key"], "prior")

    async def test_am_non_reversible_adapter(self):
        from tests.side_effect_fixtures import (
            ctx,
            eval_kwargs,
            hitl_runtime,
            issue_permit,
        )
        from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE

        engine, workflow_id, adapter, executor = hitl_runtime(reversible=False)
        action = se_action(
            workflow_id,
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            metadata={"reversible": False},
        )
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=ctx("x"),
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs("executor_confirmed"),
        )
        rollback_action = se_action(
            workflow_id,
            idempotency_key="rb-nr",
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            metadata={"reversible": False},
        )
        rollback_permit = await issue_permit(engine, rollback_action)
        with self.assertRaises(RollbackNotSupportedError):
            await executor.rollback(
                result.execution_id,
                action=rollback_action,
                permit=rollback_permit,
                gate=engine._gate(),
                hitl=engine._hitl(),
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )

    async def test_an_rollback_failure_does_not_falsify_original(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, idempotency_key="w3")
        result = await allow_execute(executor, action, engine, "kept")
        adapter.fail_rollback = True
        rollback_action = se_action(workflow_id, idempotency_key="rb-fail")
        kwargs = eval_kwargs()
        decision = engine._gate().evaluate(rollback_action, **kwargs)
        with self.assertRaises(RollbackExecutionError):
            await executor.rollback(
                result.execution_id,
                action=rollback_action,
                decision=decision,
                gate=engine._gate(),
                evaluate_kwargs=kwargs,
            )
        record = executor.store.get(result.execution_id)
        self.assertEqual(record.status, STATUS_SUCCEEDED)
        self.assertEqual(record.rollback_status, ROLLBACK_FAILED)
        self.assertEqual(adapter.data["test/key"], "kept")


if __name__ == "__main__":
    unittest.main()
