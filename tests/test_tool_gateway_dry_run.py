import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from side_effects.models import TEST_TOOL_ID, default_test_descriptor
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import T0, caps, eval_kwargs
from tools.adapters import descriptor_from_side_effect
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_INTERNAL_SAFE, ToolRequest
from tools.registry import ToolRegistry
from workflow.engine import WorkflowEngine


class ToolGatewayDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_dry_run_zero_mutation(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("t")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        adapter = InMemoryReversibleWriteAdapter(trust_level=TOOL_TRUST_INTERNAL_SAFE)
        se_reg = SideEffectAdapterRegistry()
        se_reg.register(adapter)
        gate = engine._gate()

        class DryExecutor:
            mutate_calls = 0

            async def dry_run(self, action, **kwargs):
                class Planned:
                    would_execute = True
                    would_change = True
                    would_require_approval = False

                return Planned()

            async def execute(self, *a, **k):
                self.mutate_calls += 1
                raise AssertionError("execute must not run on dry_run")

        dry_exec = DryExecutor()
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_INTERNAL_SAFE),
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        gateway = ToolGateway(
            registry=registry,
            side_effect_executor=dry_exec,
            gate=gate,
            register_search=False,
        )
        result = await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                task_id="t",
                tool_id=TEST_TOOL_ID,
                operation="set_value",
                arguments={"resource": "test/key", "value": "v"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="dry-idem-1",
                dry_run=True,
            ),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=gate,
            executor=dry_exec,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertTrue(result.success, result.error_code)
        self.assertTrue(result.data.get("dry_run"))
        self.assertIn("would_execute", dict(result.data))
        self.assertIn("policy_decision", dict(result.data))
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(dry_exec.mutate_calls, 0)
        self.assertFalse(result.metadata.get("permit_consumed"))
        self.assertIsNone(gate.idempotency.get("dry-idem-1"))


if __name__ == "__main__":
    unittest.main()
