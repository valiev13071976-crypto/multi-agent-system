import os
import unittest
from unittest.mock import patch

from task_queue.retry import (
    BACKOFF_EXPONENTIAL,
    BACKOFF_FIXED,
    NON_RETRYABLE_CODES,
    RETRYABLE_CODES,
    RetryPolicy,
    is_retryable,
)


class RetryPolicyTests(unittest.TestCase):

    def test_default_is_no_automatic_retry(self):
        policy = RetryPolicy()
        self.assertEqual(policy.max_attempts, 1)
        self.assertEqual(policy.backoff_mode, BACKOFF_FIXED)
        self.assertFalse(policy.can_retry(1, "execution_timeout"))

    def test_fixed_delay(self):
        policy = RetryPolicy(base_delay_seconds=5, backoff_mode=BACKOFF_FIXED)
        self.assertEqual(policy.delay_seconds(1), 5)
        self.assertEqual(policy.delay_seconds(4), 5)

    def test_exponential_delay_is_deterministic(self):
        policy = RetryPolicy(
            base_delay_seconds=5,
            max_delay_seconds=60,
            backoff_mode=BACKOFF_EXPONENTIAL,
        )
        self.assertEqual(policy.delay_seconds(1), 5)
        self.assertEqual(policy.delay_seconds(2), 10)
        self.assertEqual(policy.delay_seconds(3), 20)
        self.assertEqual(policy.delay_seconds(8), 60)

    def test_retryable_and_non_retryable_sets(self):
        self.assertTrue(is_retryable("execution_timeout"))
        self.assertTrue(is_retryable("temporary_provider_error"))
        self.assertTrue(is_retryable("temporary_tool_unavailable"))
        self.assertTrue(is_retryable("transient_network_error"))
        self.assertFalse(is_retryable("finops_budget_denied"))
        self.assertFalse(is_retryable("invalid_mode"))
        self.assertFalse(is_retryable("invalid_role"))
        self.assertFalse(is_retryable("no_capable_provider"))
        self.assertFalse(is_retryable("security_error"))
        self.assertFalse(is_retryable("permission_denied"))
        self.assertFalse(is_retryable("malformed_request"))
        self.assertFalse(is_retryable("workflow_transition_error"))
        self.assertTrue("timeout" in RETRYABLE_CODES)
        self.assertTrue("finops_budget_denied" in NON_RETRYABLE_CODES)

    def test_unknown_code_is_not_retryable(self):
        self.assertFalse(is_retryable("something_new"))
        self.assertFalse(is_retryable(None))

    def test_from_env_defaults(self):
        env = {
            "TASK_QUEUE_MAX_ATTEMPTS": "1",
            "TASK_QUEUE_BASE_RETRY_DELAY_SECONDS": "5",
            "TASK_QUEUE_MAX_RETRY_DELAY_SECONDS": "60",
            "TASK_QUEUE_BACKOFF_MODE": "fixed",
        }
        with patch.dict(os.environ, env, clear=False):
            policy = RetryPolicy.from_env()
        self.assertEqual(policy.max_attempts, 1)
        self.assertEqual(policy.base_delay_seconds, 5)
        self.assertEqual(policy.max_delay_seconds, 60)
        self.assertEqual(policy.backoff_mode, BACKOFF_FIXED)


if __name__ == "__main__":
    unittest.main()
