import tempfile
import unittest
from pathlib import Path

from evals.baseline import (
    baseline_from_run,
    compare_to_baseline,
    load_baseline,
    save_baseline,
)
from evals.models import EvalCaseResult, EvalRun, utc_now


def _run(results, *, status="passed"):
    now = utc_now()
    passed = sum(1 for r in results if r.passed and r.status == "passed")
    failed = sum(1 for r in results if r.status in {"failed", "error"})
    skipped = sum(1 for r in results if r.status == "skipped")
    return EvalRun(
        run_id="run-1",
        suite_id="core",
        suite_version="1.0.0",
        started_at=now,
        completed_at=now,
        total=len(results),
        passed=passed,
        failed=failed,
        skipped=skipped,
        pass_rate=1.0 if not failed else 0.0,
        critical_failures=tuple(
            r.case_id for r in results if r.critical and not r.passed
        ),
        status=status,
        case_results=tuple(results),
    )


class EvalBaselineTests(unittest.TestCase):
    def test_save_load_and_compare(self):
        base_results = (
            EvalCaseResult("r", "a", "passed", True, 1.0, critical=True),
            EvalCaseResult("r", "b", "failed", False, 0.0, critical=False),
        )
        baseline = baseline_from_run(_run(base_results), baseline_id="b1")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            save_baseline(baseline, path)
            loaded = load_baseline(path)
        self.assertEqual(loaded.baseline_id, "b1")
        self.assertIn("a", loaded.critical_case_ids)

        current = _run(
            (
                EvalCaseResult("r", "a", "passed", True, 1.0, critical=True),
                EvalCaseResult("r", "b", "passed", True, 1.0, critical=False),
                EvalCaseResult("r", "c", "passed", True, 1.0, critical=False),
            )
        )
        cmp = compare_to_baseline(current, loaded)
        self.assertEqual(cmp["classifications"]["a"], "unchanged_pass")
        self.assertEqual(cmp["classifications"]["b"], "improvement")
        self.assertEqual(cmp["classifications"]["c"], "new_case")

    def test_regression_and_critical_removal(self):
        baseline = baseline_from_run(
            _run(
                (
                    EvalCaseResult("r", "crit", "passed", True, 1.0, critical=True),
                    EvalCaseResult("r", "x", "passed", True, 1.0, critical=False),
                )
            ),
            baseline_id="b2",
        )
        current = _run(
            (
                EvalCaseResult("r", "x", "failed", False, 0.0, critical=False),
            )
        )
        cmp = compare_to_baseline(current, baseline)
        self.assertIn("x", cmp["regressions"])
        self.assertIn("crit", cmp["critical_removed"])
        self.assertIn("crit", cmp["removed_cases"])


if __name__ == "__main__":
    unittest.main()
