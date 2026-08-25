import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
from autonomy.models import utc_now
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from tools.gateway import ToolGateway
from tools.models import ToolRequest
from tools.search.fake_provider import FakeSearchProvider


class ObservabilityToolGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_tool_events_no_double_metrics(self):
        obs = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
        gateway = ToolGateway(FakeSearchProvider(), observability=obs)
        caps = CapabilitySet(
            subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now()
        )
        result = await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id="search",
                operation="search",
                arguments={"query": "hello"},
                requested_capabilities=(CAP_EXTERNAL_READ,),
            ),
            capabilities=caps,
        )
        self.assertTrue(result.success)
        types = [e.event_type for e in obs.list_events()]
        self.assertIn("tool.requested", types)
        self.assertIn("tool.started", types)
        self.assertIn("tool.completed", types)
        snap = obs.metrics.snapshot()
        self.assertEqual(snap["tool_calls_total"], 1)
        self.assertEqual(snap["tool_success_total"], 1)
        # Compatibility wrapper must not double-count.
        self.assertEqual(gateway.metrics.snapshot()["tool_calls_total"], 1)


if __name__ == "__main__":
    unittest.main()
