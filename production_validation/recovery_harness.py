"""Crash/restart recovery harness."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass
from task_queue.lanes import LANE_BACKGROUND
from task_queue.models import STATUS_RETRY_WAIT, utc_now
from task_queue.queue import TaskQueue
from task_queue.store import InMemoryTaskQueueStore
from task_queue.worker import TaskWorker, WorkerConfig


class RecoveryHarness:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def run_worker_crash_simulation(self) -> dict:
        q = TaskQueue(store=InMemoryTaskQueueStore())
        worker = TaskWorker(q, config=WorkerConfig())
        task = q.enqueue(workflow_id="w1", task_id="t1", execution_key="ek-crash-1", tenant_id="tenant-a", execution_lane=LANE_BACKGROUND)
        leased = q.dequeue(worker_id=worker.worker_id)
        started = q.start(leased.queue_task_id, leased.lease_id, worker_id=worker.worker_id)
        expired = replace(
            q.get(task.queue_task_id),
            lease_expires_at=utc_now() - timedelta(seconds=1),
        )
        q.store.save(expired)
        reclaimed_ids = q.recover_stuck_running(force=True)
        recovered = q.get(task.queue_task_id)
        ok = bool(reclaimed_ids) and recovered.status == STATUS_RETRY_WAIT
        status = GateStatus.PASS if ok else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.10_crash_recovery", environment="local", mode=ExecutionMode.LOCAL_FIXTURE, release_identity=self.config.release_identity)
        evidence.complete(
            status=status,
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics={"reclaimed": list(reclaimed_ids), "final_status": recovered.status},
        )
        self.store.save(evidence)
        return {"status": status.value, "evidence_id": evidence.evidence_id}
