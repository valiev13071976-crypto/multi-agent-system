import unittest

from side_effects.errors import SideEffectAlreadyCompletedError, SideEffectIdempotencyError
from side_effects.models import STATUS_SUCCEEDED
from tests.side_effect_fixtures import allow_execute, ctx, eval_kwargs, runtime, se_action


class SideEffectIdempotencyTests(unittest.IsolatedAsyncioTestCase):

    async def test_t_missing_key_denies(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, idempotency_key=None)
        with self.assertRaises(SideEffectIdempotencyError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_u_first_execution_succeeds(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, idempotency_key="first")
        result = await allow_execute(executor, action, engine, "one")
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(adapter.calls, 1)

    async def test_v_completed_key_does_not_recall_adapter(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, idempotency_key="once")
        first = await allow_execute(executor, action, engine, "one")
        second = await allow_execute(executor, action, engine, "two")
        self.assertEqual(first.status, STATUS_SUCCEEDED)
        self.assertEqual(second.execution_id, first.execution_id)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(adapter.data["test/key"], "one")

    async def test_w_active_key_blocks_duplicate(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, idempotency_key="active")
        engine._gate().evaluate(action, **eval_kwargs())
        engine._gate().idempotency.mark_started("active")
        other = se_action(
            workflow_id,
            action_id="other-action",
            idempotency_key="active",
        )
        with self.assertRaises(SideEffectIdempotencyError):
            await allow_execute(executor, other, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_x_adapter_receives_idempotency_key(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, idempotency_key="pass-key")
        await allow_execute(executor, action, engine)
        self.assertEqual(adapter.received_idempotency_keys, ["pass-key"])

    async def test_y_failed_execution_does_not_reset_key(self):
        engine, workflow_id, adapter, executor = runtime()
        adapter.fail_before_write = True
        action = se_action(workflow_id, idempotency_key="fail-key")
        with self.assertRaises(Exception):
            await allow_execute(executor, action, engine)
        adapter.fail_before_write = False
        with self.assertRaises(SideEffectIdempotencyError):
            await allow_execute(executor, action, engine, "retry")
        self.assertEqual(adapter.calls, 1)

    async def test_z_uncertain_blocks_rerun(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, idempotency_key="unc-key")
        context = ctx("x")
        context.simulate_finalization_failure = True
        kwargs = eval_kwargs()
        decision = engine._gate().evaluate(action, **kwargs)
        await executor.execute(
            action,
            decision=decision,
            context=context,
            gate=engine._gate(),
            state_manager=engine.state_manager,
            evaluate_kwargs=kwargs,
        )
        with self.assertRaises(SideEffectIdempotencyError):
            await executor.execute(
                action,
                decision=decision,
                context=ctx("y"),
                gate=engine._gate(),
                state_manager=engine.state_manager,
                evaluate_kwargs=kwargs,
            )
        self.assertEqual(adapter.calls, 1)


if __name__ == "__main__":
    unittest.main()
