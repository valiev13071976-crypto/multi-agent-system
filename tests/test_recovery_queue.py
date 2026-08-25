"""Recovery queue lease/defer/complete — no mutation."""

from __future__ import annotations

import unittest
from datetime import timedelta

from recovery.models import ACTION_RECONCILE_READ_ONLY, QUEUE_DEAD, QUEUE_PENDING, utc_now
from recovery.queue import RecoveryQueue


class RecoveryQueueTests(unittest.TestCase):
    def test_enqueue_lease_complete(self):
        q = RecoveryQueue(max_attempts=3)
        job = q.enqueue(recovery_id="r1", action_type=ACTION_RECONCILE_READ_ONLY)
        self.assertEqual(job.status, QUEUE_PENDING)
        due = q.get_due_jobs(now=utc_now())
        self.assertEqual(len(due), 1)
        leased = q.lease(job.job_id)
        self.assertEqual(leased.status, "leased")
        done = q.complete(job.job_id)
        self.assertEqual(done.status, "completed")

    def test_defer_and_bounded_attempts(self):
        q = RecoveryQueue(max_attempts=2)
        job = q.enqueue(recovery_id="r1")
        q.lease(job.job_id)
        deferred = q.defer(job.job_id, delay_seconds=10)
        self.assertEqual(deferred.status, "deferred")
        self.assertEqual(deferred.attempt, 1)
        q.lease(job.job_id)
        dead = q.defer(job.job_id, delay_seconds=10)
        self.assertEqual(dead.status, QUEUE_DEAD)

    def test_cancel(self):
        q = RecoveryQueue()
        job = q.enqueue(recovery_id="r1")
        cancelled = q.cancel(job.job_id)
        self.assertEqual(cancelled.status, "cancelled")

    def test_no_mutation_payload(self):
        q = RecoveryQueue()
        job = q.enqueue(
            recovery_id="r1",
            metadata_safe={"case_type": "uncertain_side_effect"},
        )
        self.assertNotIn("body", job.metadata_safe)
        self.assertNotIn("payload", job.metadata_safe)


if __name__ == "__main__":
    unittest.main()
