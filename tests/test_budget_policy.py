from decimal import Decimal
import unittest

from finops.budget_models import BudgetPolicy, SCOPE_TASK, BUDGET_POLICY_VERSION
from finops.budget_policy import load_advanced_budget_policies, policies_from_limits
from finops.models import BudgetLimits


class BudgetPolicyTests(unittest.TestCase):
    def test_compat_limits_mapped(self):
        policies = policies_from_limits(
            BudgetLimits(Decimal("1"), Decimal("2"), Decimal("3"), "allow")
        )
        scopes = {p.scope for p in policies}
        self.assertEqual(scopes, {"task", "daily", "monthly"})

    def test_optional_env_limits(self):
        policies = load_advanced_budget_policies(
            env={
                "FINOPS_GLOBAL_LIMIT": "50",
                "FINOPS_PER_PROVIDER_LIMIT": "10",
            }
        )
        scopes = {p.scope for p in policies}
        self.assertIn("global", scopes)
        self.assertIn("provider", scopes)
        self.assertEqual(BUDGET_POLICY_VERSION, "1.0.0")

    def test_unconfigured_empty(self):
        self.assertEqual(load_advanced_budget_policies(env={}), ())


if __name__ == "__main__":
    unittest.main()
