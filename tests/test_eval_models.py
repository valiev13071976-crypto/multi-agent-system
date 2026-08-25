import unittest

from evals.models import (
    EvalCase,
    EvalCaseResult,
    EvalRun,
    EvalSuite,
    content_hash,
    utc_now,
)


class EvalModelsTests(unittest.TestCase):
    def test_suite_content_hash_stable(self):
        case = EvalCase(
            case_id="c1",
            suite_id="core",
            case_version="1.0.0",
            category="security",
            description="d",
            handler="h",
            critical=True,
        )
        suite = EvalSuite(
            suite_id="core",
            suite_version="1.0.0",
            description="d",
            cases=(case,),
        )
        self.assertEqual(suite.content_hash, suite.content_hash)
        self.assertEqual(len(suite.content_hash), 64)

    def test_case_result_status(self):
        r = EvalCaseResult(
            run_id="r",
            case_id="c",
            status="passed",
            passed=True,
            score=1.0,
        )
        self.assertTrue(r.passed)
        with self.assertRaises(ValueError):
            EvalCaseResult(
                run_id="r",
                case_id="c",
                status="nope",
                passed=False,
                score=0,
            )

    def test_run_status(self):
        now = utc_now()
        run = EvalRun(
            run_id="r",
            suite_id="core",
            suite_version="1.0.0",
            started_at=now,
            completed_at=now,
            total=1,
            passed=1,
            failed=0,
            skipped=0,
            pass_rate=1.0,
            critical_failures=(),
            status="passed",
        )
        self.assertEqual(run.status, "passed")


if __name__ == "__main__":
    unittest.main()
