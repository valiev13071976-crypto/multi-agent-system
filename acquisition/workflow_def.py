"""``acquisition.crawl_process`` workflow — durable multi-page crawl (Block 5.2).

Mirrors ``data_intel.workflow_def``'s ``data.large_process`` composition
pattern: two dependent handler steps registered against the pre-existing
Block 3 ``WorkflowDefinition``/``WorkflowPlatform`` runtime. No
crawler-specific queue/worker/persistence is created here — the durable job
identity, retry policy, and progress/status contract are entirely the
existing workflow runtime's; this module only supplies the two step
handlers.

Step 1 (``acquisition_crawl_prepare``) primes robots.txt for the seed host
(best-effort, governed through the ``scrape.fetch`` tool — never bypassed).
Step 2 (``acquisition_crawl_run``) runs the bounded crawl through the
pre-existing ``AcquisitionService``/``ControlledCrawler``
(fetch -> parse -> normalize -> dedupe -> ingest), then bridges the
resulting records into a Block 5.1 Data Intelligence dataset so the
conversation can continue (filter / dedupe / export) without re-crawling.
"""

from __future__ import annotations

from acquisition.errors import AcquisitionError
from acquisition.models import MODE_CRAWL, CrawlPolicy
from acquisition.web_source import host_of, load_robots_for_host
from workflow.definition import (
    FAILURE_RETRY,
    STEP_TYPE_HANDLER,
    StepResult,
    StepRetryPolicy,
    WorkflowDefinition,
    WorkflowStep,
)

ACQUISITION_CRAWL_WORKFLOW_TYPE = "acquisition.crawl_process"


def crawl_process_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=ACQUISITION_CRAWL_WORKFLOW_TYPE,
        version="1",
        timeout_seconds=3600.0,
        steps=(
            WorkflowStep(step_id="acquisition_crawl_prepare", step_type=STEP_TYPE_HANDLER),
            WorkflowStep(
                step_id="acquisition_crawl_run",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("acquisition_crawl_prepare",),
                retry_policy=StepRetryPolicy(
                    max_attempts=3,
                    base_delay_seconds=0.01,
                    backoff_mode="fixed",
                    retryable_error_classes=(
                        "AcquisitionTimeoutError",
                        "RateLimitedError",
                    ),
                ),
                failure_policy=FAILURE_RETRY,
            ),
        ),
    )


def _acquisition_service(ctx):
    platform = ctx["platform"]
    engine = getattr(platform, "workflow_engine", None)
    return getattr(engine, "acquisition_service", None) if engine else None


def _data_intelligence(ctx):
    platform = ctx["platform"]
    engine = getattr(platform, "workflow_engine", None)
    return getattr(engine, "data_intelligence", None) if engine else None


async def acquisition_crawl_process_handler(ctx) -> StepResult:
    step = ctx["step"]
    state = ctx["state"]
    meta = dict(state.metadata or {})
    tenant_id = str(meta.get("tenant_id") or getattr(state, "tenant_id", "") or "legacy-default")
    source_id = str(meta.get("source_id") or "")
    seed_url = str(meta.get("seed_url") or "")
    max_pages = max(1, int(meta.get("max_pages") or 1))
    svc = _acquisition_service(ctx)

    if step.step_id == "acquisition_crawl_prepare":
        if svc is None:
            raise AcquisitionError("acquisition_service_unavailable")
        host = host_of(seed_url)
        primed = False
        if host:
            primed = await load_robots_for_host(
                svc, tenant_id=tenant_id, host=host, workflow_id=ctx["workflow_id"]
            )
        return StepResult(ok=True, data={"robots_primed": bool(primed), "host": host})

    if step.step_id == "acquisition_crawl_run":
        if svc is None:
            raise AcquisitionError("acquisition_service_unavailable")
        planned = svc.plan_job(
            source_id=source_id,
            tenant_id=tenant_id,
            mode=MODE_CRAWL,
            seeds=(seed_url,),
            workflow_id=str(ctx["workflow_id"]),
            estimated_pages=max_pages,
            crawl_policy=CrawlPolicy(max_depth=max_pages, max_pages=max_pages),
        )
        if hasattr(svc.store, "save_job"):
            svc.store.save_job(planned.job)
        result = await svc.run_job(
            planned.job, seeds=(seed_url,), dataset_name="acquisition", process=True
        )
        from acquisition.tools import bridge_pipeline_result_to_dataset

        bridged = bridge_pipeline_result_to_dataset(
            result, data_intelligence=_data_intelligence(ctx), tenant_id=tenant_id
        )
        return StepResult(
            ok=True,
            data={"job_id": planned.job.job_id, **bridged},
            result_ref=f"dataset:{bridged.get('dataset_id') or planned.job.job_id}",
        )

    return StepResult(ok=True, data={"step_id": step.step_id})


def register_acquisition_workflows(definitions, platform) -> None:
    try:
        definitions.register(crawl_process_definition())
    except Exception:
        pass
    for step_id in ("acquisition_crawl_prepare", "acquisition_crawl_run"):
        platform.register_handler(step_id, acquisition_crawl_process_handler)
