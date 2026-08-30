"""AcquisitionService — public internal API for the Data Acquisition Platform."""

from __future__ import annotations

from dataclasses import replace

from acquisition.batch import enqueue_acquisition_job
from acquisition.change import detect_record_change
from acquisition.crawler import ControlledCrawler, CrawlLimits, CrawlResult
from acquisition.entity import resolve_entities
from acquisition.errors import AcquisitionDeniedError, CapacityRejectedError
from acquisition.freshness import freshness_label
from acquisition.manager import AcquisitionManager
from acquisition.models import (
    JOB_CANCELLED,
    MODE_CRAWL,
    MODE_SCRAPE,
    MODE_SINGLE,
    AcquisitionJob,
    AcquisitionRequest,
    CrawlPolicy,
    EntityMatchResult,
    ParsedRecord,
    RawArtifact,
    SourceDefinition,
    SourceDescriptor,
    utc_now,
)
from acquisition.observability import get_observer
from acquisition.parsers import ParserRegistry, build_default_parser_registry
from acquisition.pipeline import AcquisitionPipeline, PipelineResult
from acquisition.planner import AcquisitionPlanner, PlannedAcquisition
from acquisition.registry import SourceRegistry
from acquisition.schedule import AcquisitionScheduler
from acquisition.scrape import ScrapePipeline, ScrapingProfile
from acquisition.source_policy import SourcePolicy
from acquisition.store import AcquisitionStore, InMemoryAcquisitionStore
from acquisition.trust import trust_weight
from security.tenant import normalize_tenant_id, require_tenant_id


