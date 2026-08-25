from datetime import datetime, timedelta, timezone
import unittest

from security.encryption import (
    ENCRYPTION_REQUIRED,
    EncryptionUnavailableError,
    SENSITIVITY_SECRET,
)
from task_queue.encrypted_store import EncryptedTaskQueueStore
from task_queue.queue import TaskQueue
from task_queue.retry import RetryPolicy
from task_queue.store import InMemoryTaskQueueStore


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class DeadLetterTests(unittest.TestCase):

    def _fail_once(self, error_code, max_attempts=1, metadata=None):
        clock = Clock()
        queue = TaskQueue(
            InMemoryTaskQueueStore(),
            retry_policy=RetryPolicy(max_attempts=max_attempts),
            now_fn=clock,
        )
        queue.enqueue(
            workflow_id="wf",
            task_id="task",
            execution_key="ek",
            max_attempts=max_attempts,
        )
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        result = queue.fail(
            leased.queue_task_id,
            leased.lease_id,
            error_code=error_code,
            metadata=metadata,
        )
        return queue, result

    def test_j_max_attempts_exhausted_dead_letters(self):
        queue, result = self._fail_once("execution_timeout", max_attempts=1)
        self.assertEqual(result.status, "dead_lettered")
        self.assertEqual(len(queue.get_dead_letters()), 1)

    def test_k_non_retryable_dead_letters_immediately(self):
        queue, result = self._fail_once("malformed_request", max_attempts=5)
        self.assertEqual(result.status, "dead_lettered")
        self.assertEqual(result.attempt, 1)

    def test_l_finops_budget_denied_is_not_retried(self):
        _, result = self._fail_once("finops_budget_denied", max_attempts=5)
        self.assertEqual(result.status, "dead_lettered")

    def test_m_invalid_mode_is_not_retried(self):
        _, result = self._fail_once("invalid_mode", max_attempts=5)
        self.assertEqual(result.status, "dead_lettered")

    def test_r_dead_letter_metadata_has_no_secrets(self):
        _, result = self._fail_once(
            "execution_timeout",
            metadata={
                "prompt": "the full prompt",
                "raw_body": '{"choices":[]}',
                "Authorization": "Bearer secret-token",
                "api_key": "sk-live",
                "PANDA_ENCRYPTION_KEY": "key-material",
                "reason": "timeout",
            },
        )
        blob = str(dict(result.metadata)) + str(result.error_code)
        self.assertNotIn("the full prompt", blob)
        self.assertNotIn("choices", blob)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("sk-live", blob)
        self.assertNotIn("key-material", blob)
        self.assertEqual(result.workflow_id, "wf")
        self.assertEqual(result.task_id, "task")
        self.assertEqual(result.attempt, 1)
        self.assertEqual(result.error_code, "execution_timeout")
        self.assertIsNotNone(result.failed_at)

    def test_aa_encrypted_sensitive_payload_fails_closed(self):
        store = EncryptedTaskQueueStore(encryption=None)
        with self.assertRaises(EncryptionUnavailableError):
            store.put_sensitive_payload("qt-1", "plaintext-secret", SENSITIVITY_SECRET)
        self.assertIn(SENSITIVITY_SECRET, ENCRYPTION_REQUIRED)


if __name__ == "__main__":
    unittest.main()
