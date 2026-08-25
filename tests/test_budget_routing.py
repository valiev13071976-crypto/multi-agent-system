from decimal import Decimal
import unittest

from agents.model_router import ModelRouter
from finops.budget_guard import BudgetGuard
from finops.budget_models import BudgetConstraints, BudgetPolicy, SCOPE_GLOBAL
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService
from tests.test_model_router import registry_with


class BudgetRoutingTests(unittest.TestCase):
    def test_degrade_prefers_cheaper_capable(self):
        finops = FinOpsService(
            prices={
                ("openai", "premium"): PriceQuote("openai", "premium", Decimal("5"), Decimal("5"), "USD", True),
                ("anthropic", "cheap"): PriceQuote("anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True),
            },
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(
                BudgetPolicy(
                    "g",
                    SCOPE_GLOBAL,
                    hard_limit=Decimal("20"),
                    soft_limit=Decimal("15"),
                    degrade_threshold=Decimal("15"),
                ),
            ),
            required=True,
        )
        guard.store.add_spent("global:", Decimal("10"))
        d = guard.evaluate(
            task_id="t",
            provider="openai",
            model="premium",
            estimated_cost=Decimal("6"),
            capable_candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        constraints = guard.constraints_for_router(d)
        router = ModelRouter(registry_with("openai", "anthropic"))
        decision = router.decide("auto", "technical", category="technical", budget_constraints=constraints)
        self.assertEqual(decision.provider_ids, ("anthropic",))

    def test_incapable_cheaper_terminates_path(self):
        finops = FinOpsService(
            prices={
                ("openai", "premium"): PriceQuote("openai", "premium", Decimal("5"), Decimal("5"), "USD", True),
            },
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(
                BudgetPolicy(
                    "g",
                    SCOPE_GLOBAL,
                    hard_limit=Decimal("20"),
                    soft_limit=Decimal("15"),
                    degrade_threshold=Decimal("15"),
                ),
            ),
            required=True,
        )
        guard.store.add_spent("global:", Decimal("10"))
        d = guard.evaluate(
            task_id="t",
            provider="openai",
            model="premium",
            estimated_cost=Decimal("6"),
            capable_candidates=(),
        )
        self.assertEqual(d.decision, "TERMINATE")


if __name__ == "__main__":
    unittest.main()
