import json
import tempfile
import unittest
from pathlib import Path

from evals.baseline import baseline_from_run, save_baseline
from evals.manifest import build_artifact_manifest, manifest_to_json
from evals.report import run_to_json_dict
from evals.runner import EvalRunner
from evals.suites import build_core_suite


SECRET_NEEDLES = (
    "GITHUB_WRITE_TOKEN",
    "PANDA_ENCRYPTION_KEY",
    "Authorization",
    "Bearer ",
    "sk-live",
    "ghp_",
)


class EvalSecurityTests(unittest.TestCase):
    def test_manifest_report_baseline_no_secrets(self):
        manifest_blob = manifest_to_json(build_artifact_manifest())
        suite = build_core_suite()
        # Run a tiny security-only subset via full suite is heavy; use secret handler path
        from evals.models import EvalCase, EvalSuite

        tiny = EvalSuite(
            suite_id="sec",
            suite_version="1.0.0",
            description="sec",
            cases=(
                EvalCase(
                    case_id="safety_secret_metadata",
                    suite_id="sec",
                    case_version="1",
                    category="security",
                    description="s",
                    handler="safety_secret_metadata",
                    critical=True,
                ),
            ),
        )
        run, gate = EvalRunner().run_suite(tiny)
        report = json.dumps(run_to_json_dict(run, gate), sort_keys=True)
        baseline = baseline_from_run(run, baseline_id="sec-b")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            save_baseline(baseline, path)
            baseline_blob = path.read_text(encoding="utf-8")
        for blob in (manifest_blob, report, baseline_blob, str(run.case_results)):
            for needle in SECRET_NEEDLES:
                self.assertNotIn(needle, blob)


if __name__ == "__main__":
    unittest.main()
