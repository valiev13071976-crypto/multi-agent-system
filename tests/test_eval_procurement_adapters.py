"""Eval coverage for P17 procurement production adapters."""

from __future__ import annotations

import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.versions import CORE_SUITE_VERSION


ADAPTER_HANDLERS = (
    "procurement_adapters_internal_first",
    "procurement_adapters_external_disabled_default",
    "procurement_adapters_tool_gateway_path",
    "procurement_adapters_arbitrary_url_denied",
    "procurement_adapters_ssrf_denied",
    "procurement_adapters_catalog_validated_ref",
    "procurement_adapters_provenance_preserved",
    "procurement_adapters_prompt_injection_safe",
    "procurement_adapters_rfq_draft_only",
    "procurement_adapters_no_communication",
    "procurement_adapters_no_purchase",
    "procurement_adapters_no_permit_for_reads",
    "procurement_adapters_timeout_degrades",
    "procurement_adapters_rate_limit_bounded",
    "procurement_adapters_no_public_api",
)


class EvalProcurementAdaptersTests(unittest.TestCase):
    def test_core_suite_version(self):
        self.assertEqual(CORE_SUITE_VERSION, "1.8.0")

    def test_adapter_core_handlers_pass(self):
        for name in ADAPTER_HANDLERS:
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="procurement_production_adapters",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], msg=f"{name}:{result.get('reason_codes')}")


if __name__ == "__main__":
    unittest.main()
