"""B2B planner — trusted batch admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from b2b_commerce.errors import B2BBatchRequired
from b2b_commerce.policy import MAX_SYNC_WHOLESALE_ROWS
from security.tenant import require_tenant_id
from task_queue.lanes import LANE_BULK, WORKLOAD_BATCH, classify_workload

TRUSTED_JOB_B2B_BULK = "b2b_bulk"


@dataclass(frozen=True)
class PlannedB2BJob:
    trusted_metadata: Mapping[str, object]
    execution_lane: str
    workload_class: str
    enqueue: bool


def assert_sync_b2b_allowed(*, row_count: int = 0, bulk: bool = False) -> None:
    if bulk or row_count > MAX_SYNC_WHOLESALE_ROWS:
        raise B2BBatchRequired()


def plan_b2b_job(*, tenant_id: str, row_count: int = 0, bulk: bool = False) -> PlannedB2BJob:
    tenant = require_tenant_id(tenant_id)
    heavy = bulk or row_count > MAX_SYNC_WHOLESALE_ROWS
    if not heavy:
        return PlannedB2BJob(
            trusted_metadata={"tenant_id": tenant, "workload_class": "normal"},
            execution_lane="background",
            workload_class="normal",
            enqueue=False,
        )
    meta = {
        "tenant_id": tenant,
        "trusted_job_type": TRUSTED_JOB_B2B_BULK,
        "workload_class": WORKLOAD_BATCH,
        "execution_lane": LANE_BULK,
    }
    stamped = classify_workload(metadata=meta)
    return PlannedB2BJob(
        trusted_metadata={**meta, "execution_lane": stamped.lane, "workload_class": stamped.name},
        execution_lane=stamped.lane,
        workload_class=stamped.name,
        enqueue=True,
    )
