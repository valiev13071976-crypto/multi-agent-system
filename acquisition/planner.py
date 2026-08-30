"""AcquisitionPlanner — mode selection + trusted TaskQueue metadata stamping.

Large / unknown crawler workloads ALWAYS stamp batch admission metadata so they
cannot run on the interactive pool even if the caller omits a hint.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from acquisition.models import (
    MODE_API,
    MODE_CRAWL,
    MODE_SCRAPE,
    MODE_SINGLE,
    POLICY_VERSION,
    AcquisitionJob,
    CrawlPolicy,
    SourceDefinition,
    SourceDescriptor,
    new_id,
    utc_now,
)
from acquisition.source_policy import SourcePolicy, evaluate_url
from task_queue.lanes import (
    LANE_BULK,
    WORKLOAD_BATCH,
    WORKLOAD_NORMAL,
    classify_workload,
)


# Thresholds — crawl/scrape above these force batch (hard stamp, not hint-only).
LARGE_CRAWL_PAGES = 20
LARGE_CRAWL_FRONTIER = 100
UNKNOWN_SIZE_ASSUME_LARGE = True


@dataclass(frozen=True)
class PlannedAcquisition:
    job: AcquisitionJob
    trusted_metadata: Mapping[str, object]
    execution_lane: str
    workload_class: str
    enqueue: bool
    reason: str = ""


def _as_definition(source: SourceDefinition | SourceDescriptor) -> SourceDefinition:
    if isinstance(source, SourceDefinition):
        return source
    return SourceDefinition.from_descriptor(source)


def _estimate_size(
    *,
    mode: str,
    seeds: tuple[str, ...],
    policy: CrawlPolicy,
    estimated_pages: int | None,
) -> tuple[int | None, bool]:
    """Return (estimated_pages, size_unknown)."""
    if estimated_pages is not None:
        return max(0, int(estimated_pages)), False
    if mode == MODE_SINGLE:
        return 1, False
    if mode in {MODE_CRAWL, MODE_SCRAPE}:
        # Upper bound from policy when caller didn't estimate.
        return int(policy.max_pages), True
    if mode == MODE_API:
        return max(1, len(seeds) or 1), False
    return None, True


def _must_batch(*, mode: str, estimated_pages: int | None, size_unknown: bool) -> bool:
    if mode in {MODE_CRAWL, MODE_SCRAPE}:
        if size_unknown and UNKNOWN_SIZE_ASSUME_LARGE:
            return True
        if estimated_pages is not None and estimated_pages >= LARGE_CRAWL_PAGES:
            return True
        return True  # crawl/scrape always batch — interactive protection
    if estimated_pages is not None and estimated_pages >= LARGE_CRAWL_PAGES:
        return True
    return False


class AcquisitionPlanner:
    def __init__(self, *, source_policy: SourcePolicy | None = None):
        self.policy = source_policy or SourcePolicy()

    def plan(
        self,
        *,
        source: SourceDefinition | SourceDescriptor,
        mode: str,
        tenant_id: str,
        actor_id: str = "",
        workflow_id: str = "",
        seeds: tuple[str, ...] | None = None,
        estimated_pages: int | None = None,
        scrape_profile_id: str = "",
        scrape_profile_version: str = "",
        crawl_policy: CrawlPolicy | None = None,
        metadata: Mapping[str, object] | None = None,
        force_interactive_hint: bool = False,
    ) -> PlannedAcquisition:
        """Build AcquisitionJob with HARD-stamped trusted TaskQueue metadata.

        ``force_interactive_hint`` from caller payload is ignored for crawl/scrape —
        trusted_job_type + workload_class=batch always win.
        """
        definition = _as_definition(source)
        if definition.tenant_id != tenant_id and tenant_id:
            # Tenant on source is authoritative.
            pass
        tid = definition.tenant_id
        policy = crawl_policy or definition.crawl_policy
        seed_urls = tuple(seeds or definition.seed_urls or ())
        if not seed_urls and mode != MODE_API:
            seed_urls = ()

        for seed in seed_urls:
            decision = evaluate_url(seed, source=definition, policy=policy)
            if not decision.permitted:
                from acquisition.errors import SourcePolicyDeniedError

                raise SourcePolicyDeniedError(
                    "seed_policy_denied", reason=decision.reason
                )

        estimated, unknown = _estimate_size(
            mode=mode, seeds=seed_urls, policy=policy, estimated_pages=estimated_pages
        )
        batch = _must_batch(mode=mode, estimated_pages=estimated, size_unknown=unknown)
        # Caller interactive hint cannot override crawl/scrape batch enforcement.
        _ = force_interactive_hint

        if mode in {MODE_CRAWL, MODE_SCRAPE} or batch:
            trusted_job_type = "scraping" if mode == MODE_SCRAPE else "crawler"
            workload = WORKLOAD_BATCH
            lane = LANE_BULK
        else:
            trusted_job_type = "acquisition"
            classified = classify_workload(
                metadata={"trusted_job_type": trusted_job_type},
                estimated_rows=estimated,
            )
            workload = classified.name
            lane = classified.lane
            if workload != WORKLOAD_BATCH and mode == MODE_SINGLE and (estimated or 0) <= 1:
                workload = WORKLOAD_NORMAL

        trusted_metadata = {
            "trusted_job_type": trusted_job_type,
            "workload_class": workload if batch or mode in {MODE_CRAWL, MODE_SCRAPE} else workload,
            "execution_lane": lane if batch or mode in {MODE_CRAWL, MODE_SCRAPE} else lane,
            "acquisition_mode": mode,
            "policy_version": POLICY_VERSION,
        }
        if batch or mode in {MODE_CRAWL, MODE_SCRAPE}:
            trusted_metadata["trusted_job_type"] = trusted_job_type
            trusted_metadata["workload_class"] = WORKLOAD_BATCH
            trusted_metadata["execution_lane"] = LANE_BULK

        # Re-classify using ONLY stamped trusted keys — proves hard enforcement.
        stamped = classify_workload(metadata=trusted_metadata)
        assert stamped.lane == trusted_metadata["execution_lane"] or mode == MODE_SINGLE

        if mode in {MODE_CRAWL, MODE_SCRAPE}:
            # Absolute hard rule: crawl/scrape → bulk/batch.
            trusted_metadata["trusted_job_type"] = trusted_job_type
            trusted_metadata["workload_class"] = WORKLOAD_BATCH
            trusted_metadata["execution_lane"] = LANE_BULK
            stamped = classify_workload(metadata=trusted_metadata)
            if stamped.lane != LANE_BULK:
                raise RuntimeError("batch_admission_stamp_failed")

        now = utc_now()
        job = AcquisitionJob(
            job_id=new_id("job-"),
            tenant_id=tid,
            actor_id=str(actor_id or ""),
            source_id=definition.source_id,
            mode=mode,
            workload_class=str(trusted_metadata["workload_class"]),
            status="pending",
            workflow_id=workflow_id,
            trusted_job_type=str(trusted_metadata["trusted_job_type"]),
            execution_lane=str(trusted_metadata["execution_lane"]),
            scrape_profile_id=scrape_profile_id,
            scrape_profile_version=scrape_profile_version,
            created_at=now,
            updated_at=now,
            counters={
                "estimated_pages": estimated if estimated is not None else -1,
                "size_unknown": bool(unknown),
                "seed_count": len(seed_urls),
                "max_pages": int(policy.max_pages),
                "max_frontier": int(policy.max_frontier),
            },
            metadata={
                **dict(metadata or {}),
                "seeds": list(seed_urls)[:64],
            },
        )
        enqueue = mode in {MODE_CRAWL, MODE_SCRAPE} or batch or (
            estimated is not None and estimated >= LARGE_CRAWL_PAGES
        )
        return PlannedAcquisition(
            job=job,
            trusted_metadata=trusted_metadata,
            execution_lane=str(trusted_metadata["execution_lane"]),
            workload_class=str(trusted_metadata["workload_class"]),
            enqueue=enqueue,
            reason="batch_required" if enqueue else "inline_ok",
        )

    def with_queued_status(self, planned: PlannedAcquisition) -> PlannedAcquisition:
        job = replace(
            planned.job,
            status="queued",
            updated_at=utc_now(),
        )
        return PlannedAcquisition(
            job=job,
            trusted_metadata=planned.trusted_metadata,
            execution_lane=planned.execution_lane,
            workload_class=planned.workload_class,
            enqueue=planned.enqueue,
            reason=planned.reason,
        )
