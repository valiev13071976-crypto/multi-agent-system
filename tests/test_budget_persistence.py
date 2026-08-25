from decimal import Decimal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from agents.provider_result import ProviderResult
from finops.budget_guard import BudgetGuard
from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
from finops.budget_store import BudgetPersistenceUnavailableError, InMemoryBudgetStore, SqliteBudgetStore
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime


class BoomStore(InMemoryBudgetStore):
    def begin_reserve_transaction(self):
        raise BudgetPersistenceUnavailableError()


class BudgetPersistenceObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistence_failure_zero_provider_calls(self):
        finops = FinOpsService(
            prices={("openai", "m"): PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)},
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("100")),),
            store=BoomStore(),
            required=True,
        )

        class Agent:
            model = "m"

            async def run(self, prompt):
                return ProviderResult("x", "openai", "m", 10, 10, 20)

        manager = ExpertManager(openai=Agent(), finops=finops, budget_guard=guard)
        with self.assertRaises(FinOpsBudgetDeniedError):
            await manager.run("prompt")
        self.assertEqual(manager.provider_calls, 0)

    def test_sqlite_schema_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteBudgetStore(Path(tmp) / "b.db")
            try:
                conn = store._connect()
                names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("finops_budget_reservations", names)
                self.assertIn("finops_budget_ledger", names)
                self.assertIn("finops_budget_policies", names)
            finally:
                store.close()

    def test_observability_events_no_secrets(self):
        obs = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
        finops = FinOpsService(
            prices={("openai", "m"): PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)},
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("10")),),
            required=True,
            observability=obs,
        )
        guard.evaluate(task_id="t", provider="openai", model="m", estimated_cost=Decimal("1"))
        r = guard.reserve(task_id="t", provider="openai", model="m", estimated_cost=Decimal("1"))
        guard.reconcile(r.reservation_id, actual_cost=Decimal("1"))
        blob = str([e.event_type for e in obs.sink.list_events()])
        self.assertIn("budget.evaluated", blob)
        self.assertIn("budget.reserved", blob)
        full = str([(e.event_type, dict(e.metadata_safe)) for e in obs.sink.list_events()])
        for needle in ("GITHUB_WRITE_TOKEN", "Bearer ", "sk-", "Authorization"):
            self.assertNotIn(needle, full)


if __name__ == "__main__":
    unittest.main()
