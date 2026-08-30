"""Bounded batching + hard TaskQueue admission for large acquisition workloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from acquisition.models import AcquisitionJob
from acquisition.planner import AcquisitionPlanner, PlannedAcquisition
from task_queue.lanes import LANE_BULK, WORKLOAD_BATCH, classify_workload


@dataclass(frozen=True)
class AcquisitionBatchPlan:
    """Plan for enqueueing crawl/parse work — does not run network itself."""

    source_id: str
    tenant_id: str
    urls: tuple[str, ...]
    batch_size: int = 20
    workflow_id: str = ""
    trusted_metadata: Mapping[str, object] | None = None
    execution_lane: str = LANE_BULK

    def batches(self) -> tuple[tuple[str, ...], ...]:
        size = max(1, int(self.batch_size))
        chunks = []
        for i in range(0, len(self.urls), size):
            chunks.append(tuple(self.urls[i : i + size]))
        return tuple(chunks)

    def execution_keys(self) -> tuple[str, ...]:
        return tuple(
            f"acq-batch:{self.tenant_id}:{self.source_id}:{i}:{len(batch)}"
            for i, batch in enumerate(self.batches())
        )


def plan_crawl_batches(
    *,
    source_id: str,
    tenant_id: str,
    urls: tuple[str, ...],
    batch_size: int = 20,
    workflow_id: str = "",
    max_urls: int = 500,
) -> AcquisitionBatchPlan:
    bounded = tuple(urls[: max(0, int(max_urls))])
    # Hard stamp — large crawl URLs always batch/bulk regardless of caller hint.
    trusted = {
        "trusted_job_type": "crawler",
        "workload_class": WORKLOAD_BATCH,
        "execution_lane": LANE_BULK,
    }
    stamped = classify_workload(metadata=trusted)
    if stamped.lane != LANE_BULK:
        raise RuntimeError("acquisition_batch_stamp_failed")
    return AcquisitionBatchPlan(
        source_id=source_id,
        tenant_id=tenant_id,
        urls=bounded,
        batch_size=batch_size,
        workflow_id=workflow_id,
        trusted_metadata=trusted,
        execution_lane=LANE_BULK,
    )


def enqueue_acquisition_job(
    task_queue,
    *,
    planned: PlannedAcquisition,
    execution_key: str | None = None,
    priority: str = "low",
) -> object:
    """Enqueue via TaskQueue with HARD-stamped trusted metadata (not hint-only)."""
    meta = dict(planned.trusted_metadata)
    # Absolute enforcement for crawl/scrape
    if planned.job.mode in {"crawl", "scrape"} or planned.workload_class == WORKLOAD_BATCH:
        meta["trusted_job_type"] = meta.get("trusted_job_type") or (
            "scraping" if planned.job.mode == "scrape" else "crawler"
        )
        meta["workload_class"] = WORKLOAD_BATCH
        meta["execution_lane"] = LANE_BULK
    classified = classify_workload(metadata=meta)
    if planned.job.mode in {"crawl", "scrape"} and classified.lane != LANE_BULK:
        raise RuntimeError("hard_batch_admission_failed")
    key = execution_key or f"acq-job:{planned.job.tenant_id}:{planned.job.job_id}"
    return task_queue.enqueue(
        workflow_id=planned.job.workflow_id or f"acquisition:{planned.job.job_id}",
        task_id=f"acq:{planned.job.job_id}",
        execution_key=key,
        tenant_id=planned.job.tenant_id,
        priority=priority,
        execution_lane=str(meta["execution_lane"]),
        metadata=meta,
        actor_ref=planned.job.actor_id or "",
    )


def submit_large_crawl(
    task_queue,
    *,
    planner: AcquisitionPlanner,
    source,
    tenant_id: str,
    seeds: tuple[str, ...],
    actor_id: str = "",
    workflow_id: str = "",
    estimated_pages: int | None = None,
) -> tuple[AcquisitionJob, object]:
    """Plan + enqueue large crawl with trusted crawler→batch admission."""
    planned = planner.plan(
        source=source,
        mode="crawl",
        tenant_id=tenant_id,
        actor_id=actor_id,
        workflow_id=workflow_id,
        seeds=seeds,
        estimated_pages=estimated_pages,
        force_interactive_hint=True,  # deliberately ignored for crawl
    )
    task = enqueue_acquisition_job(task_queue, planned=planned)
    return planned.job, task
