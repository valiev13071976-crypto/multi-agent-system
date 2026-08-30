"""FH.13 / FH.14 — canonical ToolGateway + bypass rejection."""

from __future__ import annotations

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
from tools.models import (
    FORBIDDEN_BYPASS_KEYS,
    TOOL_STATUS_SUCCEEDED,
    TOOL_TRUST_INTERNAL_SAFE,
    ToolRequest,
)
from tools.registry import ToolRegistry
from workflow.engine import WorkflowEngine


class FHToolGatewayContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = WorkflowEngine()
        self.workflow_id = self.engine.create("t", tenant_id="tenant-se")
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
            idempotency_key="tool-idem-fh",
            actor_id="agent-1",
        )
        base.update(kwargs)
        return ToolRequest(**base)

    async def test_allowed_write_via_gateway(self):
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
        self.assertEqual(self.adapter.calls, 1)

    async def test_unknown_tool_reject(self):
        result = await self.gateway.invoke(
            self._req(tool_id="no.such.tool"),
            capabilities=self.capset,
            gate=self.gate,
            executor=self.executor,
            state_manager=self.engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertFalse(result.success)
        self.assertEqual(self.adapter.calls, 0)

    async def test_bypass_keys_forbidden(self):
        self.assertIn("bypass_hitl", FORBIDDEN_BYPASS_KEYS)
        result = await self.gateway.invoke(
            self._req(
                arguments={
                    "resource": "test/key",
                    "value": "v",
                    "bypass_hitl": True,
                }
            ),
            capabilities=self.capset,
            gate=self.gate,
            executor=self.executor,
            state_manager=self.engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        if result.success:
            self.assertEqual(self.adapter.calls, 1)
        data = getattr(result, "data", None) or {}
        self.assertNotIn("bypass_hitl", data)

    async def test_tenant_propagation_on_write(self):
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
        record = self.executor.store.get(result.execution_id)
        self.assertEqual(record.tenant_id, "tenant-se")


if __name__ == "__main__":
    unittest.main()
