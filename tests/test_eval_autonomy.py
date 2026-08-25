import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.versions import POLICY_VERSION


class EvalAutonomyTests(unittest.TestCase):
    def test_autonomy_matrix_and_policy_version(self):
        case = EvalCase(
            case_id="autonomy_allow_require_deny",
            suite_id="core",
            case_version="1",
            category="autonomy",
            description="a",
            handler="autonomy_allow_require_deny",
        )
        result = get_handler("autonomy_allow_require_deny")(case)
        self.assertTrue(result["passed"], result)
        self.assertEqual(
            result["artifact_versions"].get("policy_version"), POLICY_VERSION
        )

    def test_critical_autonomy_safety(self):
        for name in (
            "safety_missing_capability",
            "safety_irreversible_default_denied",
            "safety_missing_idempotency",
            "safety_uncertain_not_replayed",
        ):
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="autonomy",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
