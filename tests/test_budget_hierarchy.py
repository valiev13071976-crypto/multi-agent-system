from decimal import Decimal
import unittest

from finops.budget_guard import BudgetGuard
from finops.budget_models import DECISION_SKIP_MODEL, BudgetPolicy, SCOPE_GLOBAL, SCOPE_PROVIDER
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService


class BudgetHierarchyTests(unittest.TestCase):
    def test_most_restrictive_terminates(self):
        finops = FinOpsService(
            prices={("openai", "m"): PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)},
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(
                BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("100")),
                BudgetPolicy("p", SCOPE_PROVIDER, scope_key="openai", hard_limit=Decimal("2")),
            ),
            required=True,
        )
        d = guard.evaluate(
            task_id="t", provider="openai", model="m", estimated_cost=Decimal("3")
        )
        # Candidate exceeds provider-scope remaining → SKIP_MODEL (not global TERMINATE).
        self.assertEqual(d.decision, DECISION_SKIP_MODEL)
        self.assertTrue(any("provider" in r for r in d.scope_reasons))


if __name__ == "__main__":
    unittest.main()
