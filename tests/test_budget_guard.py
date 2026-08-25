"""P11 BudgetGuard decision tests."""

from decimal import Decimal
import unittest

from finops.budget_guard import BudgetGuard
from finops.budget_models import (
    DECISION_CONTINUE,
    DECISION_DEGRADE,
    DECISION_TERMINATE,
    BudgetPolicy,
    SCOPE_GLOBAL,
)
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService


QUOTE = PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)


def _guard(policies, **kwargs):
    finops = FinOpsService(
        prices={("openai", "m"): QUOTE, ("anthropic", "cheap"): PriceQuote("anthropic", "cheap", Decimal("0.1"), Decimal("0.1"), "USD", True)},
        limits=BudgetLimits(None, None, None, "allow"),
    )
    return BudgetGuard(finops=finops, policies=policies, required=True, **kwargs)


class BudgetGuardTests(unittest.TestCase):
    def test_continue_below_soft(self):
        guard = _guard(
            (
                BudgetPolicy(
                    "g",
                    SCOPE_GLOBAL,
                    hard_limit=Decimal("100"),
                    soft_limit=Decimal("10"),
                    degrade_threshold=Decimal("10"),
                ),
            )
        )
        d = guard.evaluate(
            task_id="t", provider="openai", model="m", estimated_cost=Decimal("5")
        )
        self.assertEqual(d.decision, DECISION_CONTINUE)

    def test_degrade_near_soft(self):
        guard = _guard(
            (
                BudgetPolicy(
                    "g",
                    SCOPE_GLOBAL,
                    hard_limit=Decimal("20"),
                    soft_limit=Decimal("15"),
                    degrade_threshold=Decimal("15"),
                ),
            )
        )
        guard.store.add_spent("global:", Decimal("10"))
        d = guard.evaluate(
            task_id="t",
            provider="openai",
            model="m",
            estimated_cost=Decimal("3"),
            capable_candidates=(("anthropic", "cheap"),),
        )
        self.assertEqual(d.decision, DECISION_DEGRADE)
        self.assertEqual(d.recommended_provider, "anthropic")

    def test_terminate_over_hard(self):
        guard = _guard((BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("5")),))
        d = guard.evaluate(
            task_id="t", provider="openai", model="m", estimated_cost=Decimal("6")
        )
        self.assertEqual(d.decision, DECISION_TERMINATE)


if __name__ == "__main__":
    unittest.main()
