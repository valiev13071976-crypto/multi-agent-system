"""Procurement model eval catalog / offline invariants."""

from __future__ import annotations

import unittest

from agents.procurement_model_eval import (
    ProcurementModelBenchmarkRunner,
    procurement_model_eval_catalog,
    procurement_model_eval_snapshot,
)
from procurement.errors import PROCUREMENT_ACTION_DENIED, ProcurementError
from procurement.runtime import build_procurement_runtime


class ProcurementModelEvalTests(unittest.TestCase):
    def test_catalog_covers_required_task_types(self):
        cases = procurement_model_eval_catalog()
        types = {c.task_type for c in cases}
        for needed in (
            "requirement_normalization",
            "offer_comparison",
            "multilingual_supplier",
            "long_document_reasoning",
            "prompt_injection",
            "financial_deny",
        ):
            self.assertIn(needed, types)

    def test_offline_runner_no_live(self):
        runner = ProcurementModelBenchmarkRunner(live_enabled=False)
        snap = runner.run_offline_invariants()
        self.assertFalse(snap["ran_live"])
        with self.assertRaises(RuntimeError):
            runner.run_live()

    def test_financial_deny_authoritative(self):
        rt = build_procurement_runtime(env={"PROCUREMENT_ENABLED": "true"})
        with self.assertRaises(ProcurementError) as ctx:
            rt.service.execute_financial_action("place_order")
        self.assertEqual(ctx.exception.reason, PROCUREMENT_ACTION_DENIED)

    def test_snapshot_no_quality_claim(self):
        snap = procurement_model_eval_snapshot()
        self.assertTrue(snap["no_quality_claim_without_live"])
        self.assertIn("moonshot", snap["providers_comparable"])


if __name__ == "__main__":
    unittest.main()
