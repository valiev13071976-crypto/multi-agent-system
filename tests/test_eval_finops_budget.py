import unittest

from evals.handlers import get_handler
from evals.models import EvalCase


class EvalFinopsBudgetTests(unittest.TestCase):
    def test_critical_finops_handlers(self):
        for name in (
            "finops_hard_limit_terminates",
            "finops_missing_reservation_blocks",
            "finops_concurrent_no_overspend",
            "finops_release_restores_capacity",
            "finops_degrade_capability_safe",
            "finops_unknown_cost_not_zero",
        ):
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="finops_budget_guardrails",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
