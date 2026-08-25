import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from side_effects.executor import SideEffectExecutor
from side_effects.models import TEST_TOOL_ID, default_test_descriptor
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import T0, caps, eval_kwargs
from tools.adapters import descriptor_from_side_effect
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_INTERNAL_SAFE, ToolRequest
from tools.registry import ToolRegistry
from workflow.engine import WorkflowEngine


class ToolGatewayIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = WorkflowEngine()
        self.workflow_id = self.engine.create("t")
        self.engine.state_manager.plan(self.workflow_id)
        self.engine.state_manager.start(self.workflow_id)
        self.adapter = InMemoryReversibleWriteAdapter(
            trust_level=TOOL_TRUST_INTERNAL_SAFE
        )
        se_reg = SideEffectAdapterRegistry()
        se_reg.register(self.adapter)
        self.gate = self.engine._gate()
        self.executor = SideEffectExecutor(se_reg, gate=self.gate)
        self.registry = ToolRegistry()
        self.registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_INTERNAL_SAFE),
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=self.adapter,
        )
        self.gateway = ToolGateway(
            registry=self.registry,
            side_effect_executor=self.executor,
            gate=self.gate,
            register_search=False,
        )

    def _req(self, **kwargs):
        base = dict(
            request_id=str(uuid.uuid4()),
            workflow_id=self.workflow_id,
            task_id="t",
            tool_id=TEST_TOOL_ID,
            operation="set_value",
            arguments={"resource": "test/key", "value": "v"},
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            actor_id="agent-1",
        )
        base.update(kwargs)
        return ToolRequest(**base)

    async def test_missing_key_denied(self):
        result = await self.gateway.invoke(
            self._req(idempotency_key=None),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=self.gate,
            executor=self.executor,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertEqual(result.error_code, "idempotency_key_required")
        self.assertEqual(self.adapter.calls, 0)

    async def test_completed_duplicate_not_replayed(self):
        first = await self.gateway.invoke(
            self._req(idempotency_key="dup-1"),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=self.gate,
            executor=self.executor,
            state_manager=self.engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertTrue(first.success, first.error_code)
        self.assertEqual(self.adapter.calls, 1)
        second = await self.gateway.invoke(
            self._req(idempotency_key="dup-1"),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=self.gate,
            executor=self.executor,
            state_manager=self.engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertTrue(second.success, second.error_code)
        self.assertEqual(self.adapter.calls, 1)

    async def test_uncertain_duplicate_blocked(self):
        self.gate.idempotency.reserve("unc-1", "action-a")
        self.gate.idempotency.mark_started("unc-1")
        self.gate.idempotency.mark_uncertain("unc-1")
        result = await self.gateway.invoke(
            self._req(idempotency_key="unc-1"),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=self.gate,
            executor=self.executor,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertEqual(result.error_code, "tool_side_effect_uncertain")
        self.assertEqual(self.adapter.calls, 0)


if __name__ == "__main__":
    unittest.main()
