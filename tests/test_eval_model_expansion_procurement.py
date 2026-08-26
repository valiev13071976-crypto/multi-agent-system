"""Eval coverage for P18 Moonshot / model expansion procurement category."""

from __future__ import annotations

import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.versions import CORE_SUITE_VERSION


MOONSHOT_HANDLERS = (
    "moonshot_disabled_by_default",
    "moonshot_missing_key_safe",
    "moonshot_explicit_fake_route",
    "moonshot_auto_capability_route",
    "moonshot_long_context_profile",
    "moonshot_provisional_quality",
    "moonshot_unknown_price_not_zero",
    "moonshot_finops_exact_usage",
    "moonshot_budget_can_exclude",
    "moonshot_429_bounded",
    "moonshot_timeout_normalized",
    "moonshot_malformed_safe",
    "moonshot_injection_procurement_immune",
    "moonshot_financial_still_denied",
    "moonshot_no_direct_tool_invoke",
    "moonshot_api_analyze_contract",
)


class EvalModelExpansionProcurementTests(unittest.TestCase):
    def test_core_suite_version(self):
        self.assertEqual(CORE_SUITE_VERSION, "1.8.0")

    def test_moonshot_handlers_pass(self):
        for name in MOONSHOT_HANDLERS:
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="model_expansion_procurement",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], msg=f"{name}:{result.get('reason_codes')}")


if __name__ == "__main__":
    unittest.main()
