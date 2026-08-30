"""Budget observability coverage (events/metrics)."""

from decimal import Decimal
import unittest

from finops.budget_guard import BudgetGuard
from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime


class BudgetObservabilityTests(unittest.TestCase):
    def test_events_and_metrics_bounded(self):
        obs = ObservabilityRuntime(sink=InMemoryObservabilitySink(), metrics=MetricsCollector())
        finops = FinOpsService(
            prices={("openai", "m"): PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)},
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("3")),),
            required=True,
            observability=obs,
        )
        guard.evaluate(task_id="t", provider="openai", model="m", estimated_cost=Decimal("5"))
        types = {e.event_type for e in obs.sink.list_events()}
        self.assertIn("budget.evaluated", types)
        self.assertGreaterEqual(obs.metrics.budget_skip_model_total, 1)


if __name__ == "__main__":
    unittest.main()
