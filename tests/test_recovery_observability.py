"""Recovery observability events and metrics."""

from __future__ import annotations

import unittest

from observability.events import InMemoryObservabilitySink
from observability.health import HEALTH_DEGRADED, build_operational_health
from observability.metrics import FORBIDDEN_LABEL_KEYS, MetricsCollector
from observability.runtime import ObservabilityRuntime
from recovery.models import CASE_MANUAL_REVIEW, CASE_UNCERTAIN_SIDE_EFFECT
from recovery.orchestrator import RecoveryOrchestrator


class RecoveryObservabilityTests(unittest.TestCase):
    def test_events_and_metrics(self):
        sink = InMemoryObservabilitySink()
        metrics = MetricsCollector()
        obs = ObservabilityRuntime(sink=sink, metrics=metrics)
        orch = RecoveryOrchestrator(
            observability=obs, enqueue_reconcile_on_create=False
        )
        orch.create_case(
            execution_id="e1",
            case_type=CASE_UNCERTAIN_SIDE_EFFECT,
            enqueue=False,
        )
        orch.create_case(
            execution_id="e2",
            case_type=CASE_MANUAL_REVIEW,
            enqueue=False,
        )
        types = {e.event_type for e in sink.list_events()}
        self.assertIn("recovery.case_created", types)
        self.assertGreaterEqual(metrics.recovery_cases_total, 1)
        snap = metrics.snapshot()
        for key in snap.get("by_label", {}).get("recovery_cases_total", {}):
            for forbidden in FORBIDDEN_LABEL_KEYS:
                self.assertNotIn(f"{forbidden}=", key)

    def test_health_degraded_with_open_cases(self):
        snap = build_operational_health(open_recovery_cases=2, pending_manual_review=1)
        self.assertEqual(snap.recovery_status, HEALTH_DEGRADED)
        self.assertEqual(snap.overall_status, HEALTH_DEGRADED)


if __name__ == "__main__":
    unittest.main()
