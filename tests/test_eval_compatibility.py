import unittest

from evals.handlers import get_handler
from evals.manifest import assert_version_bumped_if_content_changed
from evals.models import ArtifactVersion, EvalCase
from tests.test_smoke import CONTRACT_KEYS


class EvalCompatibilityTests(unittest.TestCase):
    def test_analyze_contract_keys(self):
        case = EvalCase(
            case_id="compat_analyze_public_keys",
            suite_id="core",
            case_version="1",
            category="compatibility",
            description="c",
            handler="compat_analyze_public_keys",
            critical=True,
        )
        result = get_handler("compat_analyze_public_keys")(case)
        self.assertTrue(result["passed"], result)
        self.assertEqual(set(result["actual"]["keys"]), set(CONTRACT_KEYS))
        self.assertEqual(len(CONTRACT_KEYS), 7)

    def test_version_change_fail_and_bump_pass(self):
        a = ArtifactVersion("tool_schema", "search", "1.0.0", "hashA")
        b = ArtifactVersion("tool_schema", "search", "1.0.0", "hashB")
        with self.assertRaises(AssertionError) as ctx:
            assert_version_bumped_if_content_changed(a, b)
        self.assertIn("artifact_changed_without_version_bump", str(ctx.exception))
        c = ArtifactVersion("tool_schema", "search", "1.0.1", "hashB")
        assert_version_bumped_if_content_changed(a, c)


if __name__ == "__main__":
    unittest.main()
