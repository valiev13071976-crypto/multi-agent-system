"""FH.9 / FH.10 — admission deadline + bounded backpressure rejection."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from task_queue.models import PRIORITY_NORMAL
from task_queue.store import InMemoryTaskQueueStore
from workflow.admission import (
    DECISION_REJECT,
    AdmissionController,
    AdmissionLimits,
    AdmissionRejectedError,
)


T0 = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


class _SaturatedStore:
    def count_by_status(self, tenant_id="", now=None):
        return {
            "pending_global": 0,
            "pending_tenant": 5,
            "pending_by_lane": {},
            "running_global": 0,
            "running_tenant": 0,
            "running_by_lane": {},
        }


class _SaturatedQueue:
    store = _SaturatedStore()


class FHAdmissionBackpressureTests(unittest.TestCase):
    def test_deadline_expired_rejected(self):
        ctl = AdmissionController(AdmissionLimits(max_pending_global=1000))
        queue = InMemoryTaskQueueStore()
        decision = ctl.evaluate_enqueue(
            queue,
            tenant_id="tenant-a",
            priority=PRIORITY_NORMAL,
            deadline_at=T0,
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(decision.decision, DECISION_REJECT)
        self.assertEqual(decision.reason_code, "deadline_expired")

    def test_capacity_accepted(self):
        ctl = AdmissionController(AdmissionLimits(max_pending_global=10))
        queue = InMemoryTaskQueueStore()
        decision = ctl.evaluate_enqueue(
            queue, tenant_id="tenant-a", priority=PRIORITY_NORMAL, now=T0
        )
        self.assertEqual(decision.decision, "ACCEPT")

    def test_saturation_rejected_bounded(self):
        ctl = AdmissionController(AdmissionLimits(max_pending_per_tenant=1))
        decision = ctl.evaluate_enqueue(
            _SaturatedQueue(), tenant_id="tenant-a", priority=PRIORITY_NORMAL, now=T0
        )
        self.assertEqual(decision.decision, DECISION_REJECT)
        self.assertEqual(decision.reason_code, "tenant_pending_limit")

    def test_require_enqueue_raises(self):
        ctl = AdmissionController(AdmissionLimits(max_pending_per_tenant=1))
        with self.assertRaises(AdmissionRejectedError) as ctx:
            ctl.require_enqueue(_SaturatedQueue(), tenant_id="tenant-a", now=T0)
        self.assertEqual(ctx.exception.reason, "tenant_pending_limit")


if __name__ == "__main__":
    unittest.main()
