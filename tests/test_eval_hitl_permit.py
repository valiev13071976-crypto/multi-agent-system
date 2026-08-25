import unittest

from evals.handlers import get_handler
from evals.models import EvalCase


class EvalHitlPermitTests(unittest.TestCase):
    def test_critical_hitl_permit(self):
        for name in (
            "safety_consumed_permit",
            "safety_rejected_approval",
            "safety_expired_permit",
        ):
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="hitl",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
