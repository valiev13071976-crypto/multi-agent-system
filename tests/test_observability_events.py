import unittest
from datetime import timezone

from observability.events import InMemoryObservabilitySink, make_event
from observability.runtime import ObservabilityRuntime, build_observability_runtime
from observability.metrics import MetricsCollector


class ObservabilityEventsTests(unittest.TestCase):
    def test_emit_utc_and_fifo(self):
        sink = InMemoryObservabilitySink(max_events=3)
        runtime = ObservabilityRuntime(sink=sink, metrics=MetricsCollector())
        ctx = runtime.create_context()
        for i in range(5):
            runtime.emit(
                "workflow.created",
                context=ctx.child() if i else ctx,
                status="created",
                metadata={"n": i},
            )
        events = sink.list_events()
        self.assertEqual(len(events), 3)
        self.assertTrue(events[0].timestamp.tzinfo)
        self.assertEqual(events[0].timestamp.tzinfo, timezone.utc)
        self.assertEqual(events[-1].metadata_safe.get("n"), 4)

    def test_oversized_metadata_truncated(self):
        event = make_event(
            "tool.requested",
            correlation_id="c",
            trace_id="t",
            span_id="s",
            metadata={"blob": "x" * 10000},
            max_bytes=200,
        )
        self.assertTrue(event.metadata_safe.get("metadata_truncated"))

    def test_sink_failure_does_not_break(self):
        class Boom:
            def emit(self, event):
                raise RuntimeError("sink_down")

        runtime = ObservabilityRuntime(sink=Boom(), metrics=MetricsCollector())
        result = runtime.emit("workflow.created", context=runtime.create_context())
        self.assertIsNone(result)
        self.assertGreaterEqual(runtime.emit_errors, 1)


if __name__ == "__main__":
    unittest.main()
