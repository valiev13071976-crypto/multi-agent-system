import unittest

from evals.handlers import get_handler
from evals.models import EvalCase


def _case(handler):
    return EvalCase(
        case_id=handler,
        suite_id="core",
        case_version="1",
        category="tool_gateway_write",
        description=handler,
        handler=handler,
        critical=True,
    )


class EvalToolGatewayTests(unittest.TestCase):
    def test_disabled_bypass_dry_run(self):
        for name in (
            "safety_disabled_tool",
            "safety_write_bypass_impossible",
            "safety_dry_run_zero_mutation",
            "safety_github_write_disabled_default",
            "safety_dynamic_execution_denied",
        ):
            with self.subTest(name=name):
                result = get_handler(name)(_case(name))
                self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
