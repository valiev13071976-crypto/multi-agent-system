"""Applied expansion closure — semantics, contract kit, security negative paths."""

from __future__ import annotations

import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_WRITE, CAP_FILESYSTEM_READ
from autonomy.models import utc_now
from tools.contract_kit import (
    AdapterContractTestCase,
    assert_descriptor_valid,
    assert_invocation_context,
)
from tools.errors import ToolPolicyDeniedError
from tools.gateway import ToolGateway
from tools.integration_families import ADAPTER_FILES, ADAPTER_WEB_SEARCH, INTEGRATION_FAMILIES
from tools.invocation import ToolInvocation, invocation_from_request
from tools.models import (
    APPROVAL_POLICY_NONE,
    APPROVAL_POLICY_REQUIRED,
    OP_WRITE,
    SIDE_EFFECT_CRITICAL,
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    ToolDescriptor,
    ToolRequest,
)
from tools.platform.fake import FakeToolAdapter
from tools.registry import ToolRegistry
from tools.side_effect_semantics import (
    SEMANTIC_DESTRUCTIVE,
    SEMANTIC_FINANCIAL_OR_HIGH_RISK,
    SEMANTIC_READ_ONLY,
    SEMANTIC_WRITE_EXTERNAL,
    enforce_side_effect_policy,
    resolve_semantic_side_effect,
)


def _read_desc(**overrides) -> ToolDescriptor:
    base = dict(
        tool_id="closure.read",
        name="Closure Read",
        description="read",
        version="1.0.0",
        trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
        capabilities_required=(CAP_FILESYSTEM_READ,),
        action_types_supported=("read",),
        operations=("read",),
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=5.0,
        category="files",
        adapter_id="files",
        metadata={"adapter_type": ADAPTER_FILES},
    )
    base.update(overrides)
    return ToolDescriptor(**base)


class SemanticSideEffectTests(unittest.TestCase):
    def test_read_only_mapping(self):
        desc = _read_desc()
        self.assertEqual(resolve_semantic_side_effect(desc, "read"), SEMANTIC_READ_ONLY)

    def test_write_external_mapping(self):
        desc = _read_desc(
            read_only=False,
            reversible=False,
            trust_level=TOOL_TRUST_INTERNAL_SAFE,
            side_effect_level=SIDE_EFFECT_CRITICAL,
            operation_class={OP_WRITE: OP_WRITE},
        )
        self.assertEqual(resolve_semantic_side_effect(desc, "write"), SEMANTIC_DESTRUCTIVE)

    def test_financial_metadata(self):
        desc = _read_desc(metadata={"adapter_type": ADAPTER_FILES, "financial_risk": True})
        self.assertEqual(resolve_semantic_side_effect(desc), SEMANTIC_FINANCIAL_OR_HIGH_RISK)

    def test_destructive_requires_approval_policy(self):
        desc = _read_desc(
            read_only=False,
            trust_level=TOOL_TRUST_INTERNAL_SAFE,
            side_effect_level=SIDE_EFFECT_CRITICAL,
            approval_policy=APPROVAL_POLICY_NONE,
            operations=("delete",),
            operation_class={"delete": "destructive"},
        )
        with self.assertRaises(ToolPolicyDeniedError):
            enforce_side_effect_policy(desc, "delete")

    def test_destructive_passes_with_approval_policy(self):
        desc = _read_desc(
            read_only=False,
            trust_level=TOOL_TRUST_INTERNAL_SAFE,
            side_effect_level=SIDE_EFFECT_CRITICAL,
            approval_policy=APPROVAL_POLICY_REQUIRED,
            operations=("delete",),
            operation_class={"delete": "destructive"},
        )
        enforce_side_effect_policy(desc, "delete")


class IntegrationFamilyTests(unittest.TestCase):
    def test_families_complete(self):
        self.assertIn(ADAPTER_WEB_SEARCH, INTEGRATION_FAMILIES)
        self.assertIn(ADAPTER_FILES, INTEGRATION_FAMILIES)


class InvocationEnvelopeTests(unittest.TestCase):
    def test_invocation_preserves_tenant(self):
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="closure.read",
            operation="read",
            tenant_id="tenant-z",
            user_id="u1",
            execution_id="exec-1",
            correlation_id="trace-1",
        )
        inv = invocation_from_request(req, invocation_id="inv-1")
        self.assertIsInstance(inv, ToolInvocation)
        assert_invocation_context(inv, tenant_id="tenant-z")
        self.assertEqual(inv.invocation_id, "inv-1")
        self.assertEqual(inv.trace_ref, "trace-1")


class FakeReadContractTests(AdapterContractTestCase):
    descriptor_factory = staticmethod(lambda: _read_desc())
    adapter_factory = staticmethod(lambda: FakeToolAdapter(adapter_id="files"))


class GatewaySideEffectNegativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_destructive_without_approval_denied_at_gateway(self):
        registry = ToolRegistry()
        desc = _read_desc(
            tool_id="closure.delete",
            read_only=False,
            trust_level=TOOL_TRUST_INTERNAL_SAFE,
            side_effect_level=SIDE_EFFECT_CRITICAL,
            approval_policy=APPROVAL_POLICY_NONE,
            capabilities_required=(CAP_EXTERNAL_WRITE,),
            operations=("delete",),
            operation_class={"delete": "destructive"},
        )
        registry.register(desc, adapter=FakeToolAdapter(adapter_id="files"))
        gw = ToolGateway(registry=registry, register_search=False)
        from autonomy.capabilities import CapabilitySet

        caps = CapabilitySet(
            subject_id="u1",
            capabilities=(CAP_EXTERNAL_WRITE,),
            issued_at=utc_now(),
        )
        result = await gw.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id="closure.delete",
                operation="delete",
                tenant_id="tenant-a",
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
            ),
            capabilities=caps,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "tool_policy_denied")


class ContractKitTests(unittest.TestCase):
    def test_assert_descriptor_valid(self):
        assert_descriptor_valid(_read_desc())

    def test_invalid_descriptor_rejected(self):
        with self.assertRaises(ValueError):
            ToolDescriptor(
                tool_id="",
                name="x",
                description="x",
                version="1",
                trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
                capabilities_required=(),
                action_types_supported=(),
                operations=(),
                read_only=True,
                reversible=True,
                idempotency_required=False,
                timeout_seconds=1.0,
            )


if __name__ == "__main__":
    unittest.main()
