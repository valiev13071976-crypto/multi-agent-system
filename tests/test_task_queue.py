from datetime import datetime, timedelta, timezone
import unittest

from task_queue.errors import (
    QueueDuplicateExecutionError,
    QueueLeaseError,
    QueueTaskNotFoundError,
    QueueTransitionError,
)
from task_queue.models import (
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_LEASED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
)
from task_queue.queue import TaskQueue, sanitize_metadata
from task_queue.retry import RetryPolicy
from task_queue.store import InMemoryTaskQueueStore


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def queue_at(clock, **kwargs):
    policy = kwargs.pop("retry_policy", RetryPolicy(max_attempts=1))
    return TaskQueue(
        InMemoryTaskQueueStore(),
        retry_policy=policy,
        now_fn=clock,
        lease_seconds=kwargs.pop("lease_seconds", 300),
    )


def enqueue(queue, **overrides):
    fields = {
        "workflow_id": "wf-1",
        "task_id": "task-1",
        "execution_key": "ek-1",
    }
    fields.update(overrides)
    return queue.enqueue(**fields)


class TaskQueueTests(unittest.TestCase):

    def test_a_enqueue_is_queued(self):
        clock = Clock()
        queue = queue_at(clock)
        task = enqueue(queue)
        self.assertEqual(task.status, STATUS_QUEUED)
        self.assertEqual(task.attempt, 0)
        self.assertEqual(task.priority, PRIORITY_NORMAL)
        self.assertEqual(task.task_id, "task-1")

    def test_b_priority_ordering(self):
        clock = Clock()
        queue = queue_at(clock)
        enqueue(queue, execution_key="low", priority=PRIORITY_LOW, queue_task_id="d")
        enqueue(queue, execution_key="normal", priority=PRIORITY_NORMAL, queue_task_id="c")
        enqueue(queue, execution_key="high", priority=PRIORITY_HIGH, queue_task_id="b")
        enqueue(queue, execution_key="critical", priority=PRIORITY_CRITICAL, queue_task_id="a")
        ready = queue.list_ready()
        self.assertEqual(
            [item.priority for item in ready],
            [PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW],
        )

    def test_c_same_priority_uses_available_then_created_then_id(self):
        clock = Clock()
        queue = queue_at(clock)
        first = enqueue(queue, execution_key="a", queue_task_id="zz")
        clock.now = T0 + timedelta(seconds=1)
        second = enqueue(queue, execution_key="b", queue_task_id="aa")
        ready = queue.list_ready()
        self.assertEqual(ready[0].queue_task_id, first.queue_task_id)
        self.assertEqual(ready[1].queue_task_id, second.queue_task_id)

    def test_d_dequeue_leases(self):
        clock = Clock()
        queue = queue_at(clock)
        enqueue(queue)
        leased = queue.dequeue()
        self.assertEqual(leased.status, STATUS_LEASED)
        self.assertTrue(leased.lease_id)
        self.assertEqual(leased.leased_at, T0)

    def test_e_wrong_and_stale_lease(self):
        clock = Clock()
        queue = queue_at(clock, lease_seconds=30)
        enqueue(queue)
        leased = queue.dequeue()
        with self.assertRaises(QueueLeaseError):
            queue.start(leased.queue_task_id, "not-the-lease")
        clock.now = T0 + timedelta(seconds=31)
        with self.assertRaises(QueueLeaseError):
            queue.start(leased.queue_task_id, leased.lease_id)

    def test_f_leased_running_completed(self):
        clock = Clock()
        queue = queue_at(clock)
        enqueue(queue)
        leased = queue.dequeue()
        running = queue.start(leased.queue_task_id, leased.lease_id)
        self.assertEqual(running.status, STATUS_RUNNING)
        self.assertEqual(running.attempt, 1)
        done = queue.ack(leased.queue_task_id, leased.lease_id)
        self.assertEqual(done.status, STATUS_COMPLETED)

    def test_g_retryable_failure_goes_to_retry_wait(self):
        clock = Clock()
        queue = queue_at(clock, retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=5))
        enqueue(queue, max_attempts=3)
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        waiting = queue.fail(
            leased.queue_task_id,
            leased.lease_id,
            error_code="execution_timeout",
        )
        self.assertEqual(waiting.status, STATUS_RETRY_WAIT)
        self.assertEqual(waiting.available_at, T0 + timedelta(seconds=5))

    def test_h_retry_wait_before_available_at_not_dequeued(self):
        clock = Clock()
        queue = queue_at(clock, retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=5))
        enqueue(queue, max_attempts=3)
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        queue.fail(leased.queue_task_id, leased.lease_id, error_code="timeout")
        self.assertIsNone(queue.dequeue())

    def test_i_retry_after_available_at_is_dequeued(self):
        clock = Clock()
        queue = queue_at(clock, retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=5))
        enqueue(queue, max_attempts=3)
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        waiting = queue.fail(
            leased.queue_task_id,
            leased.lease_id,
            error_code="transient_network_error",
        )
        clock.now = waiting.available_at
        again = queue.dequeue()
        self.assertIsNotNone(again)
        self.assertEqual(again.status, STATUS_LEASED)
        self.assertEqual(again.queue_task_id, waiting.queue_task_id)

    def test_o_default_max_attempts_is_one(self):
        clock = Clock()
        queue = queue_at(clock)
        self.assertEqual(queue.retry_policy.max_attempts, 1)
        enqueue(queue)
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        lettered = queue.fail(
            leased.queue_task_id,
            leased.lease_id,
            error_code="execution_timeout",
        )
        self.assertEqual(lettered.status, "dead_lettered")

    def test_p_active_execution_key_returns_existing(self):
        clock = Clock()
        queue = queue_at(clock)
        first = enqueue(queue, execution_key="same")
        second = enqueue(queue, execution_key="same", task_id="other")
        self.assertEqual(first.queue_task_id, second.queue_task_id)
        self.assertEqual(len(queue.store.list_all()), 1)

    def test_q_terminal_execution_key_does_not_rerun(self):
        clock = Clock()
        queue = queue_at(clock)
        enqueue(queue, execution_key="done")
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        queue.ack(leased.queue_task_id, leased.lease_id)
        with self.assertRaises(QueueDuplicateExecutionError):
            enqueue(queue, execution_key="done")

    def test_s_cancel_queued(self):
        clock = Clock()
        queue = queue_at(clock)
        task = enqueue(queue)
        cancelled = queue.cancel(task.queue_task_id)
        self.assertEqual(cancelled.status, STATUS_CANCELLED)

    def test_t_cancel_terminal_is_noop(self):
        clock = Clock()
        queue = queue_at(clock)
        enqueue(queue)
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        done = queue.ack(leased.queue_task_id, leased.lease_id)
        again = queue.cancel(done.queue_task_id)
        self.assertEqual(again.status, STATUS_COMPLETED)
        self.assertEqual(again.queue_task_id, done.queue_task_id)

    def test_y_canonical_task_id_is_preserved(self):
        clock = Clock()
        queue = queue_at(clock)
        task = enqueue(queue, task_id="canonical-task-id")
        self.assertEqual(task.task_id, "canonical-task-id")
        leased = queue.dequeue()
        self.assertEqual(leased.task_id, "canonical-task-id")

    def test_z_errors_are_redacted(self):
        clock = Clock()
        queue = queue_at(clock)
        enqueue(queue)
        leased = queue.dequeue()
        queue.start(leased.queue_task_id, leased.lease_id)
        lettered = queue.fail(
            leased.queue_task_id,
            leased.lease_id,
            error_code="execution_timeout",
            metadata={
                "error_message": "Authorization: Bearer abc.def.ghi api_key=sk-live-secret",
                "prompt": "full user prompt",
            },
        )
        blob = str(dict(lettered.metadata))
        self.assertNotIn("abc.def.ghi", blob)
        self.assertNotIn("sk-live-secret", blob)
        self.assertNotIn("full user prompt", blob)

    def test_invalid_transition(self):
        clock = Clock()
        queue = queue_at(clock)
        task = enqueue(queue)
        with self.assertRaises(QueueTransitionError):
            queue._transition(task, STATUS_COMPLETED)

    def test_missing_task(self):
        queue = queue_at(Clock())
        with self.assertRaises(QueueTaskNotFoundError):
            queue.get("missing")

    def test_sanitize_drops_forbidden_keys(self):
        cleaned = sanitize_metadata(
            {
                "prompt": "secret prompt",
                "attempt": 1,
                "api_key": "sk-x",
            }
        )
        self.assertNotIn("prompt", cleaned)
        self.assertNotIn("api_key", cleaned)
        self.assertEqual(cleaned["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
