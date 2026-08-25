import unittest

from autonomy.capabilities import CAP_EXTERNAL_READ
from side_effects.errors import (
    SideEffectAdapterAlreadyRegisteredError,
    SideEffectAdapterMismatchError,
    SideEffectAuthorizationError,
    SideEffectExecutionDeniedError,
    SideEffectExecutionError,
)
from side_effects.models import TEST_TOOL_ID, SideEffectToolDescriptor
from side_effects.registry import SideEffectAdapterRegistry, empty_adapter_registry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import allow_execute, runtime, se_action
from tools.models import (
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
)


class SideEffectAdapterTests(unittest.IsolatedAsyncioTestCase):

    def test_descriptor_fields(self):
        adapter = InMemoryReversibleWriteAdapter()
        descriptor = adapter.descriptor
        self.assertIsInstance(descriptor, SideEffectToolDescriptor)
        self.assertEqual(descriptor.tool_id, TEST_TOOL_ID)
        self.assertEqual(descriptor.trust_level, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE)
        self.assertTrue(descriptor.reversible)
        self.assertTrue(descriptor.supports_idempotency)
        self.assertFalse(descriptor.network_access)
        self.assertEqual(descriptor.operations, ("set_value",))

    def test_duplicate_registration_errors(self):
        registry = SideEffectAdapterRegistry()
        registry.register(InMemoryReversibleWriteAdapter())
        with self.assertRaises(SideEffectAdapterAlreadyRegisteredError):
            registry.register(InMemoryReversibleWriteAdapter())

    def test_unknown_tool_empty_registry(self):
        registry = empty_adapter_registry()
        self.assertIsNone(registry.get("test.reversible_store"))
        self.assertEqual(registry.list_descriptors(), ())

    async def test_n_missing_capability_denies(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, requested_capabilities=(CAP_EXTERNAL_READ,))
        with self.assertRaises(SideEffectAuthorizationError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_o_adapter_capability_mismatch_denies(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, requested_capabilities=())
        with self.assertRaises(SideEffectAuthorizationError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_p_trust_mismatch_denies(self):
        from tests.side_effect_fixtures import (
            ctx,
            eval_kwargs,
            hitl_runtime,
            issue_permit,
            se_action,
        )
        from tools.models import TOOL_TRUST_INTERNAL_SAFE

        engine, workflow_id, adapter, executor = hitl_runtime(
            trust=TOOL_TRUST_INTERNAL_SAFE
        )
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        with self.assertRaises(SideEffectAdapterMismatchError):
            await executor.execute(
                action,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_q_reversible_mismatch_denies(self):
        from tests.side_effect_fixtures import ctx, eval_kwargs, hitl_runtime, issue_permit

        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id,
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            metadata={"reversible": False},
        )
        permit = await issue_permit(engine, action)
        with self.assertRaises(SideEffectAdapterMismatchError):
            await executor.execute(
                action,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_r_unknown_operation_denies(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, operation="drop_table")
        with self.assertRaises(SideEffectAdapterMismatchError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_s_resource_outside_namespace_denies(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id, resource="prod/customers")
        with self.assertRaises(SideEffectAdapterMismatchError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_no_network_or_filesystem(self):
        adapter = InMemoryReversibleWriteAdapter()
        self.assertFalse(adapter.descriptor.network_access)
        self.assertEqual(adapter.data, {})

    async def test_engine_delegates_without_adapter_logic(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        from tests.side_effect_fixtures import ctx, eval_kwargs

        kwargs = eval_kwargs()
        decision = engine._gate().evaluate(action, **kwargs)
        result = await engine.execute_side_effect(
            action,
            decision=decision,
            context=ctx("delegated"),
            evaluate_kwargs=kwargs,
        )
        self.assertEqual(result.tool_id, TEST_TOOL_ID)
        self.assertEqual(adapter.data["test/key"], "delegated")


if __name__ == "__main__":
    unittest.main()
