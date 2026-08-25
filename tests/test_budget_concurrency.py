from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import unittest

from finops.budget_guard import BudgetGuard, BudgetGuardError
from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService


class BudgetConcurrencyTests(unittest.TestCase):
    def test_only_one_of_two_sevens_against_ten(self):
        finops = FinOpsService(
            prices={("openai", "m"): PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)},
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("10")),),
            required=True,
        )

        def attempt(i):
            try:
                return guard.reserve(
                    task_id=f"t{i}",
                    provider="openai",
                    model="m",
                    estimated_cost=Decimal("7"),
                )
            except BudgetGuardError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (1, 2)))
        wins = [r for r in results if r is not None]
        self.assertEqual(len(wins), 1)
        self.assertLessEqual(guard.ledger.get_reserved("global"), Decimal("10"))


if __name__ == "__main__":
    unittest.main()
