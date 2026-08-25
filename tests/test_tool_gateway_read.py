import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
from autonomy.models import utc_now
from tools.gateway import ToolGateway
from tools.models import (
    TOOL_STATUS_DENIED,
    TOOL_STATUS_SUCCEEDED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    ToolRequest,
)
from tools.search.fake_provider import FakeSearchProvider
from tools.url_safety import is_safe_http_url


class ToolGatewayReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = ToolGateway(FakeSearchProvider())
        self.caps = CapabilitySet(
            subject_id="a",
            capabilities=(CAP_EXTERNAL_READ,),
            issued_at=utc_now(),
        )

    def _req(self, **kwargs):
        base = dict(
            request_id=str(uuid.uuid4()),
            workflow_id="wf",
            task_id="t",
            tool_id="search",
            operation="search",
            arguments={"query": "hello", "max_results": 2},
            requested_capabilities=(CAP_EXTERNAL_READ,),
        )
        base.update(kwargs)
        return ToolRequest(**base)

    async def test_allowed_read_works(self):
        result = await self.gateway.invoke(self._req(), capabilities=self.caps)
        self.assertTrue(result.success)
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)
        self.assertFalse(result.side_effect)
        self.assertIsNone(result.permit_id)

    async def test_missing_capability_denied(self):
        result = await self.gateway.invoke(self._req(requested_capabilities=()))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_tool_capability")

    async def test_unsupported_op_denied(self):
        result = await self.gateway.invoke(self._req(operation="mutate"))
        self.assertEqual(result.error_code, "tool_operation_not_allowed")

    async def test_legacy_search_preserved(self):
        self.assertEqual(self.gateway.tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)
        rows = await self.gateway.search("q")
        self.assertIsInstance(rows, list)

    async def test_no_side_effect_executor_for_read(self):
        self.gateway.side_effect_executor = object()
        calls = {"n": 0}

        class Boom:
            async def execute(self, *a, **k):
                calls["n"] += 1

        self.gateway.side_effect_executor = Boom()
        await self.gateway.invoke(self._req(), capabilities=self.caps)
        self.assertEqual(calls["n"], 0)

    async def test_ssrf_protections_preserved(self):
        self.assertFalse(is_safe_http_url("http://127.0.0.1/"))
        result = await self.gateway.invoke(
            self._req(arguments={"query": "x"}), capabilities=self.caps
        )
        self.assertTrue(result.success or result.status == TOOL_STATUS_SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
