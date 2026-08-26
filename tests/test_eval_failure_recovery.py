"""Eval coverage for failure recovery critical cases."""

from __future__ import annotations

import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.suites import build_core_suite


class EvalFailureRecoveryTests(unittest.TestCase):
    def test_critical_recovery_handlers_pass(self):
        suite = build_core_suite()
        recovery = [c for c in suite.cases if "recovery_" in c.case_id]
        self.assertGreaterEqual(len(recovery), 8)
        for case in recovery:
            self.assertTrue(case.critical)
            handler = get_handler(case.handler)
            result = handler(case)
            self.assertTrue(result["passed"], msg=f"{case.case_id}:{result['reason_codes']}")

    def test_suite_version_bumped(self):
        from evals.versions import CORE_SUITE_VERSION

        self.assertEqual(CORE_SUITE_VERSION, "1.5.0")


if __name__ == "__main__":
    unittest.main()
