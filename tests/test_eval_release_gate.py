import unittest

from evals.models import EvalCaseResult, EvalRun, utc_now
from evals.release_gate import GATE_BLOCKED, GATE_FAIL, GATE_PASS, ReleaseGate


def _run(*, critical_failures=(), pass_rate=1.0, status="passed", cases=()):
    now = utc_now()
    return EvalRun(
        run_id="r",
        suite_id="core",
        suite_version="1.0.0",
        started_at=now,
        completed_at=now,
        total=len(cases) or 1,
        passed=1 if pass_rate == 1.0 else 0,
        failed=0 if pass_rate == 1.0 else 1,
        skipped=0,
        pass_rate=pass_rate,
        critical_failures=tuple(critical_failures),
        status=status,
        case_results=tuple(cases),
    )


class EvalReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = ReleaseGate()

    def test_all_pass(self):
        d = self.gate.evaluate(_run())
        self.assertEqual(d.decision, GATE_PASS)

    def test_critical_fail(self):
        d = self.gate.evaluate(_run(critical_failures=("safety_x",), status="failed"))
        self.assertEqual(d.decision, GATE_FAIL)
        self.assertIn("critical_case_failed", d.reason_codes)

    def test_pass_rate(self):
        d = self.gate.evaluate(_run(pass_rate=0.5, status="failed"), required_pass_rate=1.0)
        self.assertEqual(d.decision, GATE_FAIL)
        self.assertIn("pass_rate_below_threshold", d.reason_codes)

    def test_version_mismatch(self):
        d = self.gate.evaluate(
            _run(),
            version_mismatches=[{"reason": "artifact_changed_without_version_bump"}],
        )
        self.assertEqual(d.decision, GATE_FAIL)
        self.assertIn("artifact_changed_without_version_bump", d.reason_codes)

    def test_compatibility_fail(self):
        cases = (
            EvalCaseResult(
                "r",
                "compat_analyze_public_keys",
                "failed",
                False,
                0.0,
                metadata_safe={"category": "compatibility"},
            ),
        )
        d = self.gate.evaluate(_run(status="failed", cases=cases, pass_rate=0.0))
        self.assertEqual(d.decision, GATE_FAIL)
        self.assertIn("compatibility_eval_failed", d.reason_codes)

    def test_removed_critical(self):
        d = self.gate.evaluate(
            _run(),
            comparison={"critical_removed": ["safety_x"], "regressions": []},
        )
        self.assertEqual(d.decision, GATE_FAIL)
        self.assertIn("critical_eval_case_removed", d.reason_codes)

    def test_blocked(self):
        d = self.gate.evaluate(_run(), blocked_reason="config_infrastructure_issue")
        self.assertEqual(d.decision, GATE_BLOCKED)


if __name__ == "__main__":
    unittest.main()
