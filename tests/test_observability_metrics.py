import unittest

from observability.metrics import HighCardinalityLabelError, MetricsCollector
from observability.runtime import ObservabilityRuntime
from observability.events import InMemoryObservabilitySink


class ObservabilityMetricsTests(unittest.TestCase):
    def test_counters_and_latency(self):
        m = MetricsCollector()
        m.inc("workflow_total", labels={"component": "workflow"})
        m.observe_latency("workflow", 12.5, labels={"component": "workflow"})
        snap = m.snapshot()
        self.assertEqual(snap["workflow_total"], 1)
        self.assertEqual(snap["latency"]["workflow"]["count"], 1.0)

    def test_high_cardinality_rejected(self):
        m = MetricsCollector()
        with self.assertRaises(HighCardinalityLabelError):
            m.inc("workflow_total", labels={"workflow_id": "wf-1"})

    def test_no_double_tool_count_via_runtime(self):
        runtime = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
        ctx = runtime.create_context()
        runtime.emit(
            "tool.completed",
            context=ctx,
            tool_id="search",
            operation="search",
            trust_level="READ_ONLY_EXTERNAL",
            status="succeeded",
            duration_ms=5,
        )
        snap = runtime.metrics.snapshot()
        self.assertEqual(snap["tool_calls_total"], 1)
        self.assertEqual(snap["tool_success_total"], 1)


if __name__ == "__main__":
    unittest.main()
