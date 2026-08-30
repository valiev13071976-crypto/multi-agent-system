"""Content planner — trusted batch admission for heavy workloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from content_intel.errors import ContentBatchRequired
from security.tenant import require_tenant_id
from task_queue.lanes import LANE_BULK, WORKLOAD_BATCH, classify_workload

LARGE_SYNC_ITEMS = 10
LARGE_BATCH_ITEMS = 50
LARGE_SYNC_BYTES = 256 * 1024

TRUSTED_JOB_CONTENT_LARGE = "content_large"
TRUSTED_JOB_CONTENT_BULK = "content_bulk"


@dataclass(frozen=True)
class PlannedContentJob:
    trusted_metadata: Mapping[str, object]
    execution_lane: str
    workload_class: str
    enqueue: bool
    reason: str = ""


def assert_hard_batch_admission(trusted_metadata: Mapping[str, object]) -> None:
    meta = dict(trusted_metadata or {})
    stamped = classify_workload(metadata=meta)
    jt = str(meta.get("trusted_job_type") or "")
    if jt in {TRUSTED_JOB_CONTENT_LARGE, TRUSTED_JOB_CONTENT_BULK}:
        if stamped.lane != LANE_BULK or str(meta.get("workload_class")) != WORKLOAD_BATCH:
            raise RuntimeError("content_batch_admission_stamp_failed")


def _must_batch(*, item_count: int | None, byte_size: int | None, bulk: bool) -> tuple[bool, str]:
    count = int(item_count) if item_count is not None else 0
    size = int(byte_size) if byte_size is not None else 0
    if bulk or count >= LARGE_BATCH_ITEMS or size >= LARGE_SYNC_BYTES * 2:
        return True, TRUSTED_JOB_CONTENT_BULK
    if count >= LARGE_SYNC_ITEMS or size >= LARGE_SYNC_BYTES:
        return True, TRUSTED_JOB_CONTENT_LARGE
    return False, ""


def plan_content_job(
    *,
    tenant_id: str,
    project_id: str,
    item_count: int | None = None,
    byte_size: int | None = None,
    bulk: bool = False,
    force_interactive_hint: bool = False,
) -> PlannedContentJob:
    _ = force_interactive_hint
    tenant = require_tenant_id(tenant_id)
    enqueue, job_type = _must_batch(item_count=item_count, byte_size=byte_size, bulk=bulk)
    if not enqueue:
        return PlannedContentJob(
            trusted_metadata={
                "tenant_id": tenant,
                "project_id": project_id,
                "workload_class": "normal",
                "execution_lane": "background",
            },
            execution_lane="background",
            workload_class="normal",
            enqueue=False,
        )
    meta = {
        "tenant_id": tenant,
        "project_id": project_id,
        "trusted_job_type": job_type,
        "workload_class": WORKLOAD_BATCH,
        "execution_lane": LANE_BULK,
    }
    stamped = classify_workload(metadata=meta)
    return PlannedContentJob(
        trusted_metadata={**meta, "execution_lane": stamped.lane, "workload_class": stamped.name},
        execution_lane=stamped.lane,
        workload_class=stamped.name,
        enqueue=True,
        reason=job_type,
    )


def assert_sync_content_allowed(*, item_count: int = 1, byte_size: int = 0, bulk: bool = False) -> None:
    enqueue, _ = _must_batch(item_count=item_count, byte_size=byte_size, bulk=bulk)
    if enqueue:
        raise ContentBatchRequired()
