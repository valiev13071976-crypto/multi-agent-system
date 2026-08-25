import unittest

from evals.models import EvalCase
from evals.runner import EvalRunner
from evals.suites import build_core_suite


class EvalRunnerTests(unittest.TestCase):
    def test_deterministic_order_and_totals(self):
        suite = build_core_suite()
        # Use tiny synthetic suite for ordering/isolation
        cases = (
            EvalCase(
                case_id="z_last",
                suite_id="t",
                case_version="1",
                category="security",
                description="z",
                handler="safety_secret_metadata",
            ),
            EvalCase(
                case_id="a_first",
                suite_id="t",
                case_version="1",
                category="security",
                description="a",
                handler="safety_secret_metadata",
            ),
            EvalCase(
                case_id="net",
                suite_id="t",
                case_version="1",
                category="observability",
                description="n",
                handler="safety_secret_metadata",
                requires_network=True,
            ),
        )
        from evals.models import EvalSuite

        tiny = EvalSuite(
            suite_id="t",
            suite_version="1.0.0",
            description="tiny",
            cases=cases,
            required_pass_rate=1.0,
        )
        run, gate = EvalRunner(allow_network=False).run_suite(tiny)
        ids = [r.case_id for r in run.case_results]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(run.skipped, 1)
        net = next(r for r in run.case_results if r.case_id == "net")
        self.assertEqual(net.status, "skipped")
        self.assertIn("network_eval_disabled", net.reason_codes)
        self.assertEqual(gate.decision, "PASS")

    def test_one_failure_does_not_stop_others(self):
        from evals import handlers

        def boom(case):
            raise RuntimeError("boom")

        handlers.HANDLER_REGISTRY["eval_boom"] = boom
        try:
            cases = (
                EvalCase(
                    case_id="bad",
                    suite_id="t",
                    case_version="1",
                    category="security",
                    description="b",
                    handler="eval_boom",
                    critical=False,
                ),
                EvalCase(
                    case_id="good",
                    suite_id="t",
                    case_version="1",
                    category="security",
                    description="g",
                    handler="safety_secret_metadata",
                ),
            )
            from evals.models import EvalSuite

            suite = EvalSuite(
                suite_id="t",
                suite_version="1",
                description="t",
                cases=cases,
                required_pass_rate=0.0,
            )
            run, gate = EvalRunner().run_suite(suite)
            statuses = {r.case_id: r.status for r in run.case_results}
            self.assertEqual(statuses["bad"], "error")
            self.assertEqual(statuses["good"], "passed")
            # Gate may FAIL due to deterministic error; isolation is the assertion.
            self.assertIn(gate.decision, {"PASS", "FAIL"})
        finally:
            handlers.HANDLER_REGISTRY.pop("eval_boom", None)

    def test_timeout(self):
        from evals import handlers
        import time

        def slow(case):
            time.sleep(2)
            return {"passed": True, "reason_codes": (), "actual": {}}

        handlers.HANDLER_REGISTRY["eval_slow"] = slow
        try:
            case = EvalCase(
                case_id="slow",
                suite_id="t",
                case_version="1",
                category="security",
                description="s",
                handler="eval_slow",
                constraints={"timeout_seconds": 0.2},
            )
            result = EvalRunner().run_case(case, run_id="r")
            self.assertEqual(result.status, "error")
            self.assertIn("timeout", result.reason_codes)
        finally:
            handlers.HANDLER_REGISTRY.pop("eval_slow", None)


if __name__ == "__main__":
    unittest.main()
