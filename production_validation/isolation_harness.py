"""Interactive vs batch isolation harness."""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
from pathlib import Path

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass
from side_effects.persistence import build_side_effect_persistence
from task_queue.lanes import LANE_BACKGROUND, LANE_BULK, LANE_INTERACTIVE
from task_queue.models import STATUS_QUEUED
from task_queue.queue import TaskQueue
from task_queue.store import InMemoryTaskQueueStore
from task_queue.worker import TaskWorker, WorkerConfig


class IsolationHarness:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def run(self) -> dict:
        q = TaskQueue(store=InMemoryTaskQueueStore())
        ix_worker = TaskWorker(q, config=WorkerConfig(allowed_lanes=frozenset({LANE_INTERACTIVE}), pool_name="interactive"))
        bg_worker = TaskWorker(q, config=WorkerConfig(allowed_lanes=frozenset({LANE_BACKGROUND, LANE_BULK}), pool_name="batch"))
        for i in range(50):
            q.enqueue(workflow_id="bg", task_id=f"b{i}", execution_key=f"ek-bg-{i}", tenant_id="tenant-a", execution_lane=LANE_BULK)
        ix_latencies: list[float] = []
        for i in range(10):
            q.enqueue(workflow_id="ix", task_id=f"i{i}", execution_key=f"ek-ix-{i}", tenant_id="tenant-a", execution_lane=LANE_INTERACTIVE)
        started = time.monotonic()
        for _ in range(10):
            t0 = time.monotonic()
            claimed = q.dequeue(worker_id=ix_worker.worker_id)
            if claimed:
                ix_latencies.append((time.monotonic() - t0) * 1000)
        duration = time.monotonic() - started
        p95 = sorted(ix_latencies)[int(min(len(ix_latencies) - 1, len(ix_latencies) * 0.95))] if ix_latencies else 0.0
        starvation = len(ix_latencies) < 5
        status = GateStatus.FAIL if starvation else GateStatus.PASS
        evidence = ReleaseEvidence.begin(gate="3.7_isolation", environment="local", mode=ExecutionMode.LOCAL_FIXTURE, release_identity=self.config.release_identity)
        evidence.complete(
            status=status,
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics={
                "interactive_claimed": len(ix_latencies),
                "interactive_p95_ms": round(p95, 2),
                "batch_enqueued": 50,
                "duration_s": round(duration, 3),
                "starvation": starvation,
            },
        )
        self.store.save(evidence)
        return {"status": status.value, "evidence_id": evidence.evidence_id, "metrics": evidence.safe_metrics}
