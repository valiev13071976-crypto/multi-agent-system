import unittest

from evals.handlers import get_handler
from evals.models import EvalCase


class EvalRoutingTests(unittest.TestCase):
    def test_routing_offline(self):
        case = EvalCase(
            case_id="routing_offline_basic",
            suite_id="core",
            case_version="1",
            category="routing",
            description="r",
            handler="routing_offline_basic",
        )
        result = get_handler("routing_offline_basic")(case)
        self.assertTrue(result["passed"], result)
        self.assertIn("routing_policy_version", result["artifact_versions"])


if __name__ == "__main__":
    unittest.main()
