"""Eval coverage for P16 procurement MVP core suite handlers."""

from __future__ import annotations

import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.versions import CORE_SUITE_VERSION


PROCUREMENT_HANDLERS = (
    "procurement_incomplete_needs_clarification",
    "procurement_mandatory_spec_beats_price",
    "procurement_restricted_supplier_excluded",
    "procurement_expired_quote_not_selected",
    "procurement_unknown_fees_not_zero",
    "procurement_currency_mismatch_safe",
    "procurement_price_provenance_required",
    "procurement_single_source_flagged",
    "procurement_prompt_injection_no_override",
    "procurement_cross_scope_denied",
    "procurement_no_purchase_execution",
    "procurement_hitl_before_action",
    "procurement_citations_preserved",
    "procurement_deterministic_comparison",
    "procurement_no_public_api",
)


class EvalProcurementTests(unittest.TestCase):
    def test_core_suite_version(self):
        self.assertEqual(CORE_SUITE_VERSION, "1.7.0")

    def test_procurement_core_handlers_pass(self):
        for name in PROCUREMENT_HANDLERS:
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="procurement_mvp",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], msg=f"{name}:{result.get('reason_codes')}")


if __name__ == "__main__":
    unittest.main()
