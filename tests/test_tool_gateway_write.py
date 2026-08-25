import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from hitl.authority import InMemoryApprovalAuthority, ROLE_PRIVILEGED_APPROVER
from hitl.service import HITLService
from side_effects.executor import SideEffectExecutor
from side_effects.models import TEST_TOOL_ID, default_test_descriptor
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import T0, caps, eval_kwargs
from tools.adapters import descriptor_from_side_effect
from tools.gateway import ToolGateway
from tools.models import (
    TOOL_STATUS_APPROVAL_REQUIRED,
    TOOL_STATUS_SUCCEEDED,
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    ToolRequest,
)
from tools.registry import ToolRegistry
from workflow.engine import WorkflowEngine


class ToolGatewayWriteTests(unittest.IsolatedAsyncioTestCase):
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
                name=TEST_TOOL_ID,
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
        self.capset = caps(CAP_EXTERNAL_WRITE)

    def _req(self, **kwargs):
        base = dict(
            request_id=str(uuid.uuid4()),
            workflow_id=self.workflow_id,
            task_id="t",
            tool_id=TEST_TOOL_ID,
            operation="set_value",
            arguments={"resource": "test/key", "value": "v"},
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            idempotency_key="tool-idem-1",
            actor_id="agent-1",
        )
        base.update(kwargs)
        return ToolRequest(**base)

    async def test_write_goes_through_executor_not_adapter_directly(self):
        result = await self.gateway.invoke(
            self._req(),
            capabilities=self.capset,
            gate=self.gate,
            executor=self.executor,
            state_manager=self.engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertTrue(result.success, result.error_code)
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)
        self.assertTrue(result.side_effect)
        self.assertEqual(self.adapter.calls, 1)
        self.assertIsNotNone(result.execution_id)

    async def test_approval_required_path(self):
        adapter = InMemoryReversibleWriteAdapter(
            trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        se_reg = SideEffectAdapterRegistry()
        se_reg.register(adapter)
        executor = SideEffectExecutor(se_reg, gate=self.gate)
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(
                    trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
                ),
                name=TEST_TOOL_ID,
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        authority = InMemoryApprovalAuthority()
        authority.grant("reviewer-1", ROLE_PRIVILEGED_APPROVER)
        hitl = HITLService(
            gate=self.gate,
            state_manager=self.engine.state_manager,
            store=self.gate.approvals.store,
            authority=authority,
        )
        gateway = ToolGateway(
            registry=registry,
            side_effect_executor=executor,
            gate=self.gate,
            hitl=hitl,
            register_search=False,
        )
        result = await gateway.invoke(
            self._req(idempotency_key="tool-idem-approval"),
            capabilities=self.capset,
            gate=self.gate,
            hitl=hitl,
            executor=executor,
            state_manager=self.engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertEqual(result.status, TOOL_STATUS_APPROVAL_REQUIRED)
        self.assertEqual(result.error_code, "tool_approval_required")
        self.assertEqual(adapter.calls, 0)
        self.assertIsNotNone(result.approval_id)

    async def test_bypass_flags_denied(self):
        result = await self.gateway.invoke(
            self._req(metadata={"skip_gate": True}),
            capabilities=self.capset,
            gate=self.gate,
            executor=self.executor,
            now=T0,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "tool_policy_denied")
        self.assertEqual(self.adapter.calls, 0)


if __name__ == "__main__":
    unittest.main()
