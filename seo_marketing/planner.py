"""SEO planner — trusted batch admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from security.tenant import require_tenant_id
from seo_marketing.errors import SeoBatchRequired
from seo_marketing.policy import MAX_SYNC_ANALYTICS_ROWS, MAX_SYNC_KEYWORDS, MAX_SYNC_SC_ROWS, MAX_SYNC_URLS
from task_queue.lanes import LANE_BULK, WORKLOAD_BATCH, classify_workload

TRUSTED_JOB_SEO_LARGE = "seo_large"
TRUSTED_JOB_SEO_BULK = "seo_bulk"


@dataclass(frozen=True)
class PlannedSeoJob:
    trusted_metadata: Mapping[str, object]
    execution_lane: str
    workload_class: str
    enqueue: bool
    reason: str = ""


def assert_sync_seo_allowed(
    *,
    keyword_count: int = 0,
    url_count: int = 0,
    sc_rows: int = 0,
    analytics_rows: int = 0,
    bulk: bool = False,
) -> None:
    if bulk:
        raise SeoBatchRequired()
    if keyword_count > MAX_SYNC_KEYWORDS:
        raise SeoBatchRequired()
    if url_count > MAX_SYNC_URLS:
        raise SeoBatchRequired()
    if sc_rows > MAX_SYNC_SC_ROWS:
        raise SeoBatchRequired()
    if analytics_rows > MAX_SYNC_ANALYTICS_ROWS:
        raise SeoBatchRequired()


def plan_seo_job(
    *,
    tenant_id: str,
    site_id: str,
    keyword_count: int = 0,
    url_count: int = 0,
    bulk: bool = False,
) -> PlannedSeoJob:
    tenant = require_tenant_id(tenant_id)
    heavy = bulk or keyword_count > MAX_SYNC_KEYWORDS or url_count > MAX_SYNC_URLS
    if not heavy:
        return PlannedSeoJob(
            trusted_metadata={"tenant_id": tenant, "site_id": site_id, "workload_class": "normal"},
            execution_lane="background",
            workload_class="normal",
            enqueue=False,
        )
    meta = {
        "tenant_id": tenant,
        "site_id": site_id,
        "trusted_job_type": TRUSTED_JOB_SEO_BULK if bulk else TRUSTED_JOB_SEO_LARGE,
        "workload_class": WORKLOAD_BATCH,
        "execution_lane": LANE_BULK,
    }
    stamped = classify_workload(metadata=meta)
    return PlannedSeoJob(
        trusted_metadata={**meta, "execution_lane": stamped.lane, "workload_class": stamped.name},
        execution_lane=stamped.lane,
        workload_class=stamped.name,
        enqueue=True,
        reason=TRUSTED_JOB_SEO_BULK if bulk else TRUSTED_JOB_SEO_LARGE,
    )
