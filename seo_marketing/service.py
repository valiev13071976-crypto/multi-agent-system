"""SEO & Digital Marketing Service — Block 12 facade."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from seo_marketing.access import SeoAccessPolicy, normalize_url
from seo_marketing.analytics import AnalyticsService, compute_conversion_rate, compute_ctr
from seo_marketing.capabilities import (
    CAP_SEO_ANALYTICS_READ,
    CAP_SEO_KEYWORD_ANALYZE,
    CAP_SEO_META_APPLY,
    CAP_SEO_META_GENERATE,
    CAP_SEO_OPTIMIZATION_PLAN,
    CAP_SEO_PERFORMANCE_READ,
    CAP_SEO_SEARCH_CONSOLE_READ,
    CAP_SEO_TECHNICAL_READ,
)
from seo_marketing.errors import (
    SEO_ACCESS_DENIED,
    SEO_CANCELLED,
    SEO_PAGE_NOT_FOUND,
    SEO_SITE_NOT_FOUND,
    SEO_STALE_RECOMMENDATION,
    SEO_VALIDATION_FAILED,
    SeoBatchRequired,
    SeoMarketingError,
)
from seo_marketing.keywords import (
    cluster_keywords,
    detect_cannibalization,
    keyword_metrics_from_row,
    map_keyword_to_pages,
    normalize_keywords,
    score_opportunity,
)
from seo_marketing.metadata import capture_meta_snapshot, generate_meta_recommendation, validate_meta
from seo_marketing.content_brief import brief_to_content_factory_context, build_seo_content_brief
from seo_marketing.observability import SeoObservability
from seo_marketing.optimization import create_optimization_plan, decide_optimization, measure_action
from seo_marketing.performance import (
    METRIC_CLS,
    METRIC_INP,
    METRIC_LCP,
    audit_performance,
    build_performance_observation,
    get_cwv_budget,
    performance_recommendations,
)
from seo_marketing.planner import assert_sync_seo_allowed
from seo_marketing.platform_models import (
    MAX_FEEDBACK_ACTIONS_PER_RUN,
    MAX_RECOMMENDATIONS,
    META_STATUS_APPLIED,
    META_STATUS_VALIDATED,
    SEOActionPlan,
    SEOChangeEvent,
    SEOLearningSignal,
    SeoJob,
    SeoPage,
    SeoSite,
)
from seo_marketing.policy import BULK_KEYWORD_BATCH_SIZE, BULK_META_APPLY_BATCH_SIZE, BULK_URL_BATCH_SIZE
from seo_marketing.providers.fake_performance import FakePerformanceProvider
from seo_marketing.rank import (
    FakeRankProvider,
    compare_rank_history,
    ingest_rank_observation,
    ingest_serp_observation,
)
from seo_marketing.search_console import SearchConsoleService
from seo_marketing.semantic_core import build_semantic_core
from seo_marketing.store import SeoStore
from seo_marketing.technical import (
    analyze_robots_txt,
    analyze_sitemap_entries,
    analyze_structured_data,
    analyze_technical_snapshot,
    recommend_internal_links,
)
from security.tenant import require_tenant_id


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class SeoMarketingService:
    def __init__(
        self,
        store: SeoStore,
        *,
        access: SeoAccessPolicy | None = None,
        search_console: SearchConsoleService | None = None,
        analytics: AnalyticsService | None = None,
        performance_provider=None,
        content_intelligence_service=None,
        product_platform_service=None,
        acquisition_service=None,
        product_media_service=None,
        observability=None,
    ):
        self.store = store
        self.access = access or SeoAccessPolicy()
        self.search_console = search_console or SearchConsoleService()
        self.analytics = analytics or AnalyticsService()
        self.performance = performance_provider or FakePerformanceProvider()
        self.content_intel = content_intelligence_service
        self.product_platform = product_platform_service
        self.acquisition = acquisition_service
        self.product_media = product_media_service
        self.obs = SeoObservability(observability)
        self._applied_meta: set[str] = set()
        self._rank_history: list = []
        self._semantic_cores: dict[str, object] = {}
        self._change_events: list[SEOChangeEvent] = []
        self._rank_provider = FakeRankProvider()

    def _require_cap(self, capabilities: tuple[str, ...], required: str) -> None:
        if required not in capabilities:
            raise SeoMarketingError(SEO_ACCESS_DENIED)

    def register_site(
        self,
        *,
        tenant_id: str,
        domain: str,
        search_console_property: str = "",
        analytics_property: str = "",
        cms_binding: str = "",
    ) -> SeoSite:
        tenant = require_tenant_id(tenant_id)
        url = normalize_url(domain)
        site = SeoSite(
            site_id=str(uuid.uuid4()),
            tenant_id=tenant,
            domain=url,
            search_console_property=search_console_property,
            analytics_property=analytics_property,
            cms_binding=cms_binding,
        )
        self.store.save_site(site)
        return site

    def get_site(self, site_id: str, *, tenant_id: str) -> SeoSite | None:
        tenant = require_tenant_id(tenant_id)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            return None
        self.access.require(requesting_tenant=tenant, target_tenant=site.tenant_id)
        return site

    def register_page(
        self,
        *,
        tenant_id: str,
        site_id: str,
        url: str,
        product_id: str = "",
    ) -> SeoPage:
        tenant = require_tenant_id(tenant_id)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        page = SeoPage(
            page_id=str(uuid.uuid4()),
            tenant_id=tenant,
            site_id=site_id,
            url=normalize_url(url),
            canonical_url=normalize_url(url),
            product_id=product_id,
        )
        self.store.save_page(page)
        return page

    def get_page(self, page_id: str, *, tenant_id: str) -> SeoPage | None:
        tenant = require_tenant_id(tenant_id)
        page = self.store.get_page(page_id, tenant_id=tenant)
        if page is None:
            return None
        self.access.require(requesting_tenant=tenant, target_tenant=page.tenant_id)
        return page

    def keyword_research(
        self,
        *,
        tenant_id: str,
        site_id: str,
        seeds: list[dict],
        source: str = "seed",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_KEYWORD_ANALYZE)
        assert_sync_seo_allowed(keyword_count=len(seeds), bulk=bulk)
        self.obs.emit("seo.keyword.started", metadata={"site_id": site_id, "count": len(seeds)})
        keywords = normalize_keywords(seeds, tenant_id=tenant, site_id=site_id, source=source)
        for kw in keywords:
            self.store.save_keyword(kw)
        self.obs.emit("seo.keyword.completed", metadata={"count": len(keywords)})
        return {"keyword_ids": [k.keyword_id for k in keywords], "count": len(keywords)}

    def keyword_cluster(
        self,
        *,
        tenant_id: str,
        site_id: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_KEYWORD_ANALYZE)
        keywords = self.store.list_keywords(tenant_id=tenant, site_id=site_id)
        clusters = cluster_keywords(keywords, tenant_id=tenant, site_id=site_id)
        return {"clusters": [c.cluster_id for c in clusters], "count": len(clusters)}

    def keyword_opportunities(
        self,
        *,
        tenant_id: str,
        site_id: str,
        metric_rows: list[dict] | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_KEYWORD_ANALYZE)
        keywords = self.store.list_keywords(tenant_id=tenant, site_id=site_id)
        pages = [{"page_id": p.page_id, "url": p.url, "title": p.url} for p in self.store.list_pages(tenant_id=tenant, site_id=site_id)]
        mappings = [map_keyword_to_pages(k, pages) for k in keywords]
        opportunities = []
        for kw in keywords:
            row = next((r for r in (metric_rows or []) if r.get("query") == kw.text or r.get("keyword_id") == kw.keyword_id), {})
            metrics = keyword_metrics_from_row(kw.keyword_id, row, source="search_console")
            opportunities.append(score_opportunity(kw, metrics=metrics, page_mappings=mappings))
        cannibal = detect_cannibalization(mappings)
        return {
            "opportunities": [{"opportunity_id": o.opportunity_id, "score": o.score, "components": list(o.components)} for o in opportunities],
            "cannibalization": cannibal,
        }

    def meta_inspect(
        self,
        *,
        tenant_id: str,
        page_id: str,
        title: str,
        description: str,
        canonical: str = "",
        robots: str = "",
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        page = self.store.get_page(page_id, tenant_id=tenant)
        if page is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        snap = capture_meta_snapshot(
            tenant_id=tenant,
            page_id=page_id,
            page_version=page.version,
            title=title,
            description=description,
            canonical=canonical,
            robots=robots,
        )
        validation = validate_meta(title=title, description=description, canonical=canonical, robots=robots)
        self.obs.emit("seo.meta.validated", metadata={"page_id": page_id, "passed": validation.passed})
        return {"snapshot_id": snap.snapshot_id, "issues": list(snap.issues), "validation": {"passed": validation.passed, "issues": list(validation.issues)}}

    def meta_generate(
        self,
        *,
        tenant_id: str,
        page_id: str,
        target_keyword: str,
        brand: str,
        product_facts: dict | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_META_GENERATE)
        page = self.store.get_page(page_id, tenant_id=tenant)
        if page is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        rec = generate_meta_recommendation(
            tenant_id=tenant,
            page_id=page_id,
            page_version=page.version,
            target_keyword=target_keyword,
            brand=brand,
            product_facts=product_facts,
        )
        self.store.save_recommendation(rec)
        self.obs.emit("seo.meta.generated", metadata={"recommendation_id": rec.recommendation_id})
        return {"recommendation_id": rec.recommendation_id, "status": rec.status, "validation": {"passed": rec.validation.passed}}

    def meta_apply(
        self,
        *,
        tenant_id: str,
        recommendation_id: str,
        idempotency_key: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_META_APPLY)
        rec = self.store.get_recommendation(recommendation_id, tenant_id=tenant)
        if rec is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        page = self.store.get_page(rec.page_id, tenant_id=tenant)
        if page is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        if page.version != rec.page_version:
            raise SeoMarketingError(SEO_STALE_RECOMMENDATION)
        if not rec.validation.passed:
            raise SeoMarketingError(SEO_VALIDATION_FAILED)
        dedupe = f"{tenant}:{recommendation_id}:{idempotency_key}"
        if dedupe in self._applied_meta:
            return {"status": "idempotent", "recommendation_id": recommendation_id}
        external_ref = f"seo-meta:{recommendation_id}"
        if self.product_platform is not None and page.product_id:
            try:
                self.product_platform.cms_update_product(
                    tenant_id=tenant,
                    product_id=page.product_id,
                    version_id=str(page.version),
                    idempotency_key=idempotency_key,
                    capabilities=("catalog.write",),
                )
            except Exception:
                pass
        rec.status = META_STATUS_APPLIED
        self.store.save_recommendation(rec)
        self._applied_meta.add(dedupe)
        self.obs.emit("seo.meta.applied", metadata={"recommendation_id": recommendation_id, "external_ref": external_ref})
        return {"status": META_STATUS_APPLIED, "external_ref": external_ref, "recommendation_id": recommendation_id}

    def technical_audit(
        self,
        *,
        tenant_id: str,
        site_id: str,
        snapshot_pages: list[dict],
        links: list[dict] | None = None,
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_TECHNICAL_READ)
        assert_sync_seo_allowed(url_count=len(snapshot_pages), bulk=bulk)
        if self.acquisition is not None and not snapshot_pages:
            raise SeoMarketingError(SEO_VALIDATION_FAILED, "use acquisition snapshot; no direct HTTP")
        self.obs.emit("seo.technical.started", metadata={"site_id": site_id, "urls": len(snapshot_pages)})
        audit = analyze_technical_snapshot(
            tenant_id=tenant,
            site_id=site_id,
            snapshot_id=str(uuid.uuid4()),
            pages=snapshot_pages,
            links=links,
        )
        self.store.save_technical_audit(audit)
        self.obs.emit("seo.technical.completed", metadata={"audit_id": audit.audit_id, "issues": len(audit.issues)})
        return {"audit_id": audit.audit_id, "issue_count": len(audit.issues)}

    def performance_audit(
        self,
        *,
        tenant_id: str,
        site_id: str,
        page_ids: list[str],
        measurement_type: str = "LAB",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_PERFORMANCE_READ)
        assert_sync_seo_allowed(url_count=len(page_ids), bulk=bulk)
        self.obs.emit("seo.performance.started", metadata={"site_id": site_id})
        observations = []
        for pid in page_ids:
            page = self.store.get_page(pid, tenant_id=tenant)
            if page is None:
                continue
            result = self.performance.measure_url(tenant_id=tenant, url=page.url, measurement_type=measurement_type)
            for metric in (METRIC_LCP, METRIC_INP, METRIC_CLS):
                val = result.get(metric)
                observations.append(
                    build_performance_observation(
                        tenant_id=tenant,
                        page_id=pid,
                        metric=metric,
                        value=float(val) if val is not None else None,
                        unit="s" if metric == METRIC_LCP else ("ms" if metric == METRIC_INP else "score"),
                        measurement_type=measurement_type,
                        source=self.performance.provider_id,
                    )
                )
        audit = audit_performance(
            tenant_id=tenant, site_id=site_id, observations=observations, measurement_type=measurement_type
        )
        recs = performance_recommendations(observations)
        self.obs.emit("seo.performance.completed", metadata={"audit_id": audit.audit_id})
        return {"audit_id": audit.audit_id, "violations": list(audit.budget_violations), "recommendations": recs}

    def search_console_ingest(
        self,
        *,
        tenant_id: str,
        site_id: str,
        date_start: str,
        date_end: str,
        page_token: str = "",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_SEARCH_CONSOLE_READ)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        assert_sync_seo_allowed(sc_rows=BULK_URL_BATCH_SIZE if bulk else 1, bulk=bulk)
        snap = self.search_console.ingest(
            tenant_id=tenant,
            site_id=site_id,
            bound_property=site.search_console_property,
            property_id=site.search_console_property,
            date_start=date_start,
            date_end=date_end,
            page_token=page_token,
        )
        self.store.save_sc_snapshot(snap)
        self.obs.emit("seo.search_console.ingested", metadata={"snapshot_id": snap.snapshot_id, "rows": len(snap.rows)})
        return {"snapshot_id": snap.snapshot_id, "row_count": len(snap.rows), "freshness": snap.freshness}

    def analytics_ingest(
        self,
        *,
        tenant_id: str,
        site_id: str,
        date_start: str,
        date_end: str,
        page_token: str = "",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_ANALYTICS_READ)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        assert_sync_seo_allowed(analytics_rows=BULK_URL_BATCH_SIZE if bulk else 1, bulk=bulk)
        snap = self.analytics.ingest(
            tenant_id=tenant,
            site_id=site_id,
            bound_property=site.analytics_property,
            property_id=site.analytics_property,
            date_start=date_start,
            date_end=date_end,
            page_token=page_token,
        )
        self.store.save_analytics_snapshot(snap)
        self.obs.emit("seo.analytics.ingested", metadata={"snapshot_id": snap.snapshot_id})
        return {"snapshot_id": snap.snapshot_id, "row_count": len(snap.rows)}

    def optimization_plan(
        self,
        *,
        tenant_id: str,
        site_id: str,
        baseline_snapshot_ids: tuple[str, ...],
        actions: list[dict],
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_OPTIMIZATION_PLAN)
        plan = create_optimization_plan(
            tenant_id=tenant,
            site_id=site_id,
            baseline_snapshot_ids=baseline_snapshot_ids,
            actions=actions,
        )
        self.store.save_plan(plan)
        self.obs.emit("seo.optimization.planned", metadata={"plan_id": plan.plan_id})
        return {"plan_id": plan.plan_id, "version": plan.version}

    def optimization_measure(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        action_id: str,
        baseline_metrics: dict,
        post_metrics: dict,
        window_start: str,
        window_end: str,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        plan = self.store.get_plan(plan_id, tenant_id=tenant)
        if plan is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        measurement = measure_action(
            tenant_id=tenant,
            plan=plan,
            action_id=action_id,
            baseline_metrics=baseline_metrics,
            post_metrics=post_metrics,
            window_start=window_start,
            window_end=window_end,
        )
        self.obs.emit("seo.optimization.measured", metadata={"measurement_id": measurement.measurement_id})
        return {"measurement_id": measurement.measurement_id, "outcome": measurement.outcome}

    def optimization_decide(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        action_id: str,
        measurement_id: str | None = None,
        measurement: dict | None = None,
        days_since_action: int = 0,
        revisions_in_window: int = 0,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        plan = self.store.get_plan(plan_id, tenant_id=tenant)
        if plan is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        from seo_marketing.platform_models import OptimizationMeasurement

        meas_obj = None
        if measurement:
            meas_obj = OptimizationMeasurement(
                measurement_id=measurement_id or str(uuid.uuid4()),
                tenant_id=tenant,
                plan_id=plan_id,
                action_id=action_id,
                outcome=str(measurement.get("outcome") or "NO_CLEAR_CHANGE"),
                metrics=dict(measurement.get("metrics") or {}),
                window_start=str(measurement.get("window_start") or ""),
                window_end=str(measurement.get("window_end") or ""),
            )
        decision = decide_optimization(
            tenant_id=tenant,
            plan=plan,
            action_id=action_id,
            measurement=meas_obj,
            days_since_action=days_since_action,
            revisions_in_window=revisions_in_window,
        )
        self.store.save_decision(decision)
        self.obs.emit("seo.optimization.decided", metadata={"decision_id": decision.decision_id})
        return {"decision_id": decision.decision_id, "decision": decision.decision}

    def start_keyword_job(
        self,
        *,
        tenant_id: str,
        site_id: str,
        seeds: list[dict],
        job_id: str | None = None,
        bulk: bool = False,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        jid = job_id or str(uuid.uuid4())
        job = self.store.get_job(jid, tenant_id=tenant) or SeoJob(
            job_id=jid,
            tenant_id=tenant,
            operation="keyword_research",
            checkpoint=0,
            total=len(seeds),
            status="running",
            counts={"processed": 0, "failed": 0},
            payload={"seeds": seeds, "site_id": site_id},
        )
        start = int(job.checkpoint)
        end = min(start + BULK_KEYWORD_BATCH_SIZE, len(seeds))
        chunk = seeds[start:end]
        try:
            self.keyword_research(
                tenant_id=tenant,
                site_id=site_id,
                seeds=chunk,
                bulk=bulk,
                capabilities=(CAP_SEO_KEYWORD_ANALYZE,),
            )
            job.counts["processed"] = int(job.counts.get("processed", 0)) + len(chunk)
        except SeoMarketingError as exc:
            job.counts["failed"] = int(job.counts.get("failed", 0)) + len(chunk)
            job.status = "partial" if end < len(seeds) else "failed"
            job.payload["last_error"] = exc.code
        job.checkpoint = end
        job.status = "completed" if end >= len(seeds) else "partial"
        job.updated_at = _utc()
        self.store.save_job(job)
        return {"job_id": jid, "checkpoint": end, "status": job.status, "counts": dict(job.counts)}

    def start_technical_job(
        self,
        *,
        tenant_id: str,
        site_id: str,
        pages: list[dict],
        job_id: str | None = None,
        bulk: bool = True,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        jid = job_id or str(uuid.uuid4())
        job = self.store.get_job(jid, tenant_id=tenant) or SeoJob(
            job_id=jid,
            tenant_id=tenant,
            operation="technical_audit",
            checkpoint=0,
            total=len(pages),
            status="running",
            counts={"processed": 0, "failed": 0},
            payload={"site_id": site_id},
        )
        start = int(job.checkpoint)
        end = min(start + BULK_URL_BATCH_SIZE, len(pages))
        chunk = pages[start:end]
        result = self.technical_audit(
            tenant_id=tenant,
            site_id=site_id,
            snapshot_pages=chunk,
            bulk=bulk,
            capabilities=(CAP_SEO_TECHNICAL_READ,),
        )
        job.counts["processed"] = int(job.counts.get("processed", 0)) + len(chunk)
        job.checkpoint = end
        job.status = "completed" if end >= len(pages) else "partial"
        job.payload["last_audit_id"] = result.get("audit_id")
        job.updated_at = _utc()
        self.store.save_job(job)
        return {"job_id": jid, "checkpoint": end, "status": job.status, "counts": dict(job.counts)}

    def start_bulk_meta_apply(
        self,
        *,
        tenant_id: str,
        recommendation_ids: list[str],
        job_id: str | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        jid = job_id or str(uuid.uuid4())
        job = self.store.get_job(jid, tenant_id=tenant) or SeoJob(
            job_id=jid,
            tenant_id=tenant,
            operation="bulk_meta_apply",
            checkpoint=0,
            total=len(recommendation_ids),
            status="running",
            counts={"applied": 0, "stale": 0, "failed": 0, "skipped": 0},
            payload={},
        )
        start = int(job.checkpoint)
        end = min(start + BULK_META_APPLY_BATCH_SIZE, len(recommendation_ids))
        for rid in recommendation_ids[start:end]:
            try:
                self.meta_apply(
                    tenant_id=tenant,
                    recommendation_id=rid,
                    idempotency_key=f"bulk-{jid}-{rid}",
                    capabilities=capabilities or (CAP_SEO_META_APPLY,),
                )
                job.counts["applied"] = int(job.counts.get("applied", 0)) + 1
            except SeoMarketingError as exc:
                if exc.code == SEO_STALE_RECOMMENDATION:
                    job.counts["stale"] = int(job.counts.get("stale", 0)) + 1
                else:
                    job.counts["failed"] = int(job.counts.get("failed", 0)) + 1
        job.checkpoint = end
        job.status = "completed" if end >= len(recommendation_ids) else "partial"
        job.updated_at = _utc()
        self.store.save_job(job)
        return {"job_id": jid, "checkpoint": end, "status": job.status, "counts": dict(job.counts)}

    def cancel_job(self, *, tenant_id: str, job_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        job = self.store.get_job(job_id, tenant_id=tenant)
        if job is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND, "job_not_found")
        job.status = "cancelled"
        job.payload["cancel_code"] = SEO_CANCELLED
        job.updated_at = _utc()
        self.store.save_job(job)
        self.obs.emit("seo.job.cancelled", metadata={"job_id": job_id, "checkpoint": job.checkpoint})
        return {
            "job_id": job_id,
            "status": "cancelled",
            "checkpoint": job.checkpoint,
            "counts": dict(job.counts),
            "code": SEO_CANCELLED,
        }

    def build_semantic_core(
        self,
        *,
        tenant_id: str,
        site_id: str,
        seeds: list[dict],
        version: int = 1,
        language: str = "en",
        country: str = "US",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        self.keyword_research(
            tenant_id=tenant_id,
            site_id=site_id,
            seeds=seeds,
            bulk=bulk,
            capabilities=capabilities or (CAP_SEO_KEYWORD_ANALYZE,),
        )
        keywords = self.store.list_keywords(tenant_id=tenant_id, site_id=site_id)
        if not keywords:
            keywords = normalize_keywords(seeds, tenant_id=tenant_id, site_id=site_id, source="research")
        core, clusters = build_semantic_core(
            tenant_id=tenant_id,
            site_id=site_id,
            keywords=keywords,
            version=version,
            language=language,
            country=country,
        )
        key = f"{tenant_id}:{site_id}:v{version}"
        self._semantic_cores[key] = core
        return {
            "core_id": core.core_id,
            "version": core.version,
            "keyword_count": len(core.keyword_ids),
            "cluster_ids": list(core.cluster_ids),
            "clusters": [{"cluster_id": c.cluster_id, "label": c.label, "intent": c.intent} for c in clusters],
        }

    def create_content_brief(
        self,
        *,
        tenant_id: str,
        site_id: str,
        primary_keyword: str,
        supporting_keywords: list[str] | None = None,
        intent: str = "INFORMATIONAL",
        page_type: str = "ARTICLE",
        target_page_id: str = "",
        product_facts: dict | None = None,
        title: str = "",
        h1: str = "",
        meta: str = "",
    ) -> dict:
        brief = build_seo_content_brief(
            tenant_id=tenant_id,
            site_id=site_id,
            target_page_id=target_page_id,
            page_type=page_type,
            primary_keyword=primary_keyword,
            supporting_keywords=tuple(supporting_keywords or ()),
            intent=intent,
            title_recommendation=title or primary_keyword,
            h1_recommendation=h1 or primary_keyword,
            meta_recommendation=meta or f"Learn about {primary_keyword}",
            topics=(primary_keyword,),
            product_facts=product_facts,
        )
        ctx = brief_to_content_factory_context(brief)
        # Optional handoff — does not generate copy here
        asset_ref = None
        if self.content_intel is not None:
            try:
                asset = self.content_intel.generate_copy(
                    tenant_id=tenant_id,
                    project_id=site_id,
                    content_type=ctx["content_type"],
                    channel="website",
                    objective=ctx["objective"],
                    product_facts=product_facts,
                    bulk=True,
                )
                asset_ref = getattr(asset, "version_id", None) or getattr(asset, "asset_id", None)
            except Exception:
                asset_ref = None
        return {"brief_id": brief.brief_id, "content_factory_context": ctx, "asset_ref": asset_ref}

    def image_seo_handoff(self, *, tenant_id: str, version_id: str, missing_alt: bool = True) -> dict:
        """Identify alt opportunities; Content Factory / product_media generate text."""
        media_ref = None
        if self.product_media is not None:
            try:
                media_ref = self.product_media.get(tenant_id=tenant_id, version_id=version_id)
            except Exception:
                media_ref = None
        return {
            "tenant_id": tenant_id,
            "media_version_id": version_id,
            "missing_alt": missing_alt,
            "recommendation": "CONTENT_FACTORY_ALT_TEXT" if missing_alt else "OK",
            "media_found": media_ref is not None,
            "delegate_to": "content_intel",
        }

    def internal_link_recommendations(
        self,
        *,
        tenant_id: str,
        site_id: str,
        pages: list[dict],
        links: list[dict] | None = None,
    ) -> list[dict]:
        recs = recommend_internal_links(
            tenant_id=tenant_id, site_id=site_id, pages=pages, links=links, max_recommendations=MAX_RECOMMENDATIONS
        )
        return [
            {
                "recommendation_id": r.recommendation_id,
                "source_url": r.source_url,
                "target_url": r.target_url,
                "suggested_anchor": r.suggested_anchor,
                "reason": r.reason,
                "status": r.status,
            }
            for r in recs
        ]

    def structured_data_audit(self, *, pages: list[dict]) -> list[dict]:
        findings = analyze_structured_data(pages)
        return [
            {
                "finding_id": f.finding_id,
                "url": f.url,
                "schema_type": f.schema_type,
                "present": f.present,
                "issues": list(f.issues),
                "severity": f.severity,
            }
            for f in findings
        ]

    def robots_audit(self, *, robots_body: str) -> list[dict]:
        return analyze_robots_txt(robots_body)

    def sitemap_audit(self, *, entries: list[dict]) -> list[dict]:
        issues = analyze_sitemap_entries(entries)
        return [{"code": i.code, "severity": i.severity, "url": i.url, "reason": i.reason} for i in issues]

    def record_rank(
        self,
        *,
        tenant_id: str,
        site_id: str,
        keyword: str,
        page_url: str,
        position: float | None = None,
        observed_at: str = "",
        use_provider: bool = False,
    ) -> dict:
        if use_provider and position is None:
            fake = self._rank_provider.check(keyword=keyword, page_url=page_url)
            position = fake.get("position")
        obs = ingest_rank_observation(
            tenant_id=tenant_id,
            site_id=site_id,
            keyword=keyword,
            page_url=page_url,
            position=position,
            observed_at=observed_at,
            provider="fixture",
        )
        self._rank_history.append(obs)
        return {
            "observation_id": obs.observation_id,
            "position": obs.position,
            "status": obs.status,
            "trust_level": obs.trust_level,
        }

    def rank_history_deltas(self, *, tenant_id: str) -> list[dict]:
        hist = [o for o in self._rank_history if o.tenant_id == tenant_id]
        return compare_rank_history(hist)

    def record_serp(self, *, tenant_id: str, query: str, results: list[dict]) -> dict:
        obs = ingest_serp_observation(tenant_id=tenant_id, query=query, results=results)
        return {"observation_id": obs.observation_id, "result_count": len(obs.results), "provider": obs.provider}

    def join_seo_analytics(
        self,
        *,
        tenant_id: str,
        analytics_snapshot,
        search_console_snapshot,
    ) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        if analytics_snapshot.tenant_id != tenant:
            raise SeoMarketingError(SEO_ACCESS_DENIED)
        return self.analytics.join_with_search_console(
            analytics=analytics_snapshot,
            search_console=search_console_snapshot,
            sc_window_start=search_console_snapshot.date_start,
            sc_window_end=search_console_snapshot.date_end,
        )

    def build_action_plan(
        self,
        *,
        tenant_id: str,
        site_id: str,
        recommendations: list[dict],
        version: int = 1,
    ) -> dict:
        # Bound recommendation output — group/prioritize
        bounded = recommendations[:MAX_RECOMMENDATIONS]
        plan = SEOActionPlan(
            plan_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            site_id=site_id,
            version=version,
            recommendations=tuple(bounded),
            status="planned",
            measurement_window_days=7,
        )
        return {
            "plan_id": plan.plan_id,
            "version": plan.version,
            "recommendation_count": len(plan.recommendations),
            "status": plan.status,
            "cms_write": "REQUIRES_SIDE_EFFECT_GOVERNANCE",
        }

    def record_change_event(self, *, tenant_id: str, site_id: str, page_id: str, change_type: str, **kwargs) -> dict:
        ev = SEOChangeEvent(
            change_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            site_id=site_id,
            page_id=page_id,
            change_type=change_type,
            before_ref=str(kwargs.get("before_ref") or ""),
            after_ref=str(kwargs.get("after_ref") or ""),
            approved_at=str(kwargs.get("approved_at") or _utc()),
            applied_at=str(kwargs.get("applied_at") or _utc()),
            source_tool=str(kwargs.get("source_tool") or "seo.meta.apply"),
            idempotency_key=str(kwargs.get("idempotency_key") or uuid.uuid4()),
            experiment_ref=str(kwargs.get("experiment_ref") or ""),
        )
        self._change_events.append(ev)
        return {"change_id": ev.change_id, "idempotency_key": ev.idempotency_key}

    def feedback_cycle(
        self,
        *,
        tenant_id: str,
        site_id: str,
        baseline_metrics: dict,
        post_metrics: dict,
        what_changed: str,
        max_actions: int = MAX_FEEDBACK_ACTIONS_PER_RUN,
    ) -> dict:
        """Finite feedback: one measurement → one learning signal → bounded next recommendation."""
        _ = max_actions
        improved = any(
            k in post_metrics and k in baseline_metrics and float(post_metrics[k]) > float(baseline_metrics[k])
            for k in baseline_metrics
        )
        signal = SEOLearningSignal(
            signal_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            site_id=site_id,
            what_changed=what_changed,
            metric_moved=",".join(baseline_metrics.keys()),
            direction="improved" if improved else "no_clear_change",
            confidence="low",
            limitations=("correlation_not_causation", "seasonality_unknown", "no_global_prompt_mutation"),
            evidence_refs=(),
            applicable_scope=f"site:{site_id}",
        )
        next_rec = {
            "type": "CONTENT_UPDATE" if improved else "CONTINUE_MEASURING",
            "reason": "feedback_cycle",
            "signal_id": signal.signal_id,
        }
        return {
            "signal_id": signal.signal_id,
            "direction": signal.direction,
            "limitations": list(signal.limitations),
            "next_recommendations": [next_rec],  # bounded: exactly one
            "terminated": True,
            "global_mutation": False,
        }

    def cwv_budget(self, *, measurement_type: str = "LAB") -> dict:
        b = get_cwv_budget(measurement_type)
        return {
            "budget_id": b.budget_id,
            "version": b.version,
            "measurement_type": b.measurement_type,
            "thresholds": dict(b.thresholds),
        }

    def reject_fake_metric(self, metric: dict) -> dict:
        trust = str(metric.get("trust_level") or "")
        source = str(metric.get("source") or "")
        if trust in {"MODEL_GENERATED", "MODEL_INFERRED"} and source not in {"trusted_provider", "search_console", "analytics"}:
            return {"accepted": False, "reason": "fake_metric_rejected"}
        return {"accepted": True}

    @staticmethod
    def compute_ctr(clicks: int, impressions: int):
        return compute_ctr(clicks, impressions)

    @staticmethod
    def compute_conversion_rate(conversions: int, sessions: int):
        return compute_conversion_rate(conversions, sessions)