class AcquisitionService:
    def __init__(
        self,
        *,
        source_registry: SourceRegistry | None = None,
        store: AcquisitionStore | None = None,
        parser_registry: ParserRegistry | None = None,
        tool_gateway=None,
        scheduler: AcquisitionScheduler | None = None,
        task_queue=None,
        max_frontier: int = 500,
    ):
        self.sources = source_registry or SourceRegistry()
        self.store = store or InMemoryAcquisitionStore()
        self.parsers = parser_registry or build_default_parser_registry()
        self.gateway = tool_gateway
        self.task_queue = task_queue
        self.max_frontier = int(max_frontier)
        self.manager = AcquisitionManager(
            source_registry=self.sources,
            tool_gateway=tool_gateway,
            store=self.store,
        )
        self.policy = SourcePolicy()
        self.planner = AcquisitionPlanner(source_policy=self.policy)
        self.crawler = ControlledCrawler(
            self.manager, store=self.store, source_policy=self.policy
        )
        self.scraper = ScrapePipeline(self.manager)
        self.pipeline = AcquisitionPipeline(service=self)
        self.scheduler = scheduler or AcquisitionScheduler()
        self.observer = get_observer()
        self.last_metrics: dict = {}

    # --- Sources ---
    def register_source(self, descriptor: SourceDescriptor) -> SourceDescriptor:
        self.sources.register(descriptor)
        self.store.save_source(descriptor)
        return descriptor

    def register_source_definition(self, definition: SourceDefinition) -> SourceDefinition:
        self.register_source(definition.to_descriptor())
        return definition

    def list_sources(self, *, tenant_id: str, include_disabled: bool = False):
        return self.sources.list_sources(tenant_id=tenant_id, include_disabled=include_disabled)

    def get_source(self, source_id: str, *, tenant_id: str) -> SourceDescriptor:
        return self.sources.get(source_id, tenant_id=tenant_id)

    def set_source_enabled(self, source_id: str, *, tenant_id: str, enabled: bool) -> SourceDescriptor:
        desc = self.sources.enable(source_id, tenant_id=tenant_id, enabled=enabled)
        self.store.save_source(desc)
        return desc

    # --- Jobs / planner ---
    def plan_job(
        self,
        *,
        source_id: str,
        tenant_id: str,
        mode: str,
        seeds: tuple[str, ...] = (),
        actor_id: str = "",
        workflow_id: str = "",
        estimated_pages: int | None = None,
        scrape_profile: ScrapingProfile | None = None,
        crawl_policy: CrawlPolicy | None = None,
        metadata: dict | None = None,
    ) -> PlannedAcquisition:
        source = self.sources.get(source_id, tenant_id=tenant_id)
        return self.planner.plan(
            source=source,
            mode=mode,
            tenant_id=tenant_id,
            actor_id=actor_id,
            workflow_id=workflow_id,
            seeds=seeds,
            estimated_pages=estimated_pages,
            scrape_profile_id=scrape_profile.profile_id if scrape_profile else "",
            scrape_profile_version=scrape_profile.version if scrape_profile else "",
            crawl_policy=crawl_policy,
            metadata=metadata,
        )

    def submit_job(
        self,
        *,
        source_id: str,
        tenant_id: str,
        mode: str,
        seeds: tuple[str, ...] = (),
        actor_id: str = "",
        workflow_id: str = "",
        estimated_pages: int | None = None,
        scrape_profile: ScrapingProfile | None = None,
        crawl_policy: CrawlPolicy | None = None,
        metadata: dict | None = None,
        enqueue: bool | None = None,
    ) -> tuple[AcquisitionJob, object | None]:
        """Submit job — large crawl/scrape ALWAYS stamps batch TaskQueue metadata."""
        tid = require_tenant_id(tenant_id)
        planned = self.plan_job(
            source_id=source_id,
            tenant_id=tid,
            mode=mode,
            seeds=seeds,
            actor_id=actor_id,
            workflow_id=workflow_id,
            estimated_pages=estimated_pages,
            scrape_profile=scrape_profile,
            crawl_policy=crawl_policy,
            metadata=metadata,
        )
        max_front = int(dict(planned.job.counters).get("max_frontier") or self.max_frontier)
        if len(seeds) > max_front:
            self.observer.on_capacity_rejected(tenant_id=tid, reason="frontier_seeds_exceeded")
            raise CapacityRejectedError("frontier_capacity_rejected")

        job = planned.job
        if hasattr(self.store, "save_job"):
            self.store.save_job(job)
        self.observer.on_job_submitted(
            job_id=job.job_id,
            tenant_id=tid,
            mode=mode,
            lane=planned.execution_lane,
        )

        should_enqueue = planned.enqueue if enqueue is None else bool(enqueue)
        task = None
        if should_enqueue:
            if self.task_queue is None:
                job = replace(job, status="queued", updated_at=utc_now())
                if hasattr(self.store, "save_job"):
                    self.store.save_job(job)
            else:
                task = enqueue_acquisition_job(self.task_queue, planned=planned)
                job = replace(job, status="queued", updated_at=utc_now())
                if hasattr(self.store, "save_job"):
                    self.store.save_job(job)
        return job, task

    def cancel_job(self, job_id: str, *, tenant_id: str) -> AcquisitionJob:
        tid = require_tenant_id(tenant_id)
        if not hasattr(self.store, "get_job"):
            raise AcquisitionDeniedError("job_store_unavailable")
        job = self.store.get_job(job_id, tenant_id=tid)
        if job is None:
            raise AcquisitionDeniedError("job_not_found")
        cancelled = replace(
            job,
            cancel_requested=True,
            status=JOB_CANCELLED,
            updated_at=utc_now(),
            completed_at=utc_now(),
        )
        self.store.save_job(cancelled)
        self.crawler.request_cancel(job_id)
        self.observer.on_job_completed(job_id=job_id, tenant_id=tid, status=JOB_CANCELLED)
        return cancelled

    def get_job(self, job_id: str, *, tenant_id: str) -> AcquisitionJob | None:
        if not hasattr(self.store, "get_job"):
            return None
        return self.store.get_job(job_id, tenant_id=require_tenant_id(tenant_id))

    # --- Acquisition ---
    async def acquire(self, request: AcquisitionRequest) -> RawArtifact:
        return await self.manager.acquire(request)

    async def crawl(
        self,
        *,
        source_id: str,
        tenant_id: str,
        seeds: tuple[str, ...],
        workflow_id: str = "",
        max_depth: int = 1,
        max_pages: int = 10,
        job: AcquisitionJob | None = None,
        resume: bool = False,
        limits: CrawlLimits | None = None,
    ) -> CrawlResult:
        source = self.sources.get(source_id, tenant_id=tenant_id)
        crawl_limits = limits or CrawlLimits(max_depth=max_depth, max_pages=max_pages)
        result = await self.crawler.crawl(
            source=source,
            seeds=seeds,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            limits=crawl_limits,
            job=job,
            resume=resume,
        )
        self.observer.on_pages(
            fetched=result.pages_fetched,
            failed=result.pages_failed,
            skipped=result.pages_skipped,
        )
        return result

    async def scrape(
        self,
        *,
        source_id: str,
        tenant_id: str,
        seed_url: str,
        profile: ScrapingProfile | None = None,
        job: AcquisitionJob | None = None,
        workflow_id: str = "",
    ):
        source = self.sources.get(source_id, tenant_id=tenant_id)
        return await self.scraper.run(
            source=source,
            seed_url=seed_url,
            tenant_id=tenant_id,
            profile=profile,
            job=job,
            workflow_id=workflow_id,
            cancel_check=lambda: bool(job and job.cancel_requested),
        )

    async def run_job(
        self,
        job: AcquisitionJob,
        *,
        seeds: tuple[str, ...] = (),
        profile: ScrapingProfile | None = None,
        dataset_name: str = "default",
        process: bool = True,
    ) -> PipelineResult | CrawlResult:
        """Execute planned job (crawl/scrape/single) then optional pipeline."""
        seed_urls = seeds or tuple(dict(job.metadata).get("seeds") or ())
        if job.mode == MODE_SCRAPE:
            scrape_result = await self.scrape(
                source_id=job.source_id,
                tenant_id=job.tenant_id,
                seed_url=seed_urls[0] if seed_urls else "",
                profile=profile,
                job=job,
                workflow_id=job.workflow_id,
            )
            artifacts = scrape_result.artifacts
            if not process:
                return scrape_result  # type: ignore[return-value]
        elif job.mode in {MODE_CRAWL, MODE_SINGLE}:
            result = await self.crawl(
                source_id=job.source_id,
                tenant_id=job.tenant_id,
                seeds=seed_urls,
                workflow_id=job.workflow_id,
                max_depth=1 if job.mode == MODE_SINGLE else 2,
                max_pages=1 if job.mode == MODE_SINGLE else int(
                    dict(job.counters).get("max_pages") or 50
                ),
                job=job,
            )
            if not process:
                return result
            artifacts = result.artifacts
        else:
            artifacts = ()
        return self.pipeline.process_artifacts(
            job=job,
            artifacts=artifacts,
            dataset_name=dataset_name,
        )

    def ingest_text(
        self,
        *,
        source_id: str,
        tenant_id: str,
        text: str,
        content_type: str,
        url: str = "",
        metadata: dict | None = None,
    ) -> RawArtifact:
        source = self.sources.get(source_id, tenant_id=tenant_id)
        return self.manager.ingest_text_artifact(
            source=source,
            tenant_id=tenant_id,
            text=text,
            content_type=content_type,
            url=url,
            metadata=metadata,
        )

    # --- Parse ---
    def parse_artifact(self, artifact_id: str, *, tenant_id: str) -> tuple[ParsedRecord, ...]:
        artifact = self.store.get_artifact(artifact_id, tenant_id=tenant_id)
        if artifact is None:
            raise AcquisitionDeniedError("artifact_not_found")
        return self.parse(artifact)

    def parse(self, artifact: RawArtifact) -> tuple[ParsedRecord, ...]:
        records = self.parsers.parse(artifact)
        stored = []
        changes = []
        for rec in records:
            try:
                source = self.sources.get(rec.source_id, tenant_id=rec.tenant_id)
                weight = trust_weight(source.trust_level)
                from dataclasses import replace as dc_replace

                rec = dc_replace(
                    rec,
                    confidence=min(1.0, float(rec.confidence) * (0.5 + 0.5 * weight)),
                    freshness=freshness_label(
                        fetched_at=artifact.fetched_at,
                        policy=source.freshness_policy,
                    ),
                )
            except Exception:
                pass

            fields = dict(rec.fields)
            natural = str(
                fields.get("ean")
                or fields.get("sku")
                or fields.get("supplier_sku")
                or fields.get("mpn")
                or ""
            )
            previous = None
            if natural and hasattr(self.store, "find_previous_observation"):
                previous = self.store.find_previous_observation(
                    tenant_id=rec.tenant_id,
                    source_id=rec.source_id,
                    natural_key=natural,
                )
            else:
                previous = self.store.find_record_by_fingerprint(
                    rec.fingerprint, tenant_id=rec.tenant_id, source_id=rec.source_id
                )

            existing = self.store.find_record_by_fingerprint(
                rec.fingerprint, tenant_id=rec.tenant_id, source_id=rec.source_id
            )
            if existing is not None:
                stored.append(existing)
                continue

            saved = self.store.save_record(rec)
            event = detect_record_change(previous=previous, current=saved)
            self.store.save_change(event)
            changes.append(event)
            stored.append(saved)

        self.last_metrics = {
            "records_produced": len(stored),
            "changes": len(changes),
            "artifact_id": artifact.artifact_id,
            "source_id": artifact.source_id,
            "tenant_id": normalize_tenant_id(artifact.tenant_id),
        }
        return tuple(stored)

    # --- Query ---
    def list_records(self, *, tenant_id: str, source_id: str | None = None, record_type: str | None = None):
        return self.store.list_records(
            tenant_id=tenant_id, source_id=source_id, record_type=record_type
        )

    def get_record(self, record_id: str, *, tenant_id: str) -> ParsedRecord | None:
        return self.store.get_record(record_id, tenant_id=tenant_id)

    def list_changes(self, *, tenant_id: str, source_id: str | None = None, record_id: str | None = None):
        return self.store.list_changes(
            tenant_id=tenant_id, source_id=source_id, record_id=record_id
        )

    def get_provenance(self, record_id: str, *, tenant_id: str) -> dict:
        rec = self.store.get_record(record_id, tenant_id=tenant_id)
        if rec is None:
            raise AcquisitionDeniedError("record_not_found")
        art = self.store.get_artifact(rec.artifact_id, tenant_id=tenant_id)
        source = None
        try:
            source = self.sources.get(rec.source_id, tenant_id=tenant_id)
        except Exception:
            source = self.store.get_source(rec.source_id, tenant_id=tenant_id)
        return {
            "record_id": rec.record_id,
            "record_provenance": dict(rec.provenance),
            "artifact": {
                "artifact_id": art.artifact_id if art else rec.artifact_id,
                "checksum": art.checksum if art else None,
                "url": art.url if art else None,
                "fetched_at": art.fetched_at.isoformat() if art else None,
                "content_trust": art.content_trust if art else rec.content_trust,
            },
            "source": {
                "source_id": rec.source_id,
                "trust_level": source.trust_level if source else None,
                "source_type": source.source_type if source else None,
            },
            "parser": {"parser_id": rec.parser_id, "version": rec.parser_version},
            "freshness": rec.freshness,
            "fingerprint": rec.fingerprint,
        }

    def resolve_entity(self, left_id: str, right_id: str, *, tenant_id: str) -> EntityMatchResult:
        left = self.store.get_record(left_id, tenant_id=tenant_id)
        right = self.store.get_record(right_id, tenant_id=tenant_id)
        if left is None or right is None:
            raise AcquisitionDeniedError("record_not_found")
        return resolve_entities(left, right)

    # --- Schedule ---
    def schedule_refresh(
        self,
        *,
        schedule_id: str,
        source_id: str,
        tenant_id: str,
        interval_seconds: float,
        target: str = "",
        acquisition_type: str = "http_get",
    ):
        self.sources.get(source_id, tenant_id=tenant_id)
        return self.scheduler.register_source_refresh(
            schedule_id=schedule_id,
            source_id=source_id,
            tenant_id=tenant_id,
            interval_seconds=interval_seconds,
            target=target,
            acquisition_type=acquisition_type,
        )
