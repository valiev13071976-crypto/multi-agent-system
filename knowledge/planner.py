"""KnowledgePlanner — trusted batch admission for large ingestion workloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from knowledge.errors import KnowledgeBatchRequired
from security.tenant import require_tenant_id
from task_queue.lanes import LANE_BULK, WORKLOAD_BATCH, classify_workload

LARGE_SYNC_BYTES = 256 * 1024  # 256 KiB
LARGE_BATCH_BYTES = 512 * 1024  # 512 KiB — large-corpus acceptance threshold
LARGE_SYNC_CHUNKS = 50
LARGE_BATCH_CHUNKS = 200

TRUSTED_JOB_KNOWLEDGE_LARGE = "knowledge_large"


@dataclass(frozen=True)
class PlannedKnowledgeJob:
    trusted_metadata: Mapping[str, object]
    execution_lane: str
    workload_class: str
    enqueue: bool
    reason: str = ""


def assert_hard_batch_admission(trusted_metadata: Mapping[str, object]) -> None:
    meta = dict(trusted_metadata or {})
    stamped = classify_workload(metadata=meta)
    jt = str(meta.get("trusted_job_type") or "")
    if jt == TRUSTED_JOB_KNOWLEDGE_LARGE:
        if stamped.lane != LANE_BULK or str(meta.get("workload_class")) != WORKLOAD_BATCH:
            raise RuntimeError("knowledge_batch_admission_stamp_failed")


def _must_batch(*, byte_size: int | None, chunk_count: int | None, bulk: bool) -> tuple[bool, str]:
    size = int(byte_size) if byte_size is not None else 0
    chunks = int(chunk_count) if chunk_count is not None else 0
    if bulk:
        return True, TRUSTED_JOB_KNOWLEDGE_LARGE
    if size >= LARGE_BATCH_BYTES or chunks >= LARGE_BATCH_CHUNKS:
        return True, TRUSTED_JOB_KNOWLEDGE_LARGE
    if size >= LARGE_SYNC_BYTES or chunks >= LARGE_SYNC_CHUNKS:
        return True, TRUSTED_JOB_KNOWLEDGE_LARGE
    return False, ""


def plan_knowledge_job(
    *,
    tenant_id: str,
    source_id: str,
    byte_size: int | None = None,
    chunk_count: int | None = None,
    bulk: bool = False,
    force_interactive_hint: bool = False,
) -> PlannedKnowledgeJob:
    _ = force_interactive_hint  # caller hint cannot downgrade
    tenant = require_tenant_id(tenant_id)
    enqueue, job_type = _must_batch(byte_size=byte_size, chunk_count=chunk_count, bulk=bulk)
    if not enqueue:
        return PlannedKnowledgeJob(
            trusted_metadata={
                "tenant_id": tenant,
                "source_id": source_id,
                "workload_class": "normal",
                "execution_lane": "background",
            },
            execution_lane="background",
            workload_class="normal",
            enqueue=False,
        )
    meta = {
        "tenant_id": tenant,
        "source_id": source_id,
        "trusted_job_type": job_type,
        "workload_class": WORKLOAD_BATCH,
        "execution_lane": LANE_BULK,
    }
    stamped = classify_workload(metadata=meta)
    return PlannedKnowledgeJob(
        trusted_metadata={
            **meta,
            "execution_lane": stamped.lane,
            "workload_class": stamped.name,
        },
        execution_lane=stamped.lane,
        workload_class=stamped.name,
        enqueue=True,
        reason=job_type,
    )


def assert_sync_ingest_allowed(
    *,
    byte_size: int,
    chunk_count: int | None = None,
    bulk: bool = False,
) -> None:
    enqueue, _ = _must_batch(byte_size=byte_size, chunk_count=chunk_count, bulk=bulk)
    if enqueue:
        raise KnowledgeBatchRequired()
