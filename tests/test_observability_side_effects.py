import unittest

from observability.events import InMemoryObservabilitySink
from observability.health import HEALTH_BLOCKED, HEALTH_DEGRADED, HEALTH_HEALTHY
from observability.health import build_operational_health
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from side_effects.executor import SideEffectExecutor
from side_effects.models import STATUS_UNKNOWN, OUTCOME_UNCERTAIN
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import T0, allow_execute, eval_kwargs, runtime as se_runtime
from tools.models import TOOL_TRUST_INTERNAL_SAFE
from autonomy.gate import build_proposed_action
from side_effects.models import TEST_TOOL_ID
from autonomy.capabilities import CAP_EXTERNAL_WRITE


class ObservabilitySideEffectTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_increments_and_health_degraded(self):
        obs = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
        engine, workflow_id, adapter, executor = se_runtime(
            trust=TOOL_TRUST_INTERNAL_SAFE
        )
        executor.observability = obs
        engine.observability = obs
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-se",
            tool_id=TEST_TOOL_ID,
            operation="set_value",
            resource="test/key",
            idempotency_key="idem-unc",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            risk_class="low",
        )
        from tests.side_effect_fixtures import make_uncertain

        await make_uncertain(executor, action, engine)
        types = [e.event_type for e in obs.list_events()]
        self.assertIn("side_effect.uncertain", types)
        self.assertGreaterEqual(obs.metrics.snapshot()["side_effect_uncertain_total"], 1)
        health = obs.health()
        self.assertEqual(health.reconciliation_status, HEALTH_DEGRADED)
        self.assertEqual(health.overall_status, HEALTH_DEGRADED)
        self.assertEqual(adapter.calls, 1)  # no automatic retry


class OperationalHealthTests(unittest.TestCase):
    def test_healthy_baseline(self):
        snap = build_operational_health()
        self.assertEqual(snap.overall_status, HEALTH_HEALTHY)

    def test_blocked_protected_persistence(self):
        snap = build_operational_health(
            protected_state_ready=False, protected_write_required=True
        )
        self.assertEqual(snap.overall_status, HEALTH_BLOCKED)

    def test_degraded_dead_letter(self):
        snap = build_operational_health(dead_letter_count=25)
        self.assertEqual(snap.queue_status, HEALTH_DEGRADED)


if __name__ == "__main__":
    unittest.main()
