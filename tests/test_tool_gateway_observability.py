import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
from autonomy.models import utc_now
from tools.gateway import ToolGateway
from tools.models import ToolRequest
from tools.observability import ToolMetrics
from tools.search.fake_provider import FakeSearchProvider


class ToolGatewayObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_increment_low_cardinality(self):
        metrics = ToolMetrics()
        gateway = ToolGateway(FakeSearchProvider(), metrics=metrics)
        caps = CapabilitySet(
            subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now()
        )
        await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id="search",
                operation="search",
                arguments={"query": "q"},
                requested_capabilities=(CAP_EXTERNAL_READ,),
            ),
            capabilities=caps,
        )
        await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id="search",
                operation="mutate",
                arguments={"query": "q"},
                requested_capabilities=(CAP_EXTERNAL_READ,),
            ),
            capabilities=caps,
        )
        snap = metrics.snapshot()
        self.assertGreaterEqual(snap["tool_calls_total"], 2)
        self.assertGreaterEqual(snap["tool_success_total"], 1)
        self.assertGreaterEqual(snap["tool_denied_total"], 1)
        for key in snap["by_tool"]:
            parts = key.split("|")
            self.assertEqual(len(parts), 3)
            self.assertNotIn("/", key)
            self.assertNotIn("http", key.lower())


if __name__ == "__main__":
    unittest.main()
